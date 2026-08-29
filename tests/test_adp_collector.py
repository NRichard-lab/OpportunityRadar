from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from backend.repository import OpportunityRepository
from collectors.adp_collector import ADPCollector, build_adp_api_request
from collectors.base import pick_collector
from job_board_discovery import rejection_reason as discovery_rejection_reason
from job_platforms import detect_job_platform
from job_tools import collect_jobs, invalid_job_board_reason
from job_validation import is_valid_job_title


BOARD_URL = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    "?cid=3993975e-194c-4504-9c5e-9e6017ca5023"
    "&ccId=19000101_000001&type=JS&lang=en_US&tracking=not-for-destination"
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json(self) -> object:
        return self.payload


def requisition(
    external_id: str,
    title: str,
    *,
    work_type: str | None = "Full-Time",
    locations: tuple[str, ...] = ("Gettysburg, PA, US",),
    pay: bool = False,
) -> dict:
    string_fields = [
        {"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": external_id},
        {"nameCode": {"codeValue": "JobClass"}},
    ]
    custom_fields: dict[str, list[dict]] = {"stringFields": string_fields}
    record: dict = {
        "itemID": f"item-{external_id}",
        "clientRequisitionID": f"client-{external_id}",
        "requisitionTitle": title,
        "postDate": "2026-08-28T10:39:00.000-04:00",
        "requisitionLocations": [
            {"nameCode": {"shortName": location}, "address": {"cityName": location.split(",")[0]}}
            for location in locations
        ],
        "customFieldGroup": custom_fields,
    }
    if work_type is not None:
        record["workLevelCode"] = {"shortName": work_type}
    if pay:
        string_fields.append(
            {"nameCode": {"codeValue": "SalaryRange"}, "stringValue": "19.71 To 24.66 (USD) Hourly"}
        )
        custom_fields["codeFields"] = [
            {
                "nameCode": {"codeValue": "SalaryType"},
                "codeValue": "HR",
                "shortName": "Hourly",
            }
        ]
        record["payGradeRange"] = {
            "minimumRate": {"amountValue": 19.71, "currencyCode": "USD"},
            "maximumRate": {"amountValue": 24.66, "currencyCode": "USD"},
        }
    return record


def response_router(pages: dict[int, dict], details: dict[str, dict], calls: list[str]):
    def fake_get(_collector: ADPCollector, url: str) -> FakeResponse:
        calls.append(url)
        parsed = urlsplit(url)
        if parsed.path.endswith("/job-requisitions"):
            query = parse_qs(parsed.query)
            sequence = int(query.get("$skip", ["1"])[0])
            return FakeResponse(pages[sequence])
        external_id = parsed.path.rsplit("/", 1)[-1]
        return FakeResponse(details[external_id])

    return fake_get


class ADPCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.company = {
            "Company ID": "company-acnb",
            "Company Name": "ACNB Bank",
            "Official Website": "https://www.acnb.com/",
            "Careers Page URL": "https://www.acnb.com/careers-employment",
            "Job Board URL": BOARD_URL,
            # This stale value mirrors the production failure and must not mask URL detection.
            "Job Platform": "Indeed Company Jobs",
            "Job Board Discovery Method": "Not Found",
            "Search Status": "Failed",
        }
        self.records = [
            requisition("100", "Receptionist - Temporary position", work_type=None),
            requisition(
                "101",
                "Community Banking Specialist",
                locations=("Gettysburg, PA, US", "Hanover, PA, US"),
                pay=True,
            ),
            requisition("102", "Application Support Analyst", locations=()),
            requisition("103", "General Employment Application", locations=()),
        ]
        self.details = {
            record["customFieldGroup"]["stringFields"][0]["stringValue"]: {
                **record,
                "requisitionDescription": f"<div><p>{record['requisitionTitle']}</p><p>Real job details.</p></div>",
            }
            for record in self.records
        }

    def test_public_api_paginates_normalizes_filters_and_uses_stable_destinations(self) -> None:
        pages = {
            1: {"jobRequisitions": self.records[:2], "meta": {"startSequence": 1, "totalNumber": 4}},
            3: {"jobRequisitions": self.records[2:], "meta": {"startSequence": 3, "totalNumber": 4}},
        }
        calls: list[str] = []
        collector = ADPCollector(delay_seconds=0)
        details_with_one_failure = dict(self.details)
        details_with_one_failure.pop("102")
        with (
            patch("collectors.adp_collector.ADP_PAGE_SIZE", 2),
            patch.object(ADPCollector, "get", new=response_router(pages, details_with_one_failure, calls)),
        ):
            jobs = collector.collect(self.company)

        self.assertFalse(collector.requires_browser)
        self.assertEqual(collector.candidate_count, 4)
        self.assertEqual(collector.rejected_count, 1)
        self.assertEqual(collector.saved_count, 3)
        self.assertEqual(
            {job.title for job in jobs},
            {"Receptionist - Temporary position", "Community Banking Specialist", "Application Support Analyst"},
        )
        self.assertTrue(any("$skip=3" in url for url in calls))
        self.assertFalse(any("$skip=2" in url for url in calls))
        self.assertTrue(all("tracking=" not in job.sourceUrl for job in jobs))
        self.assertEqual({parse_qs(urlsplit(job.sourceUrl).query)["jobId"][0] for job in jobs}, {"100", "101", "102"})
        self.assertEqual(len({job.id for job in jobs}), 3)
        self.assertEqual({job.id.split("-")[2] for job in jobs}, {"100", "101", "102"})
        specialist = next(job for job in jobs if job.title == "Community Banking Specialist")
        self.assertEqual(specialist.location, "Gettysburg, PA, US; Hanover, PA, US")
        self.assertEqual((specialist.payMin, specialist.payMax, specialist.payPeriod), (19.71, 24.66, "hourly"))
        self.assertIn("Real job details.", specialist.description)
        receptionist = next(job for job in jobs if job.title.startswith("Receptionist"))
        self.assertEqual(receptionist.workType, "Not Listed")
        application_support = next(job for job in jobs if job.title == "Application Support Analyst")
        self.assertFalse(application_support.rawData["detailRetrieved"])

    def test_duplicate_external_id_is_not_saved_twice(self) -> None:
        same_title_different_id = requisition("102", "Community Banking Specialist")
        pages = {
            1: {"jobRequisitions": self.records[:2], "meta": {"startSequence": 1, "totalNumber": 4}},
            3: {"jobRequisitions": [self.records[1], same_title_different_id], "meta": {"startSequence": 3, "totalNumber": 4}},
        }
        collector = ADPCollector(delay_seconds=0)
        with (
            patch("collectors.adp_collector.ADP_PAGE_SIZE", 2),
            patch.object(ADPCollector, "get", new=response_router(pages, self.details, [])),
        ):
            jobs = collector.collect(self.company)

        self.assertEqual(len(jobs), 3)
        self.assertEqual(sum(job.title == "Community Banking Specialist" for job in jobs), 2)
        self.assertEqual(len({job.id for job in jobs}), 3)
        self.assertIn("duplicate ADP ExternalJobID", {item["reason"] for item in collector.rejection_samples})

    def test_incomplete_api_response_fails_instead_of_authoritatively_pruning(self) -> None:
        pages = {
            1: {"jobRequisitions": self.records[:2], "meta": {"startSequence": 1, "totalNumber": 3}},
            3: {"jobRequisitions": [], "meta": {"startSequence": 3, "totalNumber": 3}},
        }
        collector = ADPCollector(delay_seconds=0)
        with (
            patch("collectors.adp_collector.ADP_PAGE_SIZE", 2),
            patch.object(ADPCollector, "get", new=response_router(pages, self.details, [])),
        ):
            with self.assertRaisesRegex(ValueError, "empty page"):
                collector.collect(self.company)

    def test_empty_api_result_requires_explicit_zero_total(self) -> None:
        valid_empty = {1: {"jobRequisitions": [], "meta": {"startSequence": 1, "totalNumber": 0}}}
        missing_meta = {1: {"jobRequisitions": []}}
        with patch.object(ADPCollector, "get", new=response_router(valid_empty, {}, [])):
            self.assertEqual(ADPCollector(delay_seconds=0).collect(self.company), [])
        with patch.object(ADPCollector, "get", new=response_router(missing_meta, {}, [])):
            with self.assertRaisesRegex(ValueError, "meta object"):
                ADPCollector(delay_seconds=0).collect(self.company)

    def test_collector_selection_uses_verified_url_despite_stale_platform(self) -> None:
        selected = pick_collector(self.company, delay_seconds=0)
        self.assertIsInstance(selected, ADPCollector)
        self.assertFalse(selected.requires_browser)

        embedded = dict(self.company, **{"Job Board URL": "https://bank.example/careers", "Job Platform": "ADP Workforce Now"})
        self.assertIsInstance(pick_collector(embedded, delay_seconds=0), ADPCollector)

    def test_unsupported_indeed_profile_is_rejected_in_discovery_and_collection(self) -> None:
        indeed = "https://www.indeed.com/cmp/Acnb-Bank-1"
        self.assertEqual(detect_job_platform(indeed), "")
        self.assertTrue(discovery_rejection_reason(indeed))
        self.assertTrue(invalid_job_board_reason(indeed))

    def test_title_filter_targets_generic_application_without_harming_real_roles(self) -> None:
        self.assertTrue(is_valid_job_title("Receptionist - Temporary position"))
        self.assertFalse(is_valid_job_title("General Employment Application"))
        self.assertTrue(is_valid_job_title("Application Support Analyst"))

    def test_api_url_requires_verified_workforce_now_tenant_parameters(self) -> None:
        api_url, params = build_adp_api_request(BOARD_URL)
        self.assertEqual(urlsplit(api_url).hostname, "workforcenow.adp.com")
        self.assertEqual(params["cid"], "3993975e-194c-4504-9c5e-9e6017ca5023")
        with self.assertRaisesRegex(ValueError, "missing cid or ccId"):
            build_adp_api_request("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html")
        with self.assertRaisesRegex(ValueError, "requires an HTTPS"):
            build_adp_api_request("https://attacker.example/mascsr/default/mdf/recruitment/recruitment.html?cid=x&ccId=y")

    def test_complete_collection_flow_persists_deduplicated_jobs_and_diagnostics(self) -> None:
        pages = {
            1: {"jobRequisitions": self.records[:2], "meta": {"startSequence": 1, "totalNumber": 4}},
            3: {"jobRequisitions": self.records[2:], "meta": {"startSequence": 3, "totalNumber": 4}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            jobs_json = root / "jobs.json"
            with (
                patch("job_tools.read_company_rows", return_value=[self.company]),
                patch("job_tools.LOG_DIR", root / "logs"),
                patch("job_tools.OUTPUT_DIR", root / "output"),
                patch("collectors.adp_collector.ADP_PAGE_SIZE", 2),
                patch.object(ADPCollector, "get", new=response_router(pages, self.details, [])),
            ):
                summary = collect_jobs(
                    master_path=root / "companies.xlsx",
                    jobs_json_path=jobs_json,
                    jobs_xlsx_path=root / "jobs.xlsx",
                    company_ids={"company-acnb"},
                    max_workers=1,
                    browser_workers=1,
                    delay_seconds=0,
                )

            payload = json.loads(jobs_json.read_text(encoding="utf-8"))
            diagnostic = json.loads((root / "logs" / "job_collection_diagnostics.json").read_text(encoding="utf-8"))[0]
            rejected = json.loads((root / "logs" / "rejected_job_candidates.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["jobs_found"], summary["errors"]), (3, 0))
            self.assertEqual(len(payload), 3)
            self.assertEqual(len({job["id"] for job in payload}), 3)
            self.assertEqual(
                (diagnostic["collectorSelected"], diagnostic["playwrightUsed"], diagnostic["validJobsSaved"], diagnostic["status"]),
                ("ADPCollector", False, 3, "Jobs Collected"),
            )
            self.assertEqual([item["candidateText"] for item in rejected], ["General Employment Application"])

            repository = OpportunityRepository(root / "radar.db", initialize=True)
            repository.upsert_company_snapshots(
                [{
                    "id": "company-acnb",
                    "name": "ACNB Bank",
                    "officialWebsite": "https://www.acnb.com/",
                    "careersPageUrl": "https://www.acnb.com/careers-employment",
                    "jobBoardUrl": BOARD_URL,
                    "jobPlatform": "ADP Workforce Now",
                    "jobBoardDiscoveryMethod": "Manual",
                    "searchStatus": "Completed",
                }]
            )
            self.assertEqual(repository.list_jobs(), [])
            repository.upsert_jobs_for_companies(payload, {"company-acnb"})
            repository.upsert_jobs_for_companies(payload, {"company-acnb"})
            repository.replace_raw_candidates(rejected, company_ids={"company-acnb"})
            stored = repository.list_jobs()
            self.assertEqual(len(stored), 3)
            self.assertEqual({job["sourceUrl"] for job in stored}, {job["sourceUrl"] for job in payload})
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
