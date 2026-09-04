"""Regression tests for the fake-job imports produced by the generic parser.

Each case here corresponds to content that was previously stored as an active
job and had to be soft-closed: site navigation, header/footer links,
account-opening and branch/ATM promotions, login and contact links, policy
pages, social links, soft-404 copy, and raw contact addresses.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from collectors.generic_collector import GenericCollector
from job_evidence import (
    careers_page_context_reason,
    evaluate_generic_candidate,
    job_identifier_in_url,
    navigation_chrome_reason,
    non_job_destination_reason,
    non_job_text_reason,
    page_job_list_structure_reason,
    page_looks_like_soft_404,
)


FIXTURES = Path(__file__).parent / "fixtures" / "generic"
BOARD_URL = "https://www.example-community-bank.invalid/careers"

COMPANY = {
    "Company ID": "company-example-community-bank",
    "Company Name": "Example Community Bank",
    "Official Website": "https://www.example-community-bank.invalid/",
    "Careers Page URL": BOARD_URL,
    "Job Board URL": BOARD_URL,
    "Job Platform": "Generic",
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class GenericFakeJobRegressionTests(unittest.TestCase):
    """The known fake rows must not be recreated from their source page."""

    def setUp(self) -> None:
        self.collector = GenericCollector(delay_seconds=0)
        self.jobs = self.collector.parse_listing_html(
            COMPANY, BOARD_URL, BOARD_URL, fixture("community_bank_careers.html"), "Job Board URL"
        )

    def test_only_the_two_real_postings_are_collected(self) -> None:
        self.assertEqual(
            sorted(job.title for job in self.jobs),
            ["Loan Operations Specialist", "Universal Banker"],
        )

    def test_real_postings_keep_their_ats_application_urls(self) -> None:
        self.assertEqual(
            sorted(job.sourceUrl for job in self.jobs),
            [
                "https://exampleco.applicantpro.com/jobs/3481902",
                "https://exampleco.applicantpro.com/jobs/3481955",
            ],
        )

    def test_real_postings_record_the_evidence_that_admitted_them(self) -> None:
        for job in self.jobs:
            self.assertTrue(job.rawData.get("jobEvidence"), job.title)

    def test_no_chrome_or_marketing_content_becomes_a_job(self) -> None:
        collected = {job.title.casefold() for job in self.jobs}
        for forbidden in [
            "open an account",
            "find a branch/atm",
            "online banking login",
            "personal checking",
            "contact us",
            "privacy policy",
            "accessibility statement",
            "terms of use",
            "career opportunities",
            "careers",
            "facebook",
            "linkedin careers",
            "see all locations",
            "learn more",
            "info@example.invalid",
            "careers@example.invalid",
        ]:
            self.assertNotIn(forbidden, collected)

    def test_no_collected_job_points_at_a_non_posting_destination(self) -> None:
        for job in self.jobs:
            self.assertEqual(non_job_destination_reason(job.sourceUrl, page_url=BOARD_URL), "")


class SoftFourOhFourTests(unittest.TestCase):
    def test_placeholder_page_yields_no_jobs(self) -> None:
        collector = GenericCollector(delay_seconds=0)
        jobs = collector.parse_listing_html(
            COMPANY, BOARD_URL, BOARD_URL, fixture("soft_404_careers.html"), "Job Board URL"
        )
        self.assertEqual(jobs, [])

    def test_placeholder_copy_is_recognized(self) -> None:
        self.assertEqual(
            page_looks_like_soft_404("Oops. We're still building this path"),
            "still building this path",
        )
        self.assertEqual(page_looks_like_soft_404("Current Openings: Universal Banker"), "")

    def test_placeholder_title_is_never_a_job_title(self) -> None:
        self.assertTrue(non_job_text_reason("Oops. We’re still building this path"))

    def test_placeholder_zero_is_not_authoritative(self) -> None:
        collector = GenericCollector(delay_seconds=0)
        html = fixture("soft_404_careers.html")
        collector.parse_listing_html(COMPANY, BOARD_URL, BOARD_URL, html, "Job Board URL")
        self.assertIn(
            "error/placeholder page",
            collector.zero_result_uncertainty(BOARD_URL, html, "Oops. We're still building this path"),
        )


class ZeroResultAuthorityTests(unittest.TestCase):
    def test_explicit_no_openings_is_an_authoritative_zero(self) -> None:
        collector = GenericCollector(delay_seconds=0)
        html = fixture("no_openings_careers.html")
        jobs = collector.parse_listing_html(COMPANY, BOARD_URL, BOARD_URL, html, "Job Board URL")
        self.assertEqual(jobs, [])
        self.assertEqual(
            collector.zero_result_uncertainty(
                BOARD_URL, html, "Careers Current Openings We have no current openings at this time."
            ),
            "",
        )

    def test_unrecognized_empty_page_is_not_authoritative(self) -> None:
        collector = GenericCollector(delay_seconds=0)
        html = "<html><body><main><h1>Careers</h1><p>Thanks for your interest.</p></main></body></html>"
        collector.parse_listing_html(COMPANY, BOARD_URL, BOARD_URL, html, "Job Board URL")
        self.assertIn(
            "no recognizable job-list structure",
            collector.zero_result_uncertainty(BOARD_URL, html, "Careers Thanks for your interest."),
        )


class NavigationChromeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.soup = BeautifulSoup(fixture("community_bank_careers.html"), "html.parser")

    def test_header_nav_links_are_chrome(self) -> None:
        anchor = self.soup.select_one(".main-nav a[href='/open-an-account']")
        self.assertTrue(navigation_chrome_reason(anchor))

    def test_footer_links_are_chrome(self) -> None:
        anchor = self.soup.select_one(".footer-links a[href='/privacy-policy']")
        self.assertTrue(navigation_chrome_reason(anchor))

    def test_social_links_are_chrome(self) -> None:
        anchor = self.soup.select_one(".social-links a")
        self.assertTrue(navigation_chrome_reason(anchor))

    def test_job_rows_are_not_chrome(self) -> None:
        row = self.soup.select_one(".job-listing-row")
        self.assertEqual(navigation_chrome_reason(row), "")


class NonJobDestinationTests(unittest.TestCase):
    def test_rejected_destinations(self) -> None:
        cases = {
            "/open-an-account": "account/product application link",
            "/locations": "branch/ATM link",
            "/login": "login/registration link",
            "/contact-us": "contact/email link",
            "/privacy-policy": "privacy/legal/accessibility page link",
            "/accessibility": "privacy/legal/accessibility page link",
            "/personal/checking": "marketing/product page link",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(non_job_destination_reason(url, page_url=BOARD_URL), expected)

    def test_mailto_and_javascript_links_are_rejected(self) -> None:
        self.assertIn("mailto", non_job_destination_reason("mailto:careers@example.invalid"))
        self.assertIn("javascript", non_job_destination_reason("javascript:void(0)"))
        self.assertIn("anchor", non_job_destination_reason("#apply"))

    def test_social_and_aggregator_links_are_rejected(self) -> None:
        for url in [
            "https://www.facebook.com/exampleco",
            "https://www.linkedin.com/company/exampleco/jobs",
            "https://www.indeed.com/cmp/exampleco/jobs",
        ]:
            with self.subTest(url=url):
                self.assertTrue(non_job_destination_reason(url, page_url=BOARD_URL))

    def test_real_posting_urls_are_not_rejected(self) -> None:
        for url in [
            "https://exampleco.applicantpro.com/jobs/3481902",
            "https://www.example-community-bank.invalid/careers/universal-banker-1042",
            "https://exampleco.isolvedhire.com/jobs/551234.html",
        ]:
            with self.subTest(url=url):
                self.assertEqual(non_job_destination_reason(url, page_url=BOARD_URL), "")

    def test_careers_segment_outranks_a_marketing_segment(self) -> None:
        self.assertEqual(
            non_job_destination_reason(
                "https://www.example-community-bank.invalid/about/careers/commercial-lender-88",
                page_url=BOARD_URL,
            ),
            "",
        )


class NonJobTextTests(unittest.TestCase):
    def test_marketing_and_placeholder_titles_are_rejected(self) -> None:
        for text in [
            "Open an Account",
            "Find a Branch/ATM",
            "Online Banking",
            "Contact Us",
            "Enroll Now",
            "Oops. We're still building this path",
            "We're sorry, that page is unavailable",
            "careers@example.invalid",
            "(555) 010-0100",
            "https://www.example-community-bank.invalid/",
        ]:
            with self.subTest(text=text):
                self.assertTrue(non_job_text_reason(text), text)

    def test_real_titles_are_kept(self) -> None:
        for text in [
            "Universal Banker",
            "Loan Operations Specialist",
            "Vice President, Commercial Lending",
            "Teller - Part Time",
            "Senior Advisor (12 Month Contract)",
        ]:
            with self.subTest(text=text):
                self.assertEqual(non_job_text_reason(text), "", text)


class PositiveEvidenceTests(unittest.TestCase):
    def test_a_bare_link_list_without_evidence_is_refused(self) -> None:
        soup = BeautifulSoup(
            "<div class='content'><ul><li><a href='/about/leadership'>Meet Our Team</a></li></ul></div>",
            "html.parser",
        )
        verdict = evaluate_generic_candidate(
            title="Meet Our Team",
            href="https://www.example-community-bank.invalid/about/leadership",
            node=soup.select_one("li"),
            text="Meet Our Team",
            page_url=BOARD_URL,
        )
        self.assertFalse(verdict.accepted)

    def test_an_ats_job_detail_url_is_strong_evidence(self) -> None:
        verdict = evaluate_generic_candidate(
            title="Universal Banker",
            href="https://exampleco.applicantpro.com/jobs/3481902",
            node=None,
            text="Universal Banker",
            page_url=BOARD_URL,
        )
        self.assertTrue(verdict.accepted)
        self.assertIn("applicantpro.com job-detail URL", verdict.signals)

    def test_a_requisition_identifier_is_strong_evidence(self) -> None:
        self.assertEqual(
            job_identifier_in_url("https://www.example.invalid/careers/openings?jobId=REQ-2291"),
            "REQ-2291",
        )
        self.assertEqual(
            job_identifier_in_url("https://www.example.invalid/careers/universal-banker-1042"),
            "universal-banker-1042",
        )
        self.assertEqual(job_identifier_in_url("https://www.example.invalid/careers"), "")

    def test_job_list_structure_plus_metadata_is_accepted(self) -> None:
        soup = BeautifulSoup(fixture("community_bank_careers.html"), "html.parser")
        row = soup.select_one(".job-listing-row")
        verdict = evaluate_generic_candidate(
            title="Universal Banker",
            href="https://www.example-community-bank.invalid/careers/apply",
            node=row,
            text=row.get_text(" ", strip=True),
            page_url=BOARD_URL,
        )
        self.assertTrue(verdict.accepted)

    def test_page_job_list_structure_ignores_navigation_containers(self) -> None:
        soup = BeautifulSoup(
            "<nav class='job-nav-list'><a href='/careers'>Careers</a></nav>", "html.parser"
        )
        self.assertEqual(page_job_list_structure_reason(soup), "")


class CareersContextTests(unittest.TestCase):
    def test_careers_url_is_a_confirmed_context(self) -> None:
        self.assertTrue(careers_page_context_reason(BOARD_URL))

    def test_marketing_url_without_a_careers_title_is_not(self) -> None:
        soup = BeautifulSoup("<html><h1>Personal Banking</h1></html>", "html.parser")
        self.assertEqual(
            careers_page_context_reason("https://www.example.invalid/personal", soup), ""
        )

    def test_careers_heading_confirms_a_non_obvious_url(self) -> None:
        soup = BeautifulSoup("<html><h1>Join Our Team</h1></html>", "html.parser")
        self.assertTrue(careers_page_context_reason("https://www.example.invalid/hr", soup))


if __name__ == "__main__":
    unittest.main()
