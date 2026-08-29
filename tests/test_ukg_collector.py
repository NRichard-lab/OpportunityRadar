from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.ukg_collector import UKGCollector, build_search_payload, build_ukg_urls
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = (
    "https://recruiting.ultipro.com/SPA1006SPCCU/JobBoard/"
    "a1ad5f09-7f9c-420c-9e77-4ace84ced6e0/?q=&o=postedDateDesc"
)


def opportunity(opportunity_id: str, title: str, *, location_type: int = 0) -> dict:
    return {
        "Id": opportunity_id,
        "Title": title,
        "RequisitionNumber": f"REQ-{opportunity_id}",
        "FullTime": True,
        "JobCategoryName": "Team Members",
        "Locations": [
            {
                "LocalizedDescription": "OPERATIONS CENTER",
                "Address": {
                    "City": "Melbourne",
                    "State": {"Code": "FL"},
                    "Country": {"Code": "USA"},
                },
            }
        ],
        "PostedDate": "2026-08-28T22:02:10.033Z",
        "BriefDescription": f"<p>Current opening for {title}.</p>",
        "JobLocationType": location_type,
    }


def response_router(pages: dict[int, dict], calls: list[tuple[str, int]]):
    def fake_fetch_page(_collector: UKGCollector, url: str, skip: int) -> dict:
        calls.append((url, skip))
        return pages[skip]

    return fake_fetch_page


class UKGCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-space-coast-credit-union-unknown",
            "Company Name": "Space Coast Credit Union",
            "Official Website": "https://www.sccu.com/",
            "Careers Page URL": "https://www.sccu.com/about-sccu/careers",
            "Job Board URL": BOARD_URL,
            # Mirrors production: URL detection must win despite a blank platform field.
            "Job Platform": "",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Needs Review",
        }
        self.records = [
            opportunity("00000000-0000-0000-0000-000000000001", "Branch Manager", location_type=0),
            opportunity("00000000-0000-0000-0000-000000000002", "Paralegal", location_type=1),
            opportunity("00000000-0000-0000-0000-000000000003", "Systems Engineer II", location_type=2),
            opportunity("00000000-0000-0000-0000-000000000004", "General Employment Application"),
        ]

    def test_public_endpoint_paginates_normalizes_filters_and_uses_stable_ids(self) -> None:
        pages = {
            0: {"totalCount": 4, "opportunities": self.records[:2], "locations": []},
            2: {"totalCount": 4, "opportunities": self.records[2:], "locations": []},
        }
        calls: list[tuple[str, int]] = []
        collector = UKGCollector(delay_seconds=0)
        with (
            patch("collectors.ukg_collector.UKG_PAGE_SIZE", 2),
            patch.object(UKGCollector, "fetch_page", new=response_router(pages, calls)),
        ):
            jobs = collector.collect(self.company)

        self.assertFalse(collector.requires_browser)
        self.assertEqual(collector.candidate_count, 4)
        self.assertEqual(collector.rejected_count, 1)
        self.assertEqual(collector.saved_count, 3)
        self.assertEqual([skip for _url, skip in calls], [0, 2])
        self.assertEqual(
            {job.title for job in jobs},
            {"Branch Manager", "Paralegal", "Systems Engineer II"},
        )
        self.assertEqual(len({job.id for job in jobs}), 3)
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))
        self.assertEqual(
            {parse_qs(urlsplit(job.sourceUrl).query)["opportunityId"][0] for job in jobs},
            {record["Id"] for record in self.records[:3]},
        )
        branch_manager = next(job for job in jobs if job.title == "Branch Manager")
        paralegal = next(job for job in jobs if job.title == "Paralegal")
        self.assertEqual(branch_manager.workType, "Hybrid")
        self.assertEqual((paralegal.workType, paralegal.location), ("Onsite", "OPERATIONS CENTER — Melbourne, FL"))
        self.assertTrue(paralegal.rawData["structuredSource"])

    def test_duplicate_id_fails_closed_instead_of_pruning_from_a_changed_result_set(self) -> None:
        pages = {
            0: {"totalCount": 4, "opportunities": self.records[:2]},
            2: {"totalCount": 4, "opportunities": [self.records[1], self.records[2]]},
        }
        collector = UKGCollector(delay_seconds=0)
        with (
            patch("collectors.ukg_collector.UKG_PAGE_SIZE", 2),
            patch.object(UKGCollector, "fetch_page", new=response_router(pages, [])),
        ):
            with self.assertRaisesRegex(ValueError, "unique opportunities"):
                collector.collect(self.company)
        self.assertIn("duplicate UKG opportunity ID", {item["reason"] for item in collector.rejection_samples})

    def test_incomplete_and_changing_pagination_fail_closed(self) -> None:
        incomplete = {
            0: {"totalCount": 3, "opportunities": self.records[:2]},
            2: {"totalCount": 3, "opportunities": []},
        }
        changing = {
            0: {"totalCount": 4, "opportunities": self.records[:2]},
            2: {"totalCount": 3, "opportunities": self.records[2:3]},
        }
        for pages, message in ((incomplete, "empty page"), (changing, "total changed")):
            with self.subTest(message=message):
                with (
                    patch("collectors.ukg_collector.UKG_PAGE_SIZE", 2),
                    patch.object(UKGCollector, "fetch_page", new=response_router(pages, [])),
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        UKGCollector(delay_seconds=0).collect(self.company)

    def test_empty_result_requires_an_explicit_zero_total(self) -> None:
        with patch.object(
            UKGCollector,
            "fetch_page",
            new=response_router({0: {"totalCount": 0, "opportunities": []}}, []),
        ):
            self.assertEqual(UKGCollector(delay_seconds=0).collect(self.company), [])
        with patch.object(
            UKGCollector,
            "fetch_page",
            new=response_router({0: {"opportunities": []}}, []),
        ):
            with self.assertRaisesRegex(ValueError, "totalCount"):
                UKGCollector(delay_seconds=0).collect(self.company)

    def test_url_contract_and_collector_detection_use_the_verified_board(self) -> None:
        board_url, search_url = build_ukg_urls(BOARD_URL)
        self.assertEqual(urlsplit(board_url).hostname, "recruiting.ultipro.com")
        self.assertTrue(search_url.endswith("/JobBoardView/LoadSearchResults"))
        self.assertEqual(build_search_payload(50)["opportunitySearch"]["Skip"], 50)
        self.assertIsInstance(pick_collector(self.company, delay_seconds=0), UKGCollector)
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            build_ukg_urls(
                "https://attacker.example/SPA1006SPCCU/JobBoard/"
                "a1ad5f09-7f9c-420c-9e77-4ace84ced6e0"
            )
        with self.assertRaisesRegex(ValueError, "JobBoard UUID"):
            build_ukg_urls("https://recruiting.ultipro.com/not-a-board")

    def test_complete_collection_flow_filters_deduplicates_persists_and_reports_diagnostics(self) -> None:
        pages = {
            0: {"totalCount": 4, "opportunities": self.records[:2], "locations": []},
            2: {"totalCount": 4, "opportunities": self.records[2:], "locations": []},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("collectors.ukg_collector.UKG_PAGE_SIZE", 2),
                patch.object(UKGCollector, "fetch_page", new=response_router(pages, [])),
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
            diagnostic = json.loads((root / "logs" / "job_collection_diagnostics.json").read_text(encoding="utf-8"))[0]
            rejected = json.loads((root / "logs" / "rejected_job_candidates.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["jobs_found"], summary["errors"]), (3, 0))
            self.assertEqual((len(payload), len({job["id"] for job in payload})), (3, 3))
            self.assertEqual(
                (
                    diagnostic["collectorSelected"],
                    diagnostic["playwrightUsed"],
                    diagnostic["validJobsSaved"],
                    diagnostic["status"],
                ),
                ("UKGCollector", False, 3, "Jobs Collected"),
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
                        "jobPlatform": "UKG",
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
