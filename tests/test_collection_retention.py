from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import patch

from job_tools import JobRecord, collect_jobs


class FakeCollector:
    requires_browser = False
    candidate_count = 1
    rejected_count = 0
    rejected_candidates: list[dict] = []
    candidate_samples: list[dict] = []
    rejection_samples: list[dict] = []
    final_url_after_redirect = ""
    selection_reason = "test collector"

    def __init__(self, result: list[JobRecord] | Exception) -> None:
        self.result = result

    def source_url(self, company: dict) -> tuple[str, str]:
        return str(company.get("Job Board URL") or ""), "Job Board URL"

    def collect(self, _company: dict) -> list[JobRecord]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CollectionRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jobs_json = self.root / "jobs.json"
        self.jobs_xlsx = self.root / "jobs.xlsx"
        self.master = self.root / "master.xlsx"
        self.companies = [
            self.company("company-a", "Alpha Bank"),
            self.company("company-b", "Beta Bank"),
        ]
        self.old_a = self.job("old-a", "company-a", "Alpha Bank", "Security Analyst")
        self.old_b = self.job("old-b", "company-b", "Beta Bank", "Cloud Engineer")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def company(company_id: str, name: str) -> dict:
        return {
            "Company ID": company_id,
            "Company Name": name,
            "Job Board URL": f"https://jobs.example.com/{company_id}",
            "Job Platform": "Test",
        }

    @staticmethod
    def job(job_id: str, company_id: str, company_name: str, title: str) -> JobRecord:
        return JobRecord(
            id=job_id,
            companyId=company_id,
            companyName=company_name,
            title=title,
            sourceUrl=f"https://jobs.example.com/postings/{job_id}",
            description="A valid job description",
        )

    def write_existing(self) -> None:
        self.jobs_json.write_text(
            json.dumps([asdict(self.old_a), asdict(self.old_b)]),
            encoding="utf-8",
        )

    def run_collection(self, results: dict[str, list[JobRecord] | Exception], **kwargs):
        def pick(company: dict, **_collector_kwargs):
            return FakeCollector(results[str(company["Company ID"])])

        with (
            patch("job_tools.read_company_rows", return_value=self.companies),
            patch("collectors.base.pick_collector", side_effect=pick),
            patch("job_tools.LOG_DIR", self.root / "logs"),
            patch("job_tools.OUTPUT_DIR", self.root / "output"),
        ):
            return collect_jobs(
                master_path=self.master,
                jobs_json_path=self.jobs_json,
                jobs_xlsx_path=self.jobs_xlsx,
                max_workers=2,
                browser_workers=1,
                delay_seconds=0,
                **kwargs,
            )

    def loaded_ids(self) -> set[str]:
        return {item["id"] for item in json.loads(self.jobs_json.read_text(encoding="utf-8"))}

    def test_collector_exception_retains_last_known_jobs(self) -> None:
        self.write_existing()
        summary = self.run_collection({
            "company-a": RuntimeError("secret collector detail"),
            "company-b": [self.job("new-b", "company-b", "Beta Bank", "Senior Cloud Engineer")],
        })
        self.assertEqual(self.loaded_ids(), {"old-a", "new-b"})
        self.assertEqual(summary["outcome_counts"]["failed"], 1)
        failed = next(item for item in summary["collection_results"] if item["companyName"] == "Alpha Bank")
        self.assertEqual(failed["dataDisposition"], "retained")

    def test_timeout_retains_last_known_jobs(self) -> None:
        self.write_existing()
        summary = self.run_collection({
            "company-a": TimeoutError("host detail"),
            "company-b": [self.old_b],
        })
        self.assertIn("old-a", self.loaded_ids())
        self.assertEqual(summary["outcome_counts"]["timed-out"], 1)

    def test_successful_empty_result_authoritatively_replaces_scope(self) -> None:
        self.write_existing()
        summary = self.run_collection({"company-a": [], "company-b": [self.old_b]})
        self.assertEqual(self.loaded_ids(), {"old-b"})
        self.assertEqual(summary["outcome_counts"]["success-empty"], 1)

    def test_limited_run_only_changes_selected_company(self) -> None:
        self.write_existing()
        summary = self.run_collection(
            {
                "company-a": [self.job("new-a", "company-a", "Alpha Bank", "Senior Security Analyst")],
                "company-b": [],
            },
            limit_companies=1,
        )
        self.assertEqual(self.loaded_ids(), {"new-a", "old-b"})
        retained = next(item for item in summary["collection_results"] if item["companyName"] == "Beta Bank")
        self.assertEqual((retained["outcome"], retained["dataDisposition"]), ("stale", "retained"))

    def test_cancellation_never_replaces_existing_file(self) -> None:
        self.write_existing()
        before = self.jobs_json.read_bytes()
        cancellation = Event()
        cancellation.set()
        with self.assertRaises(InterruptedError):
            self.run_collection(
                {"company-a": [], "company-b": []},
                cancellation_event=cancellation,
            )
        self.assertEqual(self.jobs_json.read_bytes(), before)

    def test_browser_collectors_do_not_launch_when_feature_is_disabled(self) -> None:
        self.write_existing()
        with patch.object(FakeCollector, "requires_browser", True), patch("job_tools.APP_ENABLE_BROWSER_JOBS", False):
            summary = self.run_collection({"company-a": [], "company-b": []})
        self.assertEqual(self.loaded_ids(), {"old-a", "old-b"})
        self.assertEqual(summary["outcome_counts"], {"stale": 2})

    def test_duplicate_company_names_do_not_cross_prune_distinct_ids(self) -> None:
        self.companies[1]["Company Name"] = "Alpha Bank"
        self.old_b.companyName = "Alpha Bank"
        self.write_existing()
        self.run_collection({
            "company-a": [self.job("new-a", "company-a", "Alpha Bank", "Senior Security Analyst")],
            "company-b": RuntimeError("collector failed"),
        })
        self.assertEqual(self.loaded_ids(), {"new-a", "old-b"})


if __name__ == "__main__":
    unittest.main()
