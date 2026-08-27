import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.email_service import EmailDeliveryError, EmailService
from backend.repository import OpportunityRepository


class EmailServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = OpportunityRepository(Path(self.temporary.name) / "opportunity_radar.db", initialize=True)
        self.service = EmailService(self.repository)
        self.service.save_settings({
            "smtpHost": "smtp.example.test", "smtpPort": 465, "security": "ssl_tls",
            "smtpUsername": "radar@example.test", "smtpPassword": "top-secret-password",
            "fromEmail": "radar@example.test", "fromName": "Opportunity Radar",
            "dailyEnabled": True, "recipientEmail": "person@example.test",
            "sendAfterRefresh": True, "sendWhenEmpty": False,
        })
        self.company = self.repository.create_company({"name": "Digest Company"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_job(self, job_id: str) -> None:
        self.repository.upsert_jobs([{
            "id": job_id, "companyId": self.company["id"], "companyName": self.company["name"],
            "title": "Systems Engineer", "sourceUrl": f"https://example.test/jobs/{job_id}", "status": "Open",
        }])

    def test_password_is_encrypted_hidden_and_blank_update_preserves_it(self) -> None:
        snapshot = self.service.get_settings()
        self.assertNotIn("smtpPassword", snapshot)
        self.assertTrue(snapshot["hasSmtpPassword"])
        with self.repository.connection(readonly=True) as connection:
            stored = connection.execute("SELECT smtp_password_ciphertext FROM email_settings").fetchone()[0]
        self.assertNotIn("top-secret-password", stored)
        self.service.save_settings({**snapshot, "smtpPassword": "", "fromName": "Updated Radar"})
        with self.repository.connection(readonly=True) as connection:
            preserved = connection.execute("SELECT smtp_password_ciphertext FROM email_settings").fetchone()[0]
        self.assertEqual(stored, preserved)

    def test_successful_digest_sends_new_job_once(self) -> None:
        self.add_job("job-new")
        with patch.object(self.service, "send_email") as send:
            first = self.service.send_new_jobs_digest(trigger_type="manual")
            second = self.service.send_new_jobs_digest(trigger_type="manual")
        self.assertEqual(first["status"], "Success")
        self.assertEqual(first["jobCount"], 1)
        self.assertEqual(second["status"], "Skipped - No New Jobs")
        self.assertEqual(send.call_count, 1)
        self.assertEqual([item["status"] for item in self.service.history()], ["Skipped - No New Jobs", "Success"])

    def test_failed_digest_keeps_job_pending(self) -> None:
        self.add_job("job-pending")
        with patch.object(self.service, "send_email", side_effect=EmailDeliveryError("Sanitized failure")):
            failed = self.service.send_new_jobs_digest(trigger_type="scheduled")
        self.assertEqual(failed["status"], "Failed")
        self.assertEqual(len(self.service.pending_jobs(self.service.get_settings()["trackingStartedAt"])), 1)
        retry_service = EmailService(self.repository)
        with patch.object(retry_service, "send_email"):
            retried = retry_service.send_new_jobs_digest(trigger_type="manual")
        self.assertEqual(retried["status"], "Success")
        self.assertEqual(retried["jobCount"], 1)

    def test_disabled_scheduled_digest_does_not_send(self) -> None:
        current = self.service.get_settings()
        self.service.save_settings({**current, "dailyEnabled": False, "smtpPassword": ""})
        self.add_job("job-disabled")
        with patch.object(self.service, "send_email") as send:
            result = self.service.send_new_jobs_digest(trigger_type="scheduled")
        self.assertEqual(result["status"], "Disabled")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
