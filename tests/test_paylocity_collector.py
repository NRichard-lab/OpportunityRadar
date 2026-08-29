from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.base import pick_collector
from collectors.paylocity_collector import (
    PaylocityCollector,
    parse_paylocity_page_data,
    validate_paylocity_listing_url,
)
from job_tools import collect_jobs, is_valid_job_record


BOARD_URL = (
    "https://recruiting.paylocity.com/recruiting/jobs/All/"
    "8da14725-1e75-40e3-b44d-c09a923aad3a/Blaze-Credit-Union"
)


class FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


def listing(job_id: int, title: str, city: str, *, state: str = "MN") -> dict:
    return {
        "JobId": job_id,
        "JobTitle": title,
        "LocationName": city,
        "ShouldDisplayLocation": True,
        "PublishedDate": "2026-08-28T08:26:50-06:00",
        "Description": f"Current official opening for {title}.",
        "IsInternal": False,
        "HiringDepartment": "Member Experience",
        "JobLocation": {"City": city, "State": state, "Country": "USA"},
        "IsRemote": False,
    }


def listing_html(records: list[dict]) -> str:
    payload = {
        "Jobs": records,
        "ModuleId": 34683,
        "ModuleTitle": "Blaze Credit Union",
    }
    return f"<script>window.pageData = {json.dumps(payload)};</script>"


def detail_html(title: str, location: str, *, salary: str = "") -> str:
    salary_section = (
        f'<div class="job-listing-header">Salary Description</div><div>{salary}</div>'
        if salary
        else ""
    )
    return f"""
        <span class="job-preview-title"><span>{title}</span></span>
        <div class="preview-location">{location}</div>
        <div class="job-preview-details">
          <div class="job-listing-header">Job Type</div><div>Full-time</div>
          <div class="job-listing-header">Description</div>
          <div><p>Full public description for {title}.</p></div>
          {salary_section}
        </div>
    """


def response_router(
    records: list[dict],
    details: dict[str, str | Exception],
    calls: list[str],
):
    page = listing_html(records)

    def fake_get(_collector: PaylocityCollector, url: str) -> FakeResponse:
        calls.append(url)
        if url == BOARD_URL:
            return FakeResponse(BOARD_URL, page)
        job_id = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        result = details[job_id]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(url, result)

    return fake_get


class PaylocityCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-blazecu-com",
            "Company Name": "Blaze Credit Union",
            "Official Website": "https://blazecu.com/",
            "Careers Page URL": "https://blazecu.com/about/careers",
            "Job Board URL": BOARD_URL,
            # Mirrors production: URL detection must work with a blank field.
            "Job Platform": "",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Failed",
        }
        self.records = [
            listing(4446917, "Collector", "Saint Paul"),
            listing(4444008, "Archie Handler", "Falcon Heights"),
            listing(4452699, "Teller", "Vadnais Heights"),
            listing(4452700, "Teller", "Minneapolis"),
            listing(4000000, "General Employment Application", "Saint Paul"),
        ]
        self.details: dict[str, str | Exception] = {
            "4446917": detail_html("Collector", "Saint Paul, MN", salary="$21.25-$23.75/hour"),
            "4444008": detail_html("Archie Handler", "Falcon Heights, MN"),
            "4452699": detail_html("Teller", "Vadnais Heights, MN"),
            # A detail outage must not discard an authoritative listing record.
            "4452700": RuntimeError("detail request failed"),
        }

    def test_structured_listing_filters_normalizes_and_keeps_distinct_repeated_titles(self) -> None:
        calls: list[str] = []
        collector = PaylocityCollector(delay_seconds=0)
        with patch.object(
            PaylocityCollector,
            "get",
            new=response_router(self.records, self.details, calls),
        ):
            jobs = collector.collect(self.company)

        self.assertFalse(collector.requires_browser)
        self.assertEqual((collector.candidate_count, collector.rejected_count, collector.saved_count), (5, 1, 4))
        self.assertEqual(
            {job.title for job in jobs},
            {"Collector", "Archie Handler", "Teller"},
        )
        self.assertEqual(sum(job.title == "Teller" for job in jobs), 2)
        self.assertEqual(len({job.id for job in jobs}), 4)
        self.assertEqual(len({job.sourceUrl for job in jobs}), 4)
        self.assertTrue(all(is_valid_job_record(job) for job in jobs))
        self.assertEqual(collector.final_url_after_redirect, BOARD_URL)
        self.assertFalse(any("4000000" in url for url in calls))

        collector_job = next(job for job in jobs if job.title == "Collector")
        self.assertEqual(collector_job.location, "Saint Paul, MN")
        self.assertEqual((collector_job.payMin, collector_job.payMax, collector_job.payPeriod), (21.25, 23.75, "hourly"))
        self.assertEqual(collector_job.workType, "Onsite")
        self.assertTrue(collector_job.rawData["structuredSource"])
        self.assertTrue(collector_job.rawData["detailRetrieved"])

        fallback = next(job for job in jobs if job.rawData["paylocityJobId"] == "4452700")
        self.assertFalse(fallback.rawData["detailRetrieved"])
        self.assertEqual(fallback.description, "Current official opening for Teller.")
        self.assertEqual(fallback.sourceUrl, "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4452700")

    def test_duplicate_job_id_is_rejected_without_deduplicating_by_title(self) -> None:
        duplicate = [self.records[0], {**self.records[0], "JobTitle": "Collector II"}]
        collector = PaylocityCollector(delay_seconds=0)
        with patch.object(
            PaylocityCollector,
            "get",
            new=response_router(duplicate, self.details, []),
        ):
            jobs = collector.collect(self.company)

        self.assertEqual(len(jobs), 1)
        self.assertIn("duplicate Paylocity JobId", {item["reason"] for item in collector.rejection_samples})

    def test_listing_contract_fails_closed_but_explicit_empty_jobs_is_authoritative(self) -> None:
        with self.assertRaisesRegex(ValueError, "window.pageData"):
            parse_paylocity_page_data("<html>No structured listing here</html>")

        missing_jobs = '<script>window.pageData = {"ModuleId": 34683};</script>'
        collector = PaylocityCollector(delay_seconds=0)
        with patch.object(
            PaylocityCollector,
            "get",
            return_value=FakeResponse(BOARD_URL, missing_jobs),
        ):
            with self.assertRaisesRegex(ValueError, "Jobs list"):
                collector.collect(self.company)

        collector = PaylocityCollector(delay_seconds=0)
        with patch.object(
            PaylocityCollector,
            "get",
            return_value=FakeResponse(BOARD_URL, listing_html([])),
        ):
            self.assertEqual(collector.collect(self.company), [])

    def test_detection_and_url_validation_use_the_verified_board(self) -> None:
        selected = pick_collector(self.company, delay_seconds=0)
        self.assertIsInstance(selected, PaylocityCollector)
        self.assertFalse(selected.requires_browser)
        self.assertEqual(validate_paylocity_listing_url(BOARD_URL), BOARD_URL)
        for unsafe in (
            "http://recruiting.paylocity.com/recruiting/jobs/All/tenant/company",
            "https://recruiting.paylocity.com.attacker.example/recruiting/jobs/All/tenant/company",
            "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4446917",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(ValueError):
                    validate_paylocity_listing_url(unsafe)

    def test_complete_flow_persists_deduplicated_jobs_and_http_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch.object(
                    PaylocityCollector,
                    "get",
                    new=response_router(self.records, self.details, []),
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
                (root / "logs" / "job_collection_diagnostics.json").read_text(encoding="utf-8")
            )[0]
            rejected = json.loads(
                (root / "logs" / "rejected_job_candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual((summary["jobs_found"], summary["errors"]), (4, 0))
            self.assertEqual((len(payload), len({job["id"] for job in payload})), (4, 4))
            self.assertEqual(
                (
                    diagnostic["collectorSelected"],
                    diagnostic["playwrightUsed"],
                    diagnostic["validJobsSaved"],
                    diagnostic["status"],
                ),
                ("PaylocityCollector", False, 4, "Jobs Collected"),
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
                        "jobPlatform": "Paylocity",
                        "jobBoardDiscoveryMethod": "Verified official careers redirect",
                        "searchStatus": "Completed",
                    }
                ]
            )
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.upsert_jobs_for_companies(payload, {self.company["Company ID"]})
            repository.replace_raw_candidates(rejected, company_ids={self.company["Company ID"]})
            stored = repository.list_jobs()
            self.assertEqual(len(stored), 4)
            self.assertEqual({job["sourceUrl"] for job in stored}, {job["sourceUrl"] for job in payload})
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
