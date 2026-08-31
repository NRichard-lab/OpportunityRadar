from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.email_scheduler import EmailScheduler
from backend.email_service import EmailService
from backend.operation_gate import MutationGate
from backend.repository import OpportunityRepository


class EmailSchedulerTests(unittest.TestCase):
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
                "smtpPassword": "secret",
                "fromEmail": "radar@example.test",
                "enabled": True,
                "recipients": ["recipient@example.test"],
                "scheduleDays": ["wednesday"],
                "scheduleTime": "07:00",
                "scheduleTimezone": "America/Denver",
            }
        )
        self.gate = MutationGate()
        self.scheduler = EmailScheduler(
            lambda: EmailService(self.repository),
            mutation_gate=self.gate,
            poll_seconds=0.01,
        )
        self.due_time = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.scheduler.stop()
        self.temporary.cleanup()

    def test_due_email_waits_for_refresh_then_claims_once_across_restart(self) -> None:
        refresh_lease = self.gate.acquire("refresh-all-job-listings")
        waiting = self.scheduler.run_due_once(self.due_time)
        self.assertEqual(waiting["status"], "Waiting for active refresh")
        self.assertEqual(self.service.history(), [])
        self.assertNotEqual(self.service.get_settings()["lastScheduledDate"], "2026-09-02")

        refresh_lease.release()
        with patch.object(EmailService, "send_email") as send:
            completed = self.scheduler.run_due_once(self.due_time)
        send.assert_not_called()
        self.assertEqual(completed["status"], "Baseline Established")
        self.assertEqual(self.service.get_settings()["lastScheduledDate"], "2026-09-02")

        restarted = EmailScheduler(
            lambda: EmailService(self.repository), mutation_gate=MutationGate()
        )
        self.assertIsNone(restarted.run_due_once(self.due_time))
        self.assertEqual(len(self.service.history()), 1)

    def test_schedule_obeys_day_and_time(self) -> None:
        before_time = datetime(2026, 9, 2, 12, 59, tzinfo=timezone.utc)
        wrong_day = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.assertIsNone(self.scheduler.run_due_once(before_time))
        self.assertIsNone(self.scheduler.run_due_once(wrong_day))
        self.assertEqual(self.service.history(), [])


if __name__ == "__main__":
    unittest.main()
