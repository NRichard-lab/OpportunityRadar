from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.workday_collector import WorkdayCollector, build_workday_urls
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = "https://gatecitybank.wd5.myworkdayjobs.com/gatecitybankcareers"
API_BASE = "https://gatecitybank.wd5.myworkdayjobs.com/wday/cxs/gatecitybank/gatecitybankcareers"


GATE_CITY_OPENINGS = [
    ("REQ-2764", "Teller / Customer Service Representative - Grand Forks South Washington - Part-Time", "Grand Forks, ND", "2026-08-28", "Part time"),
    ("REQ-2763", "Mortgage Loan Officer", "Fargo, ND", "2026-08-25", "Full time"),
    ("REQ-2761", "Loan Servicing Representative - Full-Time", "Fargo, ND", "2026-08-24", "Full time"),
    ("REQ-2759", "Lending Solutions Specialist", "Fargo, ND", "2026-08-20", "Full time"),
    ("REQ-2753", "Card Operations and Servicing Specialist I - Part-Time (20-29 hrs/wk)", "Fargo, ND", "2026-08-20", "Part time"),
    ("REQ-2752", "Electronic Payments Specialist - Part-Time (20-29 hrs/wk)", "Fargo, ND", "2026-08-20", "Part time"),
    ("REQ-2757", "Teller / Customer Service Representative - Fargo Woodhaven - Full-Time", "Fargo, ND", "2026-08-19", "Full time"),
    ("REQ-2758", "Teller / Customer Service Representative - Fargo North - Part-Time", "Fargo, ND", "2026-08-19", "Part time"),
    ("REQ-2723", "Senior Creative Strategist", "Fargo, ND", "2026-08-17", "Full time"),
    ("REQ-2754", "Teller / Customer Service Representative - Minot Downtown - Part-Time", "Minot, ND", "2026-08-17", "Part time"),
    ("REQ-2748", "Accounts Payable Specialist", "Fargo, ND", "2026-08-12", "Full time"),
    ("REQ-2745", "Teller / Customer Service Representative - Waite Park - Part-Time", "Waite Park, MN", "2026-08-10", "Part time"),
    ("REQ-2712", "Teller / Customer Service Representative - Jamestown - Full-Time", "Jamestown, ND", "2026-07-20", "Full time"),
    ("REQ-2650", "VP/Manager of Insurance Agency", "Fargo, ND", "2026-07-01", "Full time"),
]


def make_posting(external_id: str, title: str, location: str, posted_on: str = "Posted Today") -> dict:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
    location_slug = re.sub(r"[^A-Za-z0-9]+", "-", location).strip("-")
    return {
        "title": title,
        "externalPath": f"/job/{location_slug}/{slug}_{external_id}",
        "locationsText": location,
        "postedOn": posted_on,
        "bulletFields": [external_id],
    }


def make_detail(posting: dict, start_date: str, time_type: str) -> dict:
    external_id = posting["bulletFields"][0]
    return {
        "title": posting["title"],
        "jobDescription": f"<div><p>{posting['title']}</p><p>Current public opening.</p></div>",
        "location": posting["locationsText"],
        "startDate": start_date,
        "timeType": time_type,
        "jobReqId": external_id,
        "jobPostingId": posting["externalPath"].rsplit("/", 1)[-1],
        "externalUrl": f"{BOARD_URL}{posting['externalPath']}",
    }


def page_router(pages: dict[int, dict], calls: list[tuple[str, int]]):
    def fake_fetch_page(_collector: WorkdayCollector, url: str, offset: int) -> dict:
        calls.append((url, offset))
        return pages[offset]

    return fake_fetch_page


def detail_router(details: dict[str, dict], calls: list[tuple[str, str]]):
    def fake_fetch_detail(_collector: WorkdayCollector, api_base: str, external_path: str) -> dict:
        calls.append((api_base, external_path))
        return details[external_path]

    return fake_fetch_detail


class WorkdayCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-gatecity-bank",
            "Company Name": "Gate City Bank",
            "Official Website": "https://www.gatecity.bank/",
            "Careers Page URL": "https://www.gatecity.bank/careers/",
            "Job Board URL": BOARD_URL,
            # Mirrors production: URL detection must win despite a blank platform field.
            "Job Platform": "",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Completed",
        }
        self.postings = [
            make_posting(external_id, title, location, "Posted Today" if index == 0 else "Posted 3 Days Ago")
            for index, (external_id, title, location, _date, _time_type) in enumerate(GATE_CITY_OPENINGS)
        ]
        self.details = {
            posting["externalPath"]: make_detail(posting, start_date, time_type)
            for posting, (_external_id, _title, _location, start_date, time_type) in zip(
                self.postings, GATE_CITY_OPENINGS, strict=True
            )
        }

    def workday_pages(self) -> dict[int, dict]:
        # Gate City's public endpoint reports total only on the first page; later
        # pages currently return zero while still returning their requested slice.
        return {
            0: {"total": 14, "jobPostings": self.postings[:5]},
            5: {"total": 0, "jobPostings": self.postings[5:10]},
            10: {"total": 0, "jobPostings": self.postings[10:]},
        }

    def test_public_cxs_api_paginates_normalizes_and_uses_stable_destinations(self) -> None:
        page_calls: list[tuple[str, int]] = []
        detail_calls: list[tuple[str, str]] = []
        collector = WorkdayCollector(delay_seconds=0)
        with (
            patch("collectors.workday_collector.WORKDAY_PAGE_SIZE", 5),
            patch.object(WorkdayCollector, "fetch_page", new=page_router(self.workday_pages(), page_calls)),
            patch.object(WorkdayCollector, "fetch_detail", new=detail_router(self.details, detail_calls)),
        ):
            jobs = collector.collect(self.company)

        self.assertFalse(collector.requires_browser)
        self.assertEqual((collector.candidate_count, collector.rejected_count, collector.saved_count), (14, 0, 14))
        self.assertEqual([offset for _url, offset in page_calls], [0, 5, 10])
        self.assertEqual({url for url, _offset in page_calls}, {f"{API_BASE}/jobs"})
        self.assertEqual({api_base for api_base, _path in detail_calls}, {API_BASE})
        self.assertEqual((len(jobs), len({job.id for job in jobs}), len({job.sourceUrl for job in jobs})), (14, 14, 14))
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))
        self.assertTrue(all(urlsplit(job.sourceUrl).hostname == "gatecitybank.wd5.myworkdayjobs.com" for job in jobs))
        mortgage = next(job for job in jobs if job.title == "Mortgage Loan Officer")
        self.assertEqual((mortgage.location, mortgage.postedDate), ("Fargo, ND", "2026-08-25"))
        self.assertEqual(mortgage.rawData["externalJobId"], "REQ-2763")
        self.assertEqual(mortgage.rawData["timeType"], "Full time")
        self.assertTrue(mortgage.rawData["structuredSource"])
        self.assertEqual(collector.final_url_after_redirect, BOARD_URL)

    def test_url_contract_detection_and_http_routing_do_not_require_chromium(self) -> None:
        board_url, api_base = build_workday_urls(f"{BOARD_URL}?source=official-careers")
        self.assertEqual((board_url, api_base), (BOARD_URL, API_BASE))
        localized_board, localized_api = build_workday_urls(
            "https://gatecitybank.wd5.myworkdayjobs.com/en-US/gatecitybankcareers"
        )
        self.assertEqual(localized_board, f"https://gatecitybank.wd5.myworkdayjobs.com/en-US/gatecitybankcareers")
        self.assertEqual(localized_api, API_BASE)
        selected = pick_collector(self.company, delay_seconds=0)
        self.assertIsInstance(selected, WorkdayCollector)
        self.assertFalse(selected.requires_browser)
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            build_workday_urls("https://attacker.example/gatecitybankcareers")
        with self.assertRaisesRegex(ValueError, "exactly one career site"):
            build_workday_urls(f"{BOARD_URL}/job/not-a-board")

    def test_filtering_and_detail_fallback_preserve_valid_public_listings(self) -> None:
        valid = self.postings[0]
        invalid = make_posting("REQ-9999", "General Employment Application", "Fargo, ND")
        pages = {0: {"total": 2, "jobPostings": [valid, invalid]}}

        def failed_detail(_collector: WorkdayCollector, _api_base: str, _external_path: str) -> dict:
            raise RuntimeError("detail unavailable")

        collector = WorkdayCollector(delay_seconds=0)
        with (
            patch.object(WorkdayCollector, "fetch_page", new=page_router(pages, [])),
            patch.object(WorkdayCollector, "fetch_detail", new=failed_detail),
        ):
            jobs = collector.collect(self.company)

        self.assertEqual([job.title for job in jobs], [valid["title"]])
        self.assertEqual(jobs[0].location, "Grand Forks, ND")
        self.assertEqual(jobs[0].postedDate, "Posted Today")
        self.assertFalse(jobs[0].rawData["detailRetrieved"])
        self.assertEqual(collector.rejected_count, 1)
        self.assertEqual(collector.rejection_samples[0]["text"], "General Employment Application")

    def test_duplicate_or_incomplete_result_sets_fail_closed_before_persistence(self) -> None:
        duplicate_pages = {
            0: {"total": 2, "jobPostings": [self.postings[0], dict(self.postings[0])]},
        }
        with (
            patch.object(WorkdayCollector, "fetch_page", new=page_router(duplicate_pages, [])),
            patch.object(WorkdayCollector, "fetch_detail", new=detail_router(self.details, [])),
        ):
            with self.assertRaisesRegex(ValueError, "unique postings"):
                WorkdayCollector(delay_seconds=0).collect(self.company)

        incomplete_pages = {
            0: {"total": 3, "jobPostings": self.postings[:2]},
            2: {"total": 0, "jobPostings": []},
        }
        with (
            patch("collectors.workday_collector.WORKDAY_PAGE_SIZE", 2),
            patch.object(WorkdayCollector, "fetch_page", new=page_router(incomplete_pages, [])),
            patch.object(WorkdayCollector, "fetch_detail", new=detail_router(self.details, [])),
        ):
            with self.assertRaisesRegex(ValueError, "empty page"):
                WorkdayCollector(delay_seconds=0).collect(self.company)

    def test_complete_flow_filters_deduplicates_persists_and_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("collectors.workday_collector.WORKDAY_PAGE_SIZE", 5),
                patch.object(WorkdayCollector, "fetch_page", new=page_router(self.workday_pages(), [])),
                patch.object(WorkdayCollector, "fetch_detail", new=detail_router(self.details, [])),
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
            self.assertEqual((summary["jobs_found"], summary["jobs_saved"], summary["errors"]), (14, 14, 0))
            self.assertEqual((len(payload), len({job["sourceUrl"] for job in payload})), (14, 14))
            self.assertEqual(rejected, [])
            self.assertEqual(
                (
                    diagnostics[0]["collectorSelected"],
                    diagnostics[0]["playwrightUsed"],
                    diagnostics[0]["validJobsSaved"],
                    diagnostics[0]["status"],
                    diagnostics[0]["outcome"],
                ),
                ("WorkdayCollector", False, 14, "Jobs Collected", "success"),
            )

            repository = OpportunityRepository(root / "radar.db", initialize=True)
            repository.upsert_company_snapshots(
                [
                    {
                        "id": self.company["Company ID"],
                        "name": self.company["Company Name"],
                        "officialWebsite": self.company["Official Website"],
                        "careersPageUrl": self.company["Careers Page URL"],
                        "jobBoardUrl": BOARD_URL,
                        "jobPlatform": "Workday",
                        "jobBoardDiscoveryMethod": "Verified official careers link",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.replace_raw_candidates(rejected, company_ids={self.company["Company ID"]})
            stored = repository.list_jobs()
            self.assertEqual((len(stored), len({job["sourceUrl"] for job in stored})), (14, 14))
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
