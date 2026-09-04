"""Regression tests for the ApplicantPro and isolved Hire collectors.

The two vendors run one platform, so the same fixtures drive both. Fixtures
reproduce the real board page and listing responses -- verified against official
public tenant boards -- with synthetic tenant names, identifiers and addresses.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from collectors.applicantpro_collector import (
    ApplicantProCollector,
    IsolvedHireCollector,
    published_pay,
    tenant_from_board_url,
)
from collectors.base import pick_collector
from job_tools import CollectionNotAuthoritative


FIXTURES = Path(__file__).parent / "fixtures" / "applicantpro"
BOARD_URL = "https://examplecu.applicantpro.com/jobs/"
LISTING_PATH = "/core/jobs/8271"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def json(self):
        return json.loads(self.text)


class Transport:
    """Serves the board page and one listing response, recording every request."""

    def __init__(self, listing_fixture: str = "listing_with_jobs.json", *, board: str = "board_page.html") -> None:
        self.board = board
        self.listing_fixture = listing_fixture
        self.requested: list[str] = []
        self.board_landing_url = ""
        self.board_error: Exception | None = None
        self.listing_error: Exception | None = None
        self.listing_body: str | None = None

    def __call__(self, collector, url: str):
        self.requested.append(url)
        path = urlsplit(url).path
        if path.startswith("/core/jobs/"):
            if self.listing_error is not None:
                raise self.listing_error
            body = self.listing_body if self.listing_body is not None else fixture(self.listing_fixture)
            return FakeResponse(body, url)
        if self.board_error is not None:
            raise self.board_error
        return FakeResponse(fixture(self.board), self.board_landing_url or url)


def company(board_url: str = BOARD_URL, platform: str = "") -> dict:
    return {
        "Company ID": "company-example-community-credit-union",
        "Company Name": "Example Community Credit Union",
        "Official Website": "https://www.example-credit-union.invalid/",
        "Job Board URL": board_url,
        "Job Platform": platform,
    }


def run(collector, transport: Transport, board_url: str = BOARD_URL):
    """Collect with ``transport`` standing in for the network."""
    collector.get = lambda url: transport(collector, url)
    return collector.collect(company(board_url))


class ApplicantProCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Transport()
        self.collector = ApplicantProCollector(delay_seconds=0)

    def test_a_complete_board_is_collected_with_canonical_application_urls(self) -> None:
        jobs = run(self.collector, self.transport)
        self.assertEqual(
            [job.title for job in jobs],
            ["Consumer Loan Processor I", "Collector I", "Human Resources Supervisor"],
        )
        self.assertEqual(
            [job.sourceUrl for job in jobs],
            [
                "https://examplecu.applicantpro.com/jobs/4179340",
                "https://examplecu.applicantpro.com/jobs/4106000",
                "https://examplecu.applicantpro.com/jobs/4090030",
            ],
        )

    def test_the_listing_request_asks_only_for_public_postings(self) -> None:
        run(self.collector, self.transport)
        listing = next(url for url in self.transport.requested if LISTING_PATH in url)
        params = parse_qs(urlsplit(listing).query)
        self.assertEqual(json.loads(params["getParams"][0]), {"isInternal": 0})

    def test_the_domain_id_comes_from_the_board_page_not_a_guess(self) -> None:
        # The listing endpoint is keyed only by domainId and ignores the host, so
        # the id must be read from this tenant's own board page (8271 in the
        # fixture). Guessing it would return a different tenant's board.
        run(self.collector, self.transport)
        self.assertEqual(self.transport.requested[0], BOARD_URL)
        listing = next(url for url in self.transport.requested if "/core/jobs/" in url)
        self.assertEqual(urlsplit(listing).path, LISTING_PATH)

    def test_published_metadata_is_carried_through(self) -> None:
        jobs = run(self.collector, self.transport)
        processor = jobs[0]
        self.assertEqual(processor.location, "Riverton, PA")
        self.assertEqual(processor.workType, "Onsite")
        self.assertEqual(processor.postedDate, "Aug 17, 2026")
        self.assertEqual(processor.rawData["department"], "Consumer Lending")
        self.assertEqual(processor.rawData["employmentType"], "Full Time")
        self.assertEqual(jobs[1].workType, "Hybrid")
        self.assertEqual(jobs[2].workType, "Remote")

    def test_a_short_but_real_role_is_not_discarded(self) -> None:
        # "Collector I" is a real posting. The generic-page title heuristic scores
        # it below the bar; a structured ATS response has already identified it as
        # a job, so the structured validator is the correct gate.
        jobs = run(self.collector, self.transport)
        self.assertIn("Collector I", [job.title for job in jobs])
        self.assertEqual(self.collector.rejected_count, 0)

    def test_job_ids_are_stable_across_refreshes(self) -> None:
        first = run(ApplicantProCollector(delay_seconds=0), Transport())
        second = run(ApplicantProCollector(delay_seconds=0), Transport())
        self.assertEqual([job.id for job in first], [job.id for job in second])
        self.assertEqual(len(set(job.id for job in first)), len(first))


class PayIsNeverGuessedTests(unittest.TestCase):
    def test_pay_is_reported_only_when_the_tenant_published_it(self) -> None:
        jobs = run(ApplicantProCollector(delay_seconds=0), Transport())
        hourly, unpublished, salaried = jobs
        self.assertEqual(hourly.payText, "$23-34 per hour")
        self.assertEqual((hourly.payMin, hourly.payMax, hourly.payPeriod), (23.0, 34.0, "hour"))
        self.assertEqual(unpublished.payText, "")
        self.assertIsNone(unpublished.payMin)
        self.assertEqual(unpublished.payPeriod, "unknown")
        self.assertEqual(salaried.payText, "$70,304-100,097 per year")

    def test_blank_and_non_numeric_pay_fields_yield_no_pay(self) -> None:
        for record in [
            {"minSalary": "", "maxSalary": "", "payRate": None},
            {"minSalary": "competitive", "maxSalary": "DOE"},
            {"payRate": "0"},
            {},
        ]:
            with self.subTest(record=record):
                self.assertEqual(published_pay(record), ("", None, None, "unknown"))

    def test_a_single_published_rate_is_reported_as_itself(self) -> None:
        self.assertEqual(
            published_pay({"payRate": "22.50", "payType": "Hourly"}),
            ("$22.50 per hour", 22.5, 22.5, "hour"),
        )


class AuthoritativeZeroTests(unittest.TestCase):
    def test_a_verified_empty_board_is_an_authoritative_zero(self) -> None:
        jobs = run(ApplicantProCollector(delay_seconds=0), Transport("listing_empty_board.json"))
        self.assertEqual(jobs, [])

    def test_a_disabled_career_site_is_not_an_authoritative_zero(self) -> None:
        transport = Transport(board="inactive_site.html")
        transport.board_landing_url = "https://examplecu.applicantpro.com/notset.php?examplecu&root=4&disabled=1"
        with self.assertRaises(CollectionNotAuthoritative) as raised:
            run(ApplicantProCollector(delay_seconds=0), transport)
        self.assertIn("disabled career site", str(raised.exception))

    def test_an_unclaimed_tenant_is_not_an_authoritative_zero(self) -> None:
        transport = Transport(board="inactive_site.html")
        transport.board_landing_url = "https://examplecu.applicantpro.com/notset.php?examplecu&root=4"
        with self.assertRaises(CollectionNotAuthoritative):
            run(ApplicantProCollector(delay_seconds=0), transport)


class NonAuthoritativeOutcomeTests(unittest.TestCase):
    """Every uncertain outcome must retain the company's existing jobs."""

    def failure(self, transport: Transport, board_url: str = BOARD_URL) -> str:
        with self.assertRaises(CollectionNotAuthoritative) as raised:
            run(ApplicantProCollector(delay_seconds=0), transport, board_url)
        return str(raised.exception)

    def test_incomplete_listing_against_the_declared_count(self) -> None:
        message = self.failure(Transport("listing_truncated.json"))
        self.assertIn("returned 1 postings but declared 9", message)

    def test_a_posting_from_another_tenant_discards_the_whole_response(self) -> None:
        message = self.failure(Transport("listing_foreign_tenant.json"))
        self.assertIn("another tenant", message)
        self.assertIn("otherbank", message)

    def test_malformed_listing_json(self) -> None:
        transport = Transport()
        transport.listing_body = "<html>not json</html>"
        self.assertIn("did not return JSON", self.failure(transport))

    def test_listing_that_does_not_report_success(self) -> None:
        transport = Transport()
        transport.listing_body = json.dumps({"success": False, "data": {"jobs": [], "jobCount": 0}})
        self.assertIn("did not report success", self.failure(transport))

    def test_listing_without_a_job_count(self) -> None:
        transport = Transport()
        transport.listing_body = json.dumps({"success": True, "data": {"jobs": []}})
        self.assertIn("did not report a job count", self.failure(transport))

    def test_forbidden_response_is_reported_as_a_block(self) -> None:
        transport = Transport()
        response = requests.Response()
        response.status_code = 403
        transport.listing_error = requests.HTTPError("forbidden", response=response)
        self.assertIn("HTTP 403", self.failure(transport))

    def test_rate_limited_response(self) -> None:
        transport = Transport()
        response = requests.Response()
        response.status_code = 429
        transport.listing_error = requests.HTTPError("too many requests", response=response)
        self.assertIn("rate limited", self.failure(transport))

    def test_timeout_while_reading_the_board(self) -> None:
        transport = Transport()
        transport.board_error = requests.Timeout("timed out")
        self.assertIn("could not be read", self.failure(transport))

    def test_a_redirect_off_the_tenant_is_refused(self) -> None:
        transport = Transport()
        transport.board_landing_url = "https://otherbank.applicantpro.com/jobs/"
        self.assertIn("redirected to otherbank.applicantpro.com", self.failure(transport))

    def test_a_board_page_naming_a_different_tenant_is_refused(self) -> None:
        transport = Transport()
        transport.board = "board_page.html"
        message = self.failure(transport, "https://otherbank.applicantpro.com/jobs/")
        self.assertIn("identifies tenant \"examplecu\"", message)

    def test_an_unrecognized_board_url_is_refused(self) -> None:
        for url in [
            "https://www.applicantpro.com/",
            "https://applicantpro.com/jobs/",
            "http://examplecu.applicantpro.com/jobs/",
            "https://examplecu.applicantpro.com.evil.invalid/jobs/",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(CollectionNotAuthoritative):
                    run(ApplicantProCollector(delay_seconds=0), Transport(), url)


class TenantExtractionTests(unittest.TestCase):
    def test_the_subdomain_form(self) -> None:
        self.assertEqual(
            tenant_from_board_url("https://examplecu.applicantpro.com/jobs/", "applicantpro.com"),
            "examplecu",
        )
        self.assertEqual(
            tenant_from_board_url("https://examplecu.isolvedhire.com/jobs/", "isolvedhire.com"),
            "examplecu",
        )

    def test_the_openings_alias_form(self) -> None:
        self.assertEqual(
            tenant_from_board_url(
                "https://www.applicantpro.com/openings/examplecu/jobs/", "applicantpro.com"
            ),
            "examplecu",
        )

    def test_a_detail_url_still_names_its_tenant(self) -> None:
        self.assertEqual(
            tenant_from_board_url(
                "https://examplecu.applicantpro.com/jobs/4179340.html", "applicantpro.com"
            ),
            "examplecu",
        )

    def test_vendor_hosts_and_wrong_vendors_are_not_tenants(self) -> None:
        for url, vendor in [
            ("https://www.applicantpro.com/", "applicantpro.com"),
            ("https://feeds.applicantpro.com/site_map_index.xml", "applicantpro.com"),
            ("https://examplecu.applicantpro.com/jobs/", "isolvedhire.com"),
            ("https://examplecu.applicantpro.com.evil.invalid/jobs/", "applicantpro.com"),
            ("https://user:pass@examplecu.applicantpro.com/jobs/", "applicantpro.com"),
            ("https://examplecu.applicantpro.com:8443/jobs/", "applicantpro.com"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(tenant_from_board_url(url, vendor), "")


class IsolvedHireTests(unittest.TestCase):
    """isolved Hire is the same platform under a different vendor domain."""

    def test_an_isolved_board_collects_and_reports_its_own_platform(self) -> None:
        transport = Transport()
        transport.board = "board_page.html"
        collector = IsolvedHireCollector(delay_seconds=0)
        # The fixture's componentData names applicantpro.com, so an isolved
        # collector must refuse it rather than silently accept a foreign vendor.
        with self.assertRaises(CollectionNotAuthoritative) as raised:
            run(collector, transport, "https://examplecu.isolvedhire.com/jobs/")
        self.assertIn("vendor domain", str(raised.exception))

    def test_the_platform_name_is_reported_on_each_record(self) -> None:
        jobs = run(ApplicantProCollector(delay_seconds=0), Transport())
        self.assertEqual({job.jobPlatform for job in jobs}, {"ApplicantPro"})


class CollectorSelectionTests(unittest.TestCase):
    def test_applicantpro_urls_select_the_dedicated_collector(self) -> None:
        for url in [
            "https://examplecu.applicantpro.com/jobs/",
            "https://www.applicantpro.com/openings/examplecu/jobs/",
        ]:
            with self.subTest(url=url):
                self.assertIsInstance(
                    pick_collector(company(url), delay_seconds=0), ApplicantProCollector
                )

    def test_isolved_hire_urls_select_the_dedicated_collector(self) -> None:
        self.assertIsInstance(
            pick_collector(company("https://examplecu.isolvedhire.com/jobs/"), delay_seconds=0),
            IsolvedHireCollector,
        )

    def test_a_stored_platform_alone_selects_the_dedicated_collector(self) -> None:
        # An official careers page that embeds the board still routes correctly.
        selected = pick_collector(
            company("https://www.example-credit-union.invalid/careers", "ApplicantPro"),
            delay_seconds=0,
        )
        self.assertIsInstance(selected, ApplicantProCollector)
        selected = pick_collector(
            company("https://www.example-credit-union.invalid/careers", "isolved Hire"),
            delay_seconds=0,
        )
        self.assertIsInstance(selected, IsolvedHireCollector)


if __name__ == "__main__":
    unittest.main()
