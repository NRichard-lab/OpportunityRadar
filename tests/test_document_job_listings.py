from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.generic_collector import GenericCollector
from job_tools import collect_jobs


BOARD_URL = "https://www.khalsacreditunion.ca/about/careers"


LISTING_HTML = """
<main>
  <div class="career-card">
    <h5>Lower Mainland</h5>
    <h5>Sales Enablement Coach</h5>
    <a href="/assets/pdfs/Job Posting - Sales Enablement Coach .pdf">Learn more</a>
  </div>
  <div class="career-card">
    <h5>Lower Mainland</h5>
    <h5>Data &amp; Reporting Analyst</h5>
    <a href="/assets/pdfs/Job Description-Data and Reporting Analyst.pdf">Read more</a>
  </div>
  <div class="career-card">
    <h5>128th Street Branch</h5>
    <h5>Senior Advisor (12 Month Contract)</h5>
    <a href="/assets/pdfs/Senior Advisor - Job Description .pdf">View details</a>
  </div>
</main>
"""


class FakeResponse:
    url = BOARD_URL
    text = LISTING_HTML


class DocumentJobListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-khalsacreditunion-ca",
            "Company Name": "Khalsa Credit Union - Surrey",
            "Official Website": "https://www.khalsacreditunion.ca/",
            "Careers Page URL": BOARD_URL,
            "Job Board URL": BOARD_URL,
            "Job Platform": "Generic",
        }

    def test_official_pdf_cards_use_nearest_heading_and_pdf_destination(self) -> None:
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(
            self.company,
            BOARD_URL,
            BOARD_URL,
            LISTING_HTML,
            "Job Board URL",
        )

        self.assertEqual(
            [(job.title, job.location) for job in jobs],
            [
                ("Sales Enablement Coach", "Lower Mainland"),
                ("Data & Reporting Analyst", "Lower Mainland"),
                ("Senior Advisor (12 Month Contract)", "128th Street Branch"),
            ],
        )
        self.assertEqual(len({job.sourceUrl for job in jobs}), 3)
        self.assertTrue(all(job.sourceUrl.lower().endswith(".pdf") for job in jobs))
        self.assertEqual(collector.saved_count, 3)
        self.assertEqual(collector.rejected_count, 0)

    def test_complete_flow_falls_back_to_http_deduplicates_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("job_tools.APP_ENABLE_BROWSER_JOBS", True),
                patch("job_tools.APP_MAX_BROWSER_WORKERS", 1),
                patch.object(GenericCollector, "collect_with_browser", side_effect=RuntimeError("browser unavailable")),
                patch.object(GenericCollector, "get", return_value=FakeResponse()),
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
            self.assertEqual(
                (summary["jobs_found"], summary["jobs_saved"], summary["errors"]),
                (3, 3, 0),
                (summary, diagnostics),
            )
            self.assertEqual((len(payload), len({job["sourceUrl"] for job in payload})), (3, 3))
            self.assertIsInstance(pick_collector(self.company), GenericCollector)
            self.assertEqual(
                (
                    diagnostics[0]["collectorSelected"],
                    diagnostics[0]["validJobsSaved"],
                    diagnostics[0]["status"],
                    diagnostics[0]["outcome"],
                ),
                ("GenericCollector", 3, "Jobs Collected", "success"),
            )

            repository = OpportunityRepository(root / "radar.db", initialize=True)
            repository.upsert_company_snapshots(
                [
                    {
                        "id": self.company["Company ID"],
                        "name": self.company["Company Name"],
                        "officialWebsite": self.company["Official Website"],
                        "careersPageUrl": BOARD_URL,
                        "jobBoardUrl": BOARD_URL,
                        "jobPlatform": "Generic",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            stored = repository.list_jobs()
            self.assertEqual((len(stored), len({job["sourceUrl"] for job in stored})), (3, 3))

    def test_browser_is_closed_when_context_or_page_setup_fails(self) -> None:
        for failure_point in ("context", "page"):
            with self.subTest(failure_point=failure_point):
                context = Mock()
                browser = Mock()
                if failure_point == "context":
                    browser.new_context.side_effect = RuntimeError("context setup failed")
                else:
                    context.new_page.side_effect = RuntimeError("page setup failed")
                    browser.new_context.return_value = context
                manager = MagicMock()
                manager.__enter__.return_value = Mock()
                collector = GenericCollector(delay_seconds=0)
                with (
                    patch("playwright.sync_api.sync_playwright", return_value=manager),
                    patch("collectors.generic_collector.launch_playwright_chromium", return_value=browser),
                    patch("collectors.generic_collector.install_playwright_url_guard"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "setup failed"):
                        collector.collect_with_browser(self.company, BOARD_URL, "Job Board URL")
                if failure_point == "page":
                    context.close.assert_called_once_with()
                browser.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
