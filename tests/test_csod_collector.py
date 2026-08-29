from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.csod_collector import (
    CSODCollector,
    build_csod_search_payload,
    normalize_csod_board_url,
    parse_csod_bootstrap,
)
from job_platforms import detect_job_platform
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = "https://ufcu.csod.com/ux/ats/careersite/4/home?c=ufcu"
SEARCH_URL = "https://us.api.csod.com/rec-job-search/external/jobs"


class FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


def bootstrap_html(*, corp: str = "ufcu", cloud: str = "https://us.api.csod.com/") -> str:
    route = {"package": "career-site", "page": "home", "cid": "4", "routeInfo": {"_rest_": "?c=ufcu"}}
    context = {
        "corp": corp,
        "user": -3001,
        "cultureID": 1,
        "cultureName": "en-US",
        "endpoints": {"cloud": cloud, "api": "/"},
        "token": "sanitized.test.token.value",
    }
    return (
        f"<script>var csodPlayerRouteInfo={json.dumps(route)};</script>"
        f"<script>if(!csod.context || !csod.context.token) csod.context={json.dumps(context)};</script>"
    )


def requisition(
    requisition_id: int,
    title: str,
    *,
    locations: tuple[tuple[str, str, str], ...] = (("Reno", "NV", "US"),),
    description: str = "",
) -> dict:
    return {
        "requisitionId": requisition_id,
        "postingEffectiveDate": "8/25/2026",
        "postingExpirationDate": "-",
        "displayJobTitle": title,
        "locations": [
            {"city": city, "state": state, "country": country}
            for city, state, country in locations
        ],
        "externalDescription": description or f"Current official opening for {title}.",
    }


def api_response(total: int, records: list[dict]) -> dict:
    return {
        "status": "Success",
        "timestamp": "2026-08-29T04:37:44Z",
        "data": {
            "totalCount": total,
            "requisitions": records,
            "filters": [],
            "customFieldFilters": [],
        },
    }


def page_router(pages: dict[int, dict], calls: list[tuple[str, int]]):
    def fake_fetch(_collector: CSODCollector, config, page_number: int) -> dict:
        calls.append((config.search_url, page_number))
        return pages[page_number]

    return fake_fetch


class CSODCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-unitedfcu-com",
            "Company Name": "United Federal Credit Union",
            "Official Website": "https://unitedfcu.com/",
            "Careers Page URL": "https://unitedfcu.com/about-us/careers",
            "Job Board URL": BOARD_URL,
            # URL detection must win over stale production discovery metadata.
            "Job Platform": "Indeed Company Jobs",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Failed",
        }
        self.records = [
            requisition(305, "Teller - Branch Float - Sparks", locations=(("Sparks", "NV", "US"),)),
            requisition(303, "Collector"),
            requisition(
                290,
                "Manager of Enterprise Applications",
                locations=(("", "", "US"),),
                description="We currently offer remote work in approved states.",
            ),
            requisition(284, "Mortgage Advisor", locations=(("Allentown", "PA", "US"),)),
            requisition(283, "Mortgage Advisor", locations=(("Grand Rapids", "MI", "US"),)),
            requisition(200, "General Employment Application", locations=()),
        ]

    def collect_fixture(self, pages: dict[int, dict], calls: list[tuple[str, int]] | None = None):
        collector = CSODCollector(delay_seconds=0)
        recorded_calls = calls if calls is not None else []
        with (
            patch("collectors.csod_collector.CSOD_PAGE_SIZE", 2),
            patch.object(CSODCollector, "get", return_value=FakeResponse(BOARD_URL, bootstrap_html())),
            patch.object(CSODCollector, "fetch_search_page", new=page_router(pages, recorded_calls)),
        ):
            jobs = collector.collect(self.company)
        return collector, jobs

    def test_public_search_pages_normalize_filter_and_keep_distinct_repeated_titles(self) -> None:
        pages = {
            1: api_response(6, self.records[:2]),
            2: api_response(6, self.records[2:4]),
            3: api_response(6, self.records[4:]),
        }
        calls: list[tuple[str, int]] = []
        collector, jobs = self.collect_fixture(pages, calls)

        self.assertFalse(collector.requires_browser)
        self.assertEqual((collector.candidate_count, collector.rejected_count, collector.saved_count), (6, 1, 5))
        self.assertEqual([page for _url, page in calls], [1, 2, 3])
        self.assertTrue(all(url == SEARCH_URL for url, _page in calls))
        self.assertEqual(
            {job.title for job in jobs},
            {"Teller - Branch Float - Sparks", "Collector", "Manager of Enterprise Applications", "Mortgage Advisor"},
        )
        self.assertEqual(sum(job.title == "Mortgage Advisor" for job in jobs), 2)
        self.assertEqual((len({job.id for job in jobs}), len({job.sourceUrl for job in jobs})), (5, 5))
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))

        collector_job = next(job for job in jobs if job.title == "Collector")
        remote_job = next(job for job in jobs if job.title.startswith("Manager of Enterprise"))
        self.assertEqual((collector_job.location, collector_job.workType), ("Reno, NV, US", "Onsite"))
        self.assertEqual((remote_job.location, remote_job.workType), ("Remote", "Remote"))
        self.assertEqual(collector_job.postedDate, "8/25/2026")
        self.assertEqual(collector_job.rawData["requisitionId"], "303")
        self.assertTrue(collector_job.rawData["structuredSource"])
        self.assertEqual(
            collector_job.sourceUrl,
            "https://ufcu.csod.com/ux/ats/careersite/4/home/requisition/303?c=ufcu",
        )

    def test_duplicate_and_incomplete_pagination_fail_closed(self) -> None:
        cases = (
            (
                {
                    1: api_response(4, self.records[:2]),
                    2: api_response(4, [self.records[1], self.records[2]]),
                },
                "unique requisitions",
            ),
            (
                {1: api_response(3, self.records[:2]), 2: api_response(3, [])},
                "empty page",
            ),
            (
                {1: api_response(4, self.records[:2]), 2: api_response(3, self.records[2:3])},
                "total changed",
            ),
        )
        for pages, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.collect_fixture(pages)

    def test_explicit_zero_is_authoritative_but_missing_contract_fields_fail(self) -> None:
        collector, jobs = self.collect_fixture({1: api_response(0, [])})
        self.assertEqual(jobs, [])
        self.assertEqual(collector.candidate_count, 0)

        malformed = {"status": "Success", "data": {"requisitions": []}}
        with self.assertRaisesRegex(ValueError, "totalCount"):
            self.collect_fixture({1: malformed})
        failed = {"status": "Failed", "data": {"totalCount": 0, "requisitions": []}}
        with self.assertRaisesRegex(ValueError, "Success"):
            self.collect_fixture({1: failed})

    def test_board_bootstrap_detection_and_request_body_use_verified_contract(self) -> None:
        normalized, host, site_id, corp = normalize_csod_board_url(
            "https://ufcu.csod.com/ux/ats/careersite/4/home/requisition/305?tracking=x&c=ufcu"
        )
        self.assertEqual((normalized, host, site_id, corp), (BOARD_URL, "ufcu.csod.com", 4, "ufcu"))
        config = parse_csod_bootstrap(bootstrap_html(), BOARD_URL)
        self.assertEqual((config.site_id, config.corp, config.culture_id, config.culture_name), (4, "ufcu", 1, "en-US"))
        self.assertEqual(config.search_url, SEARCH_URL)
        self.assertEqual(
            build_csod_search_payload(config, 1),
            {
                "careerSiteId": 4,
                "careerSitePageId": 4,
                "pageNumber": 1,
                "pageSize": 25,
                "cultureId": 1,
                "searchText": "",
                "cultureName": "en-US",
                "states": [],
                "countryCodes": [],
                "cities": [],
                "placeID": "",
                "radius": None,
                "postingsWithinDays": None,
                "customFieldCheckboxKeys": [],
                "customFieldDropdowns": [],
                "customFieldRadios": [],
            },
        )
        self.assertEqual(detect_job_platform(BOARD_URL), "Cornerstone")
        selected = pick_collector(self.company, delay_seconds=0)
        self.assertIsInstance(selected, CSODCollector)
        self.assertFalse(selected.requires_browser)

    def test_url_and_bootstrap_validation_reject_untrusted_hosts_and_tenant_mismatches(self) -> None:
        for unsafe in (
            "http://ufcu.csod.com/ux/ats/careersite/4/home?c=ufcu",
            "https://ufcu.csod.com.attacker.example/ux/ats/careersite/4/home?c=ufcu",
            "https://ufcu.csod.com/ux/ats/careersite/4/home",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(ValueError):
                    normalize_csod_board_url(unsafe)
        with self.assertRaisesRegex(ValueError, "tenant did not match"):
            parse_csod_bootstrap(bootstrap_html(corp="attacker"), BOARD_URL)
        with self.assertRaisesRegex(ValueError, "unsupported cloud API origin"):
            parse_csod_bootstrap(bootstrap_html(cloud="https://api.csod.com.attacker.example/"), BOARD_URL)

    def test_complete_flow_persists_deduplicated_jobs_and_http_diagnostics(self) -> None:
        pages = {
            1: api_response(6, self.records[:2]),
            2: api_response(6, self.records[2:4]),
            3: api_response(6, self.records[4:]),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("collectors.csod_collector.CSOD_PAGE_SIZE", 2),
                patch.object(CSODCollector, "get", return_value=FakeResponse(BOARD_URL, bootstrap_html())),
                patch.object(CSODCollector, "fetch_search_page", new=page_router(pages, [])),
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
            diagnostic = json.loads(
                (root / "logs" / "job_collection_diagnostics.json").read_text(encoding="utf-8")
            )[0]
            rejected = json.loads(
                (root / "logs" / "rejected_job_candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual((summary["jobs_found"], summary["errors"]), (5, 0))
            self.assertEqual((len(payload), len({job["id"] for job in payload})), (5, 5))
            self.assertEqual(
                (
                    diagnostic["collectorSelected"],
                    diagnostic["playwrightUsed"],
                    diagnostic["validJobsSaved"],
                    diagnostic["status"],
                ),
                ("CSODCollector", False, 5, "Jobs Collected"),
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
                        "jobPlatform": "Cornerstone",
                        "jobBoardDiscoveryMethod": "Verified official careers redirect",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.replace_raw_candidates(rejected, company_ids={self.company["Company ID"]})
            self.assertEqual(len(repository.list_jobs()), 5)
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
