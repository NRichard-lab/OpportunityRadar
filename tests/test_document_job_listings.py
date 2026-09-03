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
        # A browser failure makes the run non-authoritative: the HTTP fallback's
        # finds are still captured, deduplicated and persisted (additively), but
        # the refresh is reported as incomplete/partial and counted as an error
        # so a later authoritative run -- not this one -- decides removals.
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
                (3, 3, 1),
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
                    diagnostics[0]["dataDisposition"],
                ),
                ("GenericCollector", 3, "Collector Failed", "partial", "retained"),
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

    def test_json_ld_postings_use_matching_ats_links_when_url_is_omitted(self) -> None:
        html = """
        <main>
          <script type="application/ld+json">
          [
            {"@type":"JobPosting","title":"Risk Management Specialist","identifier":{"@type":"PropertyValue","name":"ApplicantPro","value":"4143005"},"description":"Current risk opening."},
            {"@type":"JobPosting","title":"Member Service Specialist","identifier":{"@type":"PropertyValue","name":"ApplicantPro","value":"4179322"},"description":"Current member service opening."}
          ]
          </script>
          <a href="https://example.applicantpro.com/jobs/4143005.html">More Information</a>
          <a href="https://example.applicantpro.com/jobs/4179322.html">More Information</a>
        </main>
        """
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(self.company, BOARD_URL, BOARD_URL, html, "Job Board URL")

        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            [job.sourceUrl for job in jobs],
            [
                "https://example.applicantpro.com/jobs/4143005.html",
                "https://example.applicantpro.com/jobs/4179322.html",
            ],
        )
        self.assertTrue(all(job.rawData["structuredSource"] for job in jobs))

    def test_current_openings_link_list_with_email_apply_is_collected(self) -> None:
        # Small-employer pattern: a bare list of links (each to a PDF job
        # description) under a "Current Openings" heading, apply by email.
        html = """
        <main><div class="content">
          <p>*We only accept resumes for positions that we are actively recruiting.</p>
          <h4>Current Openings</h4>
          <p><em><a href="/wp-content/uploads/Branch-Manager-Main-Office.pdf">Branch Manager – Main Office Branch</a></em></p>
          <p><em><a href="/wp-content/uploads/Teller-Pocomoke.pdf">Teller – Pocomoke City Branch</a></em></p>
          <p><em><a href="/wp-content/uploads/Rotating-Teller-Ocean-Pines.pdf">Rotating Teller – Ocean Pines Branch</a></em></p>
          <p>To apply, please forward your resume and cover letter to
             <a href="mailto:HRops@example.com">HRops@example.com</a>.</p>
          <p>First Shore Federal is proud to be an Equal Opportunity Employer.</p>
        </div></main>
        """
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(self.company, BOARD_URL, BOARD_URL, html, "Job Board URL")
        self.assertEqual(
            [job.title for job in jobs],
            [
                "Branch Manager – Main Office Branch",
                "Teller – Pocomoke City Branch",
                "Rotating Teller – Ocean Pines Branch",
            ],
        )
        self.assertTrue(all(job.sourceUrl.lower().endswith(".pdf") for job in jobs))
        self.assertTrue(all(job.rawData["officialJobDocument"] for job in jobs))
        self.assertTrue(all(job.rawData["sectionHeading"] == "Current Openings" for job in jobs))
        self.assertEqual(collector.saved_count, 3)

    def test_openings_heading_without_apply_signal_is_not_collected(self) -> None:
        html = """
        <main><div><h3>Open Positions</h3>
          <p><a href="/about/team">Meet the team</a></p>
          <p><a href="/branches">Our branches</a></p>
        </div></main>
        """
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(self.company, BOARD_URL, BOARD_URL, html, "Job Board URL")
        self.assertEqual(jobs, [])

    def test_static_available_positions_are_split_into_real_jobs(self) -> None:
        html = """
        <main><div class="wpb_wrapper">
          <h2>Available Positions</h2>
          <h2>Full-Time Teller – Broken Arrow Location</h2>
          <h4>Pay: $15.00 to $16.25 per hour</h4>
          <p>We have an immediate opening for a full-time teller.</p>
          <p>Responsibilities include member transactions and service.</p>
          <h2>Mortgage Lending Coordinator – Broken Arrow Location</h2>
          <h4>Salary is negotiable</h4>
          <p>We have an immediate opening for a Mortgage Lending Coordinator.</p>
          <p>Qualifications include two years of related experience.</p>
        </div></main>
        """
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(self.company, BOARD_URL, BOARD_URL, html, "Job Board URL")

        self.assertEqual(
            [(job.title, job.location) for job in jobs],
            [
                ("Full-Time Teller", "Broken Arrow Location"),
                ("Mortgage Lending Coordinator", "Broken Arrow Location"),
            ],
        )
        self.assertEqual(len({job.sourceUrl for job in jobs}), 2)
        self.assertTrue(all("#position-" in job.sourceUrl for job in jobs))


if __name__ == "__main__":
    unittest.main()
