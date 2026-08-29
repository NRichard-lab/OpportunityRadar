from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.clearcompany_collector import (
    ClearCompanyCollector,
    parse_hrmdirect_listing,
    validate_hrmdirect_board_url,
    validate_hrmdirect_detail_url,
)
from job_board_discovery import static_scan
from job_platforms import detect_job_platform
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = (
    "https://michedcu.hrmdirect.com/employment/"
    "job-openings.php?search=true"
)


class FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


def record(
    requisition_id: str,
    location_id: str,
    title: str,
    department: str,
) -> dict[str, str]:
    return {
        "requisitionId": requisition_id,
        "locationId": location_id,
        "title": title,
        "department": department,
        "city": "Plymouth",
        "state": "MI",
    }


def listing_row_html(item: dict[str, str]) -> str:
    detail_href = (
        f"job-opening.php?req={item['requisitionId']}"
        f"&amp;req_loc={item['locationId']}&amp;&amp;#job"
    )
    return f"""
      <tr class="reqitem ReqRowClick" data-req-id="{item['requisitionId']}">
        <td class="departments reqitem">{item['department']}</td>
        <td class="posTitle reqitem">
          <a href="{detail_href}">{item['title']}</td>
        <td class="cities reqitem">{item['city']}</td>
        <td class="state reqitem">{item['state']}</td>
      </tr>
    """


def listing_html(
    records: list[dict[str, str]],
    *,
    reported_total: int | None = None,
    pagination: bool = False,
) -> str:
    total = len(records) if reported_total is None else reported_total
    department_counts: dict[str, int] = {}
    for item in records:
        department = item["department"]
        department_counts[department] = department_counts.get(department, 0) + 1
    departments = "".join(
        f'<option value="{index}">{name} - {count} '
        f'{"Job" if count == 1 else "Jobs"}</option>'
        for index, (name, count) in enumerate(department_counts.items(), start=1)
    )
    rows = "".join(listing_row_html(item) for item in records)
    pagination_link = (
        '<a class="pagination" href="job-openings.php?search=true&amp;page=2">Next</a>'
        if pagination
        else ""
    )
    return f"""
      <div class="careersTitle">Careers At Michigan Educational Credit Union</div>
      <form action="job-openings.php" name="searchReqs">
        <input name="search" value="true">
        <select name="dept">
          <option value="-1">- All Areas of Interest -</option>
          {departments}
        </select>
        <select name="city">
          <option value="-1">- All Cities -</option>
          <option value="Plymouth">Plymouth - {total} Jobs</option>
        </select>
        <select name="state">
          <option value="-1">- All States -</option>
          <option value="MI">MI - {total} Jobs</option>
        </select>
      </form>
      <table class="reqResultTable">{rows}</table>
      {pagination_link}
    """


def empty_listing_html() -> str:
    return """
      <form action="job-openings.php" name="searchReqs">
        <select name="dept"><option value="-1">- All Areas -</option></select>
      </form>
      <div id="noOpeningsMsg">There are no current openings.</div>
    """


def detail_html(
    title: str,
    department: str,
    *,
    description: str | None = None,
) -> str:
    description = description or f"Current public opening for {title}."
    return f"""
      <title>{title} - Careers At Michigan Educational Credit Union</title>
      <table class="viewFields">
        <tr><td class="viewFieldName"><b>Department:</b></td>
            <td class="viewFieldValue">{department}</td></tr>
        <tr><td class="viewFieldName"><b>Location:</b></td>
            <td class="viewFieldValue">Plymouth, MI</td></tr>
      </table>
      <div class="jobDesc"><p>{description}</p></div>
    """


def response_router(
    records: list[dict[str, str]],
    details: dict[str, str | Exception],
    calls: list[str],
):
    board_html = listing_html(records)

    def fake_get(_collector: ClearCompanyCollector, url: str) -> FakeResponse:
        calls.append(url)
        if url == BOARD_URL:
            return FakeResponse(BOARD_URL, board_html)
        requisition_id = parse_qs(urlsplit(url).query).get("req", [""])[0]
        result = details[requisition_id]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(url, result)

    return fake_get


class ClearCompanyCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-michedcu-org",
            "Company Name": "Michigan Educational Credit Union",
            "Official Website": "https://www.michedcu.org/",
            "Careers Page URL": "https://www.michedcu.org/about-us/careers",
            "Job Board URL": BOARD_URL,
            # Mirrors production: URL detection must work with a blank field.
            "Job Platform": "",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Completed",
        }
        self.official_records = [
            record(
                "3775648",
                "1412427",
                "Accounting & Payments Specialist",
                "Accounting",
            ),
            record("3787742", "1431692", "CFO", "Executive Team"),
        ]
        self.details: dict[str, str | Exception] = {
            "3775648": detail_html(
                "Accounting & Payments Specialist",
                "Accounting",
            ),
            "3787742": detail_html(
                "CFO",
                "Executive Team",
                description="Chief Financial Officer leadership role.",
            ),
        }

    def test_structured_listing_keeps_the_legitimate_short_cfo_title(self) -> None:
        calls: list[str] = []
        collector = ClearCompanyCollector(delay_seconds=0)
        with patch.object(
            ClearCompanyCollector,
            "get",
            new=response_router(self.official_records, self.details, calls),
        ):
            jobs = collector.collect(self.company)

        self.assertFalse(collector.requires_browser)
        self.assertEqual(
            (collector.candidate_count, collector.rejected_count, collector.saved_count),
            (2, 0, 2),
        )
        self.assertEqual(
            {job.title for job in jobs},
            {"Accounting & Payments Specialist", "CFO"},
        )
        self.assertEqual(len({job.id for job in jobs}), 2)
        self.assertEqual(len({job.sourceUrl for job in jobs}), 2)
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))
        self.assertEqual(collector.final_url_after_redirect, BOARD_URL)

        cfo = next(job for job in jobs if job.title == "CFO")
        self.assertEqual(cfo.location, "Plymouth, MI")
        self.assertEqual(cfo.jobPlatform, "ClearCompany")
        self.assertEqual(cfo.rawData["hrmDirectRequisitionId"], "3787742")
        self.assertTrue(cfo.rawData["structuredSource"])
        self.assertTrue(cfo.rawData["detailRetrieved"])

    def test_detail_failure_keeps_the_authoritative_listing_record(self) -> None:
        details = dict(self.details)
        details["3787742"] = RuntimeError("detail request failed")
        collector = ClearCompanyCollector(delay_seconds=0)
        with patch.object(
            ClearCompanyCollector,
            "get",
            new=response_router(self.official_records, details, []),
        ):
            jobs = collector.collect(self.company)

        cfo = next(job for job in jobs if job.title == "CFO")
        self.assertFalse(cfo.rawData["detailRetrieved"])
        self.assertEqual(cfo.description, "CFO")
        self.assertEqual(cfo.location, "Plymouth, MI")

    def test_listing_contract_fails_closed_on_partial_or_paginated_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "facet"):
            parse_hrmdirect_listing(
                listing_html(self.official_records, reported_total=3),
                BOARD_URL,
            )
        with self.assertRaisesRegex(ValueError, "pagination controls"):
            parse_hrmdirect_listing(
                listing_html(self.official_records, pagination=True),
                BOARD_URL,
            )
        with self.assertRaisesRegex(ValueError, "explicit empty result"):
            parse_hrmdirect_listing(
                '<form action="job-openings.php"></form>',
                BOARD_URL,
            )
        self.assertEqual(parse_hrmdirect_listing(empty_listing_html(), BOARD_URL), [])

    def test_duplicate_requisition_id_fails_instead_of_silently_pruning(self) -> None:
        duplicate = [self.official_records[0], dict(self.official_records[0])]
        collector = ClearCompanyCollector(delay_seconds=0)
        with patch.object(
            ClearCompanyCollector,
            "get",
            new=response_router(duplicate, self.details, []),
        ):
            with self.assertRaisesRegex(ValueError, "duplicate requisition ID"):
                collector.collect(self.company)
        self.assertIn(
            "duplicate HRMDirect requisition ID",
            {item["reason"] for item in collector.rejection_samples},
        )

    def test_detection_picker_and_url_validation_use_the_verified_board(self) -> None:
        selected = pick_collector(self.company, delay_seconds=0)
        self.assertIsInstance(selected, ClearCompanyCollector)
        self.assertFalse(selected.requires_browser)
        self.assertEqual(detect_job_platform(BOARD_URL), "ClearCompany")
        self.assertEqual(
            validate_hrmdirect_board_url(
                f"{BOARD_URL}&dept=301401&sort=pa#openings"
            ),
            BOARD_URL,
        )
        self.assertEqual(
            validate_hrmdirect_detail_url(
                "https://michedcu.hrmdirect.com/employment/"
                "job-opening.php?req=3787742&req_loc=1431692&&#job",
                expected_host="michedcu.hrmdirect.com",
                expected_requisition_id="3787742",
                expected_location_id="1431692",
            ),
            "https://michedcu.hrmdirect.com/employment/"
            "job-opening.php?req=3787742&req_loc=1431692#job",
        )
        for unsafe in (
            "http://michedcu.hrmdirect.com/employment/job-openings.php",
            "https://hrmdirect.com/employment/job-openings.php",
            "https://michedcu.hrmdirect.com.attacker.example/employment/job-openings.php",
            "https://michedcu.hrmdirect.com/employment/job-opening.php?req=3787742",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(ValueError):
                    validate_hrmdirect_board_url(unsafe)

    def test_static_discovery_recognizes_the_official_hrmdirect_link(self) -> None:
        careers_html = (
            '<a href="https://michedcu.hrmdirect.com/employment/'
            'job-openings.php?search=true&amp;">Current job opportunities</a>'
        )
        with patch(
            "job_board_discovery.fetch_html",
            return_value=(self.company["Careers Page URL"], careers_html),
        ):
            candidates = static_scan(
                self.company["Careers Page URL"],
                self.company["Company Name"],
                object(),  # type: ignore[arg-type]
                "Static Link",
            )

        selected = next(candidate for candidate in candidates if not candidate.rejected)
        self.assertEqual(selected.url, f"{BOARD_URL}&")
        self.assertEqual(selected.platform, "ClearCompany")

    def test_complete_flow_filters_deduplicates_persists_and_reports_diagnostics(self) -> None:
        records = [
            *self.official_records,
            record(
                "3999999",
                "1499999",
                "General Employment Application",
                "Human Resources",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch.object(
                    ClearCompanyCollector,
                    "get",
                    new=response_router(records, self.details, []),
                ),
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
                (root / "logs" / "job_collection_diagnostics.json").read_text(
                    encoding="utf-8"
                )
            )[0]
            rejected = json.loads(
                (root / "logs" / "rejected_job_candidates.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual((summary["jobs_found"], summary["errors"]), (2, 0))
            self.assertEqual((len(payload), len({job["id"] for job in payload})), (2, 2))
            self.assertEqual(
                (
                    diagnostic["collectorSelected"],
                    diagnostic["playwrightUsed"],
                    diagnostic["candidateJobElementsFound"],
                    diagnostic["validJobsSaved"],
                    diagnostic["status"],
                ),
                ("ClearCompanyCollector", False, 3, 2, "Jobs Collected"),
            )
            self.assertEqual(
                [item["candidateText"] for item in rejected],
                ["General Employment Application"],
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
                        "jobPlatform": "ClearCompany",
                        "jobBoardDiscoveryMethod": "Verified official careers link",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(
                payload,
                {self.company["Company ID"]},
            )
            repository.upsert_jobs_for_companies(
                payload,
                {self.company["Company ID"]},
            )
            repository.replace_raw_candidates(
                rejected,
                company_ids={self.company["Company ID"]},
            )
            stored = repository.list_jobs()
            self.assertEqual((len(stored), len({job["sourceUrl"] for job in stored})), (2, 2))
            self.assertEqual(
                {job["title"] for job in stored},
                {"Accounting & Payments Specialist", "CFO"},
            )
            with repository.connection(readonly=True) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM raw_job_candidates"
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
