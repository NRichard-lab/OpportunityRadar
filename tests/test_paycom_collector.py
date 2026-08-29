from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.paycom_collector import PaycomCollector, normalize_paycom_board_url, parse_paycom_bootstrap
from job_tools import collect_jobs


BOARD_URL = (
    "https://www.paycomonline.net/v4/ats/web.php/portal/"
    "e426e81a21036f064e1806295bd2db4a/career-page"
)
API_BASE = "https://portal-applicant-tracking.us-cent.paycomonline.net/"


class BootstrapResponse:
    text = (
        '<script>var configsFromHost = {"sessionJWT":"public-token",'
        '"libConfig":"{\\"atsPortalMantleServiceUrl\\":\\"'
        + API_BASE.replace("/", "\\/")
        + '\\"}"}; var Mountable = {};</script>'
    )


def preview(job_id: int, title: str) -> dict:
    return {
        "jobId": job_id,
        "jobTitle": title,
        "positionType": "Full Time",
        "remoteType": "",
        "locations": "ELKHART, IN 46516",
        "description": f"Current opening for {title}.",
        "postedOn": "",
    }


def detail(job_id: int, title: str) -> dict:
    return {
        "jobPosting": {
            "jobId": job_id,
            "clientCode": "16N45",
            "jobTitle": title,
            "location": "ELKHART, IN 46516",
            "secondaryLocations": [],
            "remoteType": "",
            "salaryRange": "$20.00 - $25.00 Hourly",
            "positionType": "Full Time",
            "description": f"<p>In-person current opening for {title}.</p>",
            "qualifications": "<p>Customer service experience.</p>",
            "googleJobJson": json.dumps(
                {
                    "@type": "JobPosting",
                    "identifier": f"J16N45{job_id}",
                    "datePosted": "2026-08-28",
                }
            ),
        }
    }


def search_router(pages: dict[int, dict], calls: list[int]):
    def fake_search(_collector: PaycomCollector, _url: str, _token: str, skip: int) -> dict:
        calls.append(skip)
        return pages[skip]

    return fake_search


def detail_router(details: dict[str, dict]):
    def fake_detail(_collector: PaycomCollector, _base: str, _token: str, job_id: str) -> dict:
        return details[job_id]

    return fake_detail


class PaycomCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-inovafederal-org",
            "Company Name": "INOVA Federal Credit Union",
            "Official Website": "https://www.inovafederal.org/",
            "Careers Page URL": "https://www.inovafederal.org/about-us/careers.html",
            "Job Board URL": BOARD_URL,
            "Job Platform": "",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Failed",
        }
        self.records = [
            preview(100, "Marketing Coordinator"),
            preview(101, "Collector"),
            preview(102, "Financial Solutions Specialist"),
            preview(103, "General Employment Application"),
        ]
        self.details = {
            str(record["jobId"]): detail(record["jobId"], record["jobTitle"])
            for record in self.records
        }

    def collect(self, pages: dict[int, dict], calls: list[int] | None = None):
        calls = calls if calls is not None else []
        collector = PaycomCollector(delay_seconds=0)
        with (
            patch.object(PaycomCollector, "get", return_value=BootstrapResponse()),
            patch.object(PaycomCollector, "fetch_search_page", new=search_router(pages, calls)),
            patch.object(PaycomCollector, "fetch_detail", new=detail_router(self.details)),
            patch("collectors.paycom_collector.PAYCOM_PAGE_SIZE", 2),
        ):
            jobs = collector.collect(self.company)
        return collector, jobs

    def test_public_api_paginates_normalizes_and_keeps_legitimate_short_titles(self) -> None:
        pages = {
            0: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": self.records[:2]},
            2: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": self.records[2:]},
        }
        calls: list[int] = []
        collector, jobs = self.collect(pages, calls)

        self.assertFalse(collector.requires_browser)
        self.assertEqual((collector.candidate_count, collector.rejected_count, collector.saved_count), (4, 1, 3))
        self.assertEqual(calls, [0, 2])
        self.assertEqual(
            {job.title for job in jobs},
            {"Marketing Coordinator", "Collector", "Financial Solutions Specialist"},
        )
        self.assertEqual((len({job.id for job in jobs}), len({job.sourceUrl for job in jobs})), (3, 3))
        collector_job = next(job for job in jobs if job.title == "Collector")
        self.assertEqual(
            (collector_job.location, collector_job.workType, collector_job.postedDate),
            ("ELKHART, IN 46516", "Onsite", "2026-08-28"),
        )
        self.assertTrue(collector_job.rawData["structuredSource"])
        self.assertNotIn("public-token", json.dumps(collector_job.rawData))

    def test_duplicate_or_incomplete_pages_fail_closed(self) -> None:
        duplicate = {
            0: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": self.records[:2]},
            2: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": [self.records[1], self.records[2]]},
        }
        incomplete = {
            0: {"jobPostingPreviewsCount": 3, "jobPostingPreviews": self.records[:2]},
            2: {"jobPostingPreviewsCount": 3, "jobPostingPreviews": []},
        }
        with self.assertRaisesRegex(ValueError, "unique postings"):
            self.collect(duplicate)
        with self.assertRaisesRegex(ValueError, "empty page"):
            self.collect(incomplete)

    def test_explicit_zero_is_valid_but_missing_total_is_not(self) -> None:
        _collector, jobs = self.collect(
            {0: {"jobPostingPreviewsCount": 0, "jobPostingPreviews": []}}
        )
        self.assertEqual(jobs, [])
        with self.assertRaisesRegex(ValueError, "jobPostingPreviewsCount"):
            self.collect({0: {"jobPostingPreviews": []}})

    def test_board_and_bootstrap_contracts_are_strict(self) -> None:
        self.assertEqual(normalize_paycom_board_url(BOARD_URL + "?ignored=1"), BOARD_URL)
        self.assertIsInstance(pick_collector(self.company, delay_seconds=0), PaycomCollector)
        token, api_base = parse_paycom_bootstrap(BootstrapResponse.text)
        self.assertEqual((token, api_base), ("public-token", API_BASE))
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            normalize_paycom_board_url(
                "https://attacker.example/v4/ats/web.php/portal/"
                "e426e81a21036f064e1806295bd2db4a/career-page"
            )
        poisoned = BootstrapResponse.text.replace(
            "portal-applicant-tracking.us-cent.paycomonline.net",
            "attacker.example",
        )
        with self.assertRaisesRegex(ValueError, "unsupported API origin"):
            parse_paycom_bootstrap(poisoned)

    def test_complete_flow_persists_deduplicated_jobs_and_diagnostics(self) -> None:
        pages = {
            0: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": self.records[:2]},
            2: {"jobPostingPreviewsCount": 4, "jobPostingPreviews": self.records[2:]},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch.object(PaycomCollector, "get", return_value=BootstrapResponse()),
                patch.object(PaycomCollector, "fetch_search_page", new=search_router(pages, [])),
                patch.object(PaycomCollector, "fetch_detail", new=detail_router(self.details)),
                patch("collectors.paycom_collector.PAYCOM_PAGE_SIZE", 2),
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
            self.assertEqual((summary["jobs_found"], summary["errors"], len(payload)), (3, 0, 3))
            self.assertEqual(
                (
                    diagnostic["collectorSelected"],
                    diagnostic["playwrightUsed"],
                    diagnostic["validJobsSaved"],
                    diagnostic["status"],
                ),
                ("PaycomCollector", False, 3, "Jobs Collected"),
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
                        "jobPlatform": "Paycom",
                        "jobBoardDiscoveryMethod": "Verified embedded board",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.replace_raw_candidates(rejected, company_ids={self.company["Company ID"]})
            self.assertEqual(len(repository.list_jobs()), 3)
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
