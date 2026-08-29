from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.dayforce_collector import (
    DayforceCollector,
    build_dayforce_urls,
    build_search_payload,
    fetch_page_in_browser,
)
from job_board_discovery import discover_job_board_for_row, static_scan
from job_platforms import canonical_job_board_url, detect_job_platform
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = "https://jobs.dayforcehcm.com/en-US/bellco/CANDIDATEPORTAL"


def posting(
    posting_id: int,
    title: str,
    *,
    location: str = "Greenwood Village, CO, USA",
    virtual: bool = False,
    description: str = "",
) -> dict:
    locations = [] if not location else [{"formattedAddress": location, "locationId": posting_id}]
    return {
        "clientNamespace": "bellco",
        "jobBoardId": 123,
        "jobPostingId": posting_id,
        "jobReqId": f"req{posting_id}",
        "jobTitle": title,
        "jobDescription": description or f"<p>Current opening for {title}.</p>",
        "hasVirtualLocation": virtual,
        "postingStartTimestampUTC": "2026-08-28T16:00:00.000Z",
        "postingExpiryTimestampUTC": "2026-09-28T16:00:00.000Z",
        "isEvergreen": False,
        "postingLocations": locations,
        "postingAppliedStatus": 0,
        "searchScore": 1,
    }


def api_page(offset: int, total: int, records: list[dict]) -> dict:
    return {
        "jobPostings": records,
        "maxCount": total,
        "offset": offset,
        "count": len(records),
    }


def page_router(pages: list[tuple[int, dict]]):
    def fake_fetch_all_pages(_collector: DayforceCollector, _board_url: str) -> list[tuple[int, dict]]:
        return pages

    return fake_fetch_all_pages


class FakeSession:
    pass


class DayforceCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-bellco",
            "Company Name": "Bellco Credit Union",
            "Official Website": "https://www.bellco.org/",
            "Careers Page URL": "https://www.bellco.org/about-bellco/careers/",
            "Job Board URL": BOARD_URL,
            # URL detection must win if production still has a stale platform value.
            "Job Platform": "Indeed Company Jobs",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Failed",
        }
        self.records = [
            posting(9191, "Operations Support Specialist I"),
            posting(
                9187,
                "Commercial Credit Analyst",
                description="<p>This position uses a hybrid work schedule.</p>",
            ),
            posting(9176, "BSA/AML Analyst", location="", virtual=True),
            posting(9000, "General Employment Application", location=""),
        ]

    def test_public_api_pages_normalize_filter_and_use_stable_destinations(self) -> None:
        pages = [
            (0, api_page(0, 4, self.records[:2])),
            (2, api_page(2, 4, self.records[2:])),
        ]
        collector = DayforceCollector(delay_seconds=0)
        with (
            patch("collectors.dayforce_collector.DAYFORCE_PAGE_SIZE", 2),
            patch.object(DayforceCollector, "fetch_all_pages", new=page_router(pages)),
        ):
            jobs = collector.collect(self.company)

        self.assertTrue(collector.requires_browser)
        self.assertEqual((collector.candidate_count, collector.rejected_count, collector.saved_count), (4, 1, 3))
        self.assertEqual(
            {job.title for job in jobs},
            {"Operations Support Specialist I", "Commercial Credit Analyst", "BSA/AML Analyst"},
        )
        self.assertEqual(len({job.id for job in jobs}), 3)
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))
        self.assertEqual(
            {job.sourceUrl for job in jobs},
            {f"{BOARD_URL}/jobs/{posting_id}" for posting_id in (9191, 9187, 9176)},
        )
        onsite = next(job for job in jobs if job.title.startswith("Operations Support"))
        hybrid = next(job for job in jobs if job.title.startswith("Commercial Credit"))
        remote = next(job for job in jobs if job.title.startswith("BSA/AML"))
        self.assertEqual((onsite.location, onsite.workType), ("Greenwood Village, CO, USA", "Onsite"))
        self.assertEqual(hybrid.workType, "Hybrid")
        self.assertEqual((remote.location, remote.workType), ("Remote", "Remote"))
        self.assertEqual(onsite.postedDate, "2026-08-28T16:00:00.000Z")
        self.assertEqual(onsite.rawData["jobPostingId"], "9191")

    def test_duplicate_id_fails_closed(self) -> None:
        pages = [
            (0, api_page(0, 4, self.records[:2])),
            (2, api_page(2, 4, [self.records[1], self.records[2]])),
        ]
        collector = DayforceCollector(delay_seconds=0)
        with (
            patch("collectors.dayforce_collector.DAYFORCE_PAGE_SIZE", 2),
            patch.object(DayforceCollector, "fetch_all_pages", new=page_router(pages)),
        ):
            with self.assertRaisesRegex(ValueError, "unique postings"):
                collector.collect(self.company)
        self.assertIn("duplicate Dayforce jobPostingId", {sample["reason"] for sample in collector.rejection_samples})

    def test_incomplete_changing_and_malformed_pagination_fail_closed(self) -> None:
        cases = (
            (
                [(0, api_page(0, 3, self.records[:2])), (2, api_page(2, 3, []))],
                "empty page",
            ),
            (
                [(0, api_page(0, 4, self.records[:2])), (2, api_page(2, 3, self.records[2:3]))],
                "total changed",
            ),
            (
                [(0, api_page(0, 4, self.records[:2])), (3, api_page(3, 4, self.records[2:]))],
                "page sequence changed",
            ),
            (
                [(0, {**api_page(0, 2, self.records[:2]), "count": 1})],
                "did not match",
            ),
        )
        for pages, message in cases:
            with self.subTest(message=message):
                with (
                    patch("collectors.dayforce_collector.DAYFORCE_PAGE_SIZE", 2),
                    patch.object(DayforceCollector, "fetch_all_pages", new=page_router(pages)),
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        DayforceCollector(delay_seconds=0).collect(self.company)

    def test_empty_result_requires_explicit_zero_and_complete_metadata(self) -> None:
        valid_empty = [(0, api_page(0, 0, []))]
        with patch.object(DayforceCollector, "fetch_all_pages", new=page_router(valid_empty)):
            self.assertEqual(DayforceCollector(delay_seconds=0).collect(self.company), [])

        missing_meta = [(0, {"jobPostings": []})]
        with patch.object(DayforceCollector, "fetch_all_pages", new=page_router(missing_meta)):
            with self.assertRaisesRegex(ValueError, "maxCount"):
                DayforceCollector(delay_seconds=0).collect(self.company)

    def test_url_contract_detection_and_request_body_use_verified_tenant_root(self) -> None:
        detail_url = f"{BOARD_URL}/jobs/9191?tracking=ignored"
        board_url, search_url, namespace, board_code, culture = build_dayforce_urls(detail_url)
        self.assertEqual(board_url, BOARD_URL)
        self.assertEqual(search_url, "https://jobs.dayforcehcm.com/api/geo/bellco/jobposting/search")
        self.assertEqual((namespace, board_code, culture), ("bellco", "CANDIDATEPORTAL", "en-US"))
        self.assertEqual(
            build_search_payload(namespace, board_code, culture, 25),
            {
                "clientNamespace": "bellco",
                "jobBoardCode": "CANDIDATEPORTAL",
                "cultureCode": "en-US",
                "distanceUnit": 0,
                "paginationStart": 25,
            },
        )
        self.assertEqual(canonical_job_board_url(detail_url), BOARD_URL)
        self.assertEqual(detect_job_platform(detail_url), "Dayforce")
        self.assertIsInstance(pick_collector(self.company, delay_seconds=0), DayforceCollector)
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            build_dayforce_urls("https://attacker.example/en-US/bellco/CANDIDATEPORTAL/jobs/9191")
        with self.assertRaisesRegex(ValueError, "culture, tenant namespace"):
            build_dayforce_urls("https://jobs.dayforcehcm.com/api/geo/bellco/jobposting/search")
        attacker = "https://jobs.dayforcehcm.com.attacker.example/en-US/bellco/CANDIDATEPORTAL/jobs/9191"
        self.assertEqual(canonical_job_board_url(attacker), attacker)
        self.assertEqual(detect_job_platform(attacker), "")

    def test_official_h5_job_links_discover_the_canonical_dayforce_board(self) -> None:
        html = f"""
        <main>
          <h5><a href="{BOARD_URL}/jobs/9191">Operations Support Specialist I</a></h5>
          <h5><a href="{BOARD_URL}/jobs/9187">Commercial Credit Analyst</a></h5>
        </main>
        """
        with (
            patch(
                "job_board_discovery.fetch_html",
                return_value=("https://www.bellco.org/about-bellco/careers/", html),
            ),
            patch("job_board_discovery.make_session", return_value=FakeSession()),
        ):
            candidates = static_scan(
                "https://www.bellco.org/about-bellco/careers/",
                "Bellco Credit Union",
                FakeSession(),
                "Static Link",
            )
            discovery = discover_job_board_for_row(
                {
                    "Company Name": "Bellco Credit Union",
                    "Official Website": "https://www.bellco.org/",
                    "Careers Page URL": "https://www.bellco.org/about-bellco/careers/",
                    "Job Board URL": "",
                },
            )

        self.assertEqual(len(candidates), 2)
        self.assertEqual({candidate.url for candidate in candidates}, {BOARD_URL})
        self.assertEqual({candidate.platform for candidate in candidates}, {"Dayforce"})
        self.assertTrue(all(not candidate.rejected for candidate in candidates))
        self.assertTrue(discovery.found)
        self.assertEqual((discovery.candidate_selected, discovery.platform), (BOARD_URL, "Dayforce"))

    def test_browser_bootstrap_reuses_observed_csrf_for_later_pages_and_closes_browser(self) -> None:
        first = api_page(0, 3, self.records[:2])
        second = api_page(2, 3, self.records[2:3])
        expected_initial_body = build_search_payload("bellco", "CANDIDATEPORTAL", "en-US", 0)
        request = Mock(method="POST", post_data_json=expected_initial_body)
        request.all_headers.return_value = {"x-csrf-token": "sanitized-test-token"}
        response = Mock(status=200, url="https://jobs.dayforcehcm.com/api/geo/bellco/jobposting/search")
        response.request = request
        response.json.return_value = first

        expectation = MagicMock()
        expectation.__enter__.return_value = MagicMock(value=response)
        page = Mock()

        def expect_response(predicate, **_kwargs):
            self.assertTrue(predicate(response))
            return expectation

        page.expect_response.side_effect = expect_response
        page.evaluate.return_value = {"status": 200, "text": json.dumps(second)}
        context = Mock()
        context.new_page.return_value = page
        browser = Mock()
        browser.new_context.return_value = context
        manager = MagicMock()
        manager.__enter__.return_value = Mock()

        collector = DayforceCollector(delay_seconds=0)
        with (
            patch("collectors.dayforce_collector.DAYFORCE_PAGE_SIZE", 2),
            patch("playwright.sync_api.sync_playwright", return_value=manager),
            patch("collectors.dayforce_collector.launch_playwright_chromium", return_value=browser),
            patch("collectors.dayforce_collector.install_playwright_url_guard"),
            patch("collectors.dayforce_collector.safe_page_goto"),
        ):
            pages = collector.fetch_all_pages(BOARD_URL)

        self.assertEqual(pages, [(0, first), (2, second)])
        evaluate_argument = page.evaluate.call_args.args[1]
        self.assertEqual(evaluate_argument["payload"]["paginationStart"], 2)
        self.assertEqual(evaluate_argument["csrfToken"], "sanitized-test-token")
        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()

    def test_browser_page_fetch_rejects_non_json_and_non_200_responses(self) -> None:
        page = Mock()
        page.evaluate.return_value = {"status": 403, "text": "Forbidden"}
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            fetch_page_in_browser(page, "https://jobs.dayforcehcm.com/api/geo/x/jobposting/search", {}, "token")
        page.evaluate.return_value = {"status": 200, "text": "not-json"}
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            fetch_page_in_browser(page, "https://jobs.dayforcehcm.com/api/geo/x/jobposting/search", {}, "token")

    def test_browser_lease_is_released_when_context_creation_fails(self) -> None:
        browser = Mock()
        browser.new_context.side_effect = RuntimeError("context creation failed")
        manager = MagicMock()
        manager.__enter__.return_value = Mock()
        with (
            patch("playwright.sync_api.sync_playwright", return_value=manager),
            patch("collectors.dayforce_collector.launch_playwright_chromium", return_value=browser),
        ):
            with self.assertRaisesRegex(RuntimeError, "context creation failed"):
                DayforceCollector(delay_seconds=0).fetch_all_pages(BOARD_URL)
        browser.close.assert_called_once_with()

    def test_complete_collection_flow_deduplicates_persists_and_reports_browser_diagnostics(self) -> None:
        pages = [
            (0, api_page(0, 4, self.records[:2])),
            (2, api_page(2, 4, self.records[2:])),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("job_tools.APP_ENABLE_BROWSER_JOBS", True),
                patch("collectors.dayforce_collector.DAYFORCE_PAGE_SIZE", 2),
                patch.object(DayforceCollector, "fetch_all_pages", new=page_router(pages)),
            ):
                summary = collect_jobs(
                    master_path=root / "companies.xlsx",
                    jobs_json_path=jobs_json,
                    jobs_xlsx_path=root / "jobs.xlsx",
                    company_ids={self.company["Company ID"]},
                    max_workers=1,
                    browser_workers=1,
                    delay_seconds=0,
                )

            payload = json.loads(jobs_json.read_text(encoding="utf-8"))
            diagnostics = json.loads(
                (root / "logs" / "job_collection_diagnostics.json").read_text(encoding="utf-8")
            )
            rejected = json.loads(
                (root / "logs" / "rejected_job_candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual((summary["jobs_found"], summary["errors"]), (3, 0))
            self.assertEqual((len(payload), len({job["id"] for job in payload})), (3, 3))
            self.assertEqual(
                (
                    diagnostics[0]["collectorSelected"],
                    diagnostics[0]["playwrightUsed"],
                    diagnostics[0]["validJobsSaved"],
                    diagnostics[0]["status"],
                ),
                ("DayforceCollector", True, 3, "Jobs Collected"),
            )
            self.assertEqual([item["candidateText"] for item in rejected], ["General Employment Application"])

            repository = OpportunityRepository(root / "radar.db", initialize=True)
            repository.upsert_company_snapshots(
                [
                    {
                        "id": self.company["Company ID"],
                        "name": self.company["Company Name"],
                        "officialWebsite": self.company["Official Website"],
                        "careersPageUrl": self.company["Careers Page URL"],
                        "jobBoardUrl": BOARD_URL,
                        "jobPlatform": "Dayforce",
                        "jobBoardDiscoveryMethod": "Verified embedded board",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.replace_raw_candidates(rejected, company_ids={self.company["Company ID"]})
            stored = repository.list_jobs()
            self.assertEqual((len(stored), len({job["sourceUrl"] for job in stored})), (3, 3))
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
