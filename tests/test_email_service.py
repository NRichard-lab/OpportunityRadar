import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.email_service import (
    EmailConfigurationError,
    EmailDeliveryError,
    EmailService,
)
from backend.repository import OpportunityRepository


class EmailServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = OpportunityRepository(
            Path(self.temporary.name) / "opportunity_radar.db", initialize=True
        )
        self.service = EmailService(self.repository)
        self.service.save_settings(
            {
                "smtpHost": "smtp.example.test",
                "smtpPort": 465,
                "security": "ssl_tls",
                "smtpUsername": "radar@example.test",
                "smtpPassword": "top-secret-password",
                "fromEmail": "radar@example.test",
                "fromName": "Opportunity Radar",
                "enabled": True,
                "recipients": ["first@example.test", "second@example.test"],
                "scheduleDays": ["monday", "wednesday", "friday"],
                "scheduleTime": "07:00",
                "scheduleTimezone": "America/Denver",
            }
        )
        self.company = self.repository.create_company({"name": "Digest Company"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_job(
        self,
        job_id: str,
        *,
        title: str = "Systems Engineer",
        pay_text: str = "",
        pay_min: int | None = None,
        pay_max: int | None = None,
    ) -> None:
        self.repository.upsert_jobs(
            [
                {
                    "id": job_id,
                    "companyId": self.company["id"],
                    "companyName": self.company["name"],
                    "title": title,
                    "payText": pay_text,
                    "payMin": pay_min,
                    "payMax": pay_max,
                    "payPeriod": "year",
                    "sourceUrl": f"https://employer.example.test/jobs/{job_id}",
                    "status": "Open",
                }
            ]
        )

    def establish_baseline(self) -> dict[str, object]:
        with patch.object(self.service, "send_email") as send:
            result = self.service.send_job_digest(trigger_type="manual")
        send.assert_not_called()
        self.assertEqual(result["status"], "Baseline Established")
        return result

    def test_password_is_encrypted_hidden_and_blank_update_preserves_it(self) -> None:
        snapshot = self.service.get_settings()
        self.assertNotIn("smtpPassword", snapshot)
        self.assertTrue(snapshot["hasSmtpPassword"])
        with self.repository.connection(readonly=True) as connection:
            stored = connection.execute(
                "SELECT smtp_password_ciphertext FROM email_settings"
            ).fetchone()[0]
        self.assertNotIn("top-secret-password", stored)
        self.service.save_settings({**snapshot, "smtpPassword": "", "fromName": "Updated Radar"})
        with self.repository.connection(readonly=True) as connection:
            preserved = connection.execute(
                "SELECT smtp_password_ciphertext FROM email_settings"
            ).fetchone()[0]
        self.assertEqual(stored, preserved)

    def test_recipient_list_add_remove_validation_and_restart_persistence(self) -> None:
        saved = self.service.get_settings()
        self.assertEqual(saved["recipients"], ["first@example.test", "second@example.test"])
        self.service.save_settings(
            {**saved, "recipients": ["second@example.test"], "smtpPassword": ""}
        )
        restarted = EmailService(self.repository)
        self.assertEqual(restarted.get_settings()["recipients"], ["second@example.test"])
        self.assertEqual(restarted.get_settings()["scheduleDays"], ["monday", "wednesday", "friday"])
        self.assertEqual(restarted.get_settings()["scheduleTime"], "07:00")

        for recipients, message in (
            (["same@example.test", "SAME@example.test"], "Duplicate recipient"),
            (["not-an-email"], "valid recipient"),
            ([""], "cannot be blank"),
        ):
            with self.subTest(recipients=recipients):
                with self.assertRaisesRegex(EmailConfigurationError, message):
                    restarted.save_settings(
                        {**restarted.get_settings(), "recipients": recipients, "smtpPassword": ""}
                    )

    def test_first_run_establishes_baseline_and_zero_changes_skip(self) -> None:
        self.add_job("job-existing")
        self.establish_baseline()
        restarted = EmailService(self.repository)
        self.assertTrue(restarted.get_settings()["checkpointEstablishedAt"])
        self.assertEqual(set(restarted.snapshot_jobs()), {"id:job-existing"})
        with patch.object(restarted, "send_email") as send:
            result = restarted.send_job_digest(trigger_type="scheduled")
        send.assert_not_called()
        self.assertEqual(result["status"], "Skipped - No Changes")
        self.assertEqual(
            [item["status"] for item in restarted.history()],
            ["Skipped - No Changes", "Baseline Established"],
        )

    def test_digest_reports_added_removed_pay_and_external_links_once(self) -> None:
        self.add_job("job-stays")
        self.add_job("job-removed", title="Former Analyst", pay_text="$80,000 posted")
        self.establish_baseline()
        with self.repository.connection() as connection:
            connection.execute("DELETE FROM jobs WHERE id='job-removed'")
        self.add_job("job-paid", title="Senior Administrator", pay_min=95_000, pay_max=125_000)
        self.add_job("job-unpaid", title="Network Engineer")

        with patch.object(self.service, "send_email") as send:
            result = self.service.send_job_digest(trigger_type="manual")
            second = self.service.send_job_digest(trigger_type="manual")

        self.assertEqual(result["status"], "Sent")
        self.assertEqual(result["addedCount"], 2)
        self.assertEqual(result["removedCount"], 1)
        self.assertEqual(second["status"], "Skipped - No Changes")
        send.assert_called_once()
        _settings, recipients, subject, plain, html = send.call_args.args
        self.assertEqual(recipients, ["first@example.test", "second@example.test"])
        self.assertIn("2 Added, 1 Removed", subject)
        self.assertIn("Digest Company", plain)
        self.assertIn("Senior Administrator", plain)
        self.assertIn("$95,000 - $125,000 per year", plain)
        self.assertIn("Pay not posted", plain)
        self.assertIn("Former Analyst", plain)
        self.assertIn("Original Posting: https://employer.example.test/jobs/job-removed", plain)
        self.assertIn('href="https://employer.example.test/jobs/job-paid"', html)
        self.assertIn("View Job", html)
        self.assertIn("Original Posting", html)
        self.assertNotIn("OpportunityRadar/jobs", html)
        self.assertEqual(
            set(self.service.snapshot_jobs()),
            {"id:job-stays", "id:job-paid", "id:job-unpaid"},
        )

    def test_non_identifying_job_change_does_not_repeat_job(self) -> None:
        self.add_job("job-stable", pay_text="$90,000")
        self.establish_baseline()
        self.add_job("job-stable", pay_text="$100,000")
        with patch.object(self.service, "send_email") as send:
            result = self.service.send_job_digest(trigger_type="manual")
        self.assertEqual(result["status"], "Skipped - No Changes")
        send.assert_not_called()

    def test_failed_send_does_not_advance_checkpoint_and_success_does(self) -> None:
        self.add_job("job-baseline")
        self.establish_baseline()
        checkpoint_before = self.service.get_settings()["lastSuccessfulAt"]
        snapshot_before = self.service.snapshot_jobs()
        self.add_job("job-pending")
        with patch.object(
            self.service, "send_email", side_effect=EmailDeliveryError("Sanitized failure")
        ):
            failed = self.service.send_job_digest(trigger_type="scheduled")
        self.assertEqual(failed["status"], "Failed")
        self.assertEqual(self.service.snapshot_jobs(), snapshot_before)
        self.assertEqual(self.service.get_settings()["lastSuccessfulAt"], checkpoint_before)

        restarted = EmailService(self.repository)
        with patch.object(restarted, "send_email"):
            retried = restarted.send_job_digest(trigger_type="manual")
        self.assertEqual(retried["status"], "Sent")
        self.assertEqual(retried["addedCount"], 1)
        self.assertIn("id:job-pending", restarted.snapshot_jobs())
        self.assertTrue(restarted.get_settings()["lastSuccessfulAt"])

    def test_disabled_scheduled_digest_does_not_send(self) -> None:
        current = self.service.get_settings()
        self.service.save_settings({**current, "enabled": False, "smtpPassword": ""})
        self.add_job("job-disabled")
        with patch.object(self.service, "send_email") as send:
            result = self.service.send_job_digest(trigger_type="scheduled")
        self.assertEqual(result["status"], "Disabled")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
