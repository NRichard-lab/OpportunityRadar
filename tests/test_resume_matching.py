import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import patch

from docx import Document

from backend.repository import OpportunityRepository
from backend.resume_files import build_resume_profile
from backend.resume_matching import ResumeMatchService


class ResumeMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = OpportunityRepository(Path(self.temporary.name) / "opportunity_radar.db", initialize=True)
        self.company = self.repository.create_company({"name": "Example Company", "country": "United States"})
        self.job = {
            "id": "job-example", "companyId": self.company["id"], "companyName": self.company["name"],
            "title": "Cloud Security Engineer", "description": "Azure cloud security automation and PowerShell",
            "sourceUrl": "https://example.test/jobs/1", "status": "Open", "workType": "Remote",
        }
        self.repository.upsert_jobs([self.job])
        self.repository.upsert_resume({
            "id": "resume-v1", "version": "resume-v1", "fileName": "resume.docx",
            "rawText": "Cloud security engineer with 8 years of Azure automation and PowerShell experience.",
            "extractedText": "Cloud security engineer with 8 years of Azure automation and PowerShell experience.",
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_match_persists_and_unchanged_job_is_skipped(self) -> None:
        service = ResumeMatchService(self.repository)
        result = service.match_job(self.job["id"])
        self.assertEqual(result["matchStatus"], "Matched")
        self.assertIsInstance(result["matchScore"], int)
        persisted = OpportunityRepository(self.repository.database_path).get_job(self.job["id"])
        self.assertEqual(persisted["matchScore"], result["matchScore"])
        self.assertEqual(service.match_jobs_if_needed([self.job["id"]]), {"matched": 0, "failed": 0, "skipped": 1})

    def test_individual_rematch_replaces_timestamp(self) -> None:
        service = ResumeMatchService(self.repository)
        with patch("backend.repository.utc_now", return_value="2026-08-25T10:00:00-06:00"):
            service.match_job(self.job["id"])
        with patch("backend.repository.utc_now", return_value="2026-08-25T10:01:00-06:00"):
            updated = service.match_job(self.job["id"])
        self.assertEqual(updated["matchedAt"], "2026-08-25T10:01:00-06:00")

    def test_job_or_resume_change_marks_result_stale(self) -> None:
        service = ResumeMatchService(self.repository)
        service.match_job(self.job["id"])
        changed_job = {**self.job, "description": "Linux infrastructure and database operations"}
        self.repository.upsert_jobs([changed_job])
        self.assertEqual(self.repository.get_job(self.job["id"])["matchStatus"], "Needs Rematch")
        self.assertEqual(service.match_jobs_if_needed([self.job["id"]])["matched"], 1)

        self.repository.upsert_resume({
            "id": "resume-v2", "version": "resume-v2", "fileName": "updated.docx",
            "rawText": "Linux infrastructure leader", "extractedText": "Linux infrastructure leader",
        })
        stale = self.repository.get_job(self.job["id"])
        self.assertEqual(stale["matchStatus"], "Needs Rematch")
        self.assertIsNone(stale["matchScore"])

    def test_bulk_match_reports_real_progress(self) -> None:
        reports = []
        summary = ResumeMatchService(self.repository).rematch_all(
            lambda current, total, item, details: reports.append((current, total, item, details)), Event()
        )
        self.assertEqual(summary, {"jobsProcessed": 1, "jobsMatched": 1, "jobsFailed": 0})
        self.assertEqual(reports[-1][3]["jobsRemaining"], 0)

    def test_matching_failure_keeps_saved_job(self) -> None:
        service = ResumeMatchService(self.repository)
        with patch.object(service, "_match_and_persist", side_effect=RuntimeError("forced failure")):
            result = service.match_jobs_if_needed([self.job["id"]])
        self.assertEqual(result["failed"], 1)
        self.assertTrue(self.repository.job_exists(self.job["id"]))
        self.assertEqual(self.repository.get_job(self.job["id"])["matchStatus"], "Match Failed")

    def test_no_active_resume_keeps_new_job_not_matched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(Path(temp_dir) / "empty-resume.db", initialize=True)
            company = repository.create_company({"name": "No Resume Company"})
            repository.upsert_jobs([{**self.job, "id": "job-no-resume", "companyId": company["id"], "companyName": company["name"]}])
            result = ResumeMatchService(repository).match_jobs_if_needed(["job-no-resume"])
            self.assertEqual(result["reason"], "No active resume.")
            self.assertEqual(repository.get_job("job-no-resume")["matchStatus"], "Not Matched")

    def test_docx_upload_extracts_real_document_text(self) -> None:
        document = Document()
        document.add_paragraph("Azure cloud security and PowerShell automation")
        contents = BytesIO()
        document.save(contents)
        profile = build_resume_profile("resume.docx", contents.getvalue())
        self.assertIn("Azure cloud security", profile["extractedText"])
        self.assertIn("azure", profile["skills"])


if __name__ == "__main__":
    unittest.main()
