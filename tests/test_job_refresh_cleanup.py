import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.job_collection import Candidate, run_collection


class JobRefreshCleanupTest(unittest.TestCase):
    def setUp(self):
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(tempfile.gettempdir()) / "job_refresh_cleanup_test.db"
        database.DATABASE_PATH.unlink(missing_ok=True)
        database.initialize_database()
        with database.get_connection() as conn:
            conn.execute("DELETE FROM applications")
            conn.execute("DELETE FROM jobs")
            conn.execute("UPDATE companies SET discovery_status='Verified', verified_job_board_url='https://example.wd1.myworkdayjobs.com/External', job_board_type='Workday' WHERE id=1")
            self.stale_id = self._insert_job(conn, "Stale role", "stale-1")
            self.tracked_id = self._insert_job(conn, "Tracked stale role", "tracked-1")
            self._insert_job(conn, "Current role", "current-1")
            conn.execute(
                "INSERT INTO applications(job_id,status,applied_date,notes) VALUES(?,?,?,?)",
                (self.tracked_id, "Interview", "2026-08-01", "Keep this application history"),
            )

    def _insert_job(self, conn, title, external_id):
        url = f"https://jobs.example/{external_id}"
        key = Candidate(title, url, external_job_id=external_id).dedupe_key
        return conn.execute(
            "INSERT INTO jobs(company_id,title,location,detail_url,external_job_id,dedupe_key) VALUES(1,?,?,?,?,?)",
            (title, "Seattle, WA", url, external_id, key),
        ).lastrowid

    def tearDown(self):
        database.DATABASE_PATH.unlink(missing_ok=True)
        database.DATABASE_PATH = self.original_path

    def test_complete_refresh_upserts_and_safely_cleans_stale_jobs(self):
        candidates = [
            Candidate("Current role updated", "https://jobs.example/current-1", location="Seattle, WA", external_job_id="current-1"),
            Candidate("New role", "https://jobs.example/new-1", location="Bellevue, WA", external_job_id="new-1"),
        ]
        with patch("backend.job_collection.collect_candidates", return_value=("Workday", False, candidates, [], True)):
            report = run_collection(1)

        self.assertEqual(report["saved_count"], 1)
        self.assertEqual(report["updated_count"], 1)
        self.assertEqual(report["removed_count"], 1)
        self.assertEqual(report["tracked_no_longer_posted_count"], 1)
        self.assertTrue(report["collection_complete"])
        with database.get_connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM jobs WHERE id=?", (self.stale_id,)).fetchone())
            tracked = conn.execute("SELECT status FROM jobs WHERE id=?", (self.tracked_id,)).fetchone()
            application = conn.execute("SELECT status,applied_date,notes FROM applications WHERE job_id=?", (self.tracked_id,)).fetchone()
        self.assertEqual(tracked["status"], "No Longer Posted")
        self.assertEqual(dict(application), {"status": "Interview", "applied_date": "2026-08-01", "notes": "Keep this application history"})

        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as client:
            active_jobs = client.get("/api/jobs").json()
            applications = client.get("/api/applications").json()
        self.assertNotIn(self.tracked_id, {job["id"] for job in active_jobs})
        self.assertIn(self.tracked_id, {application["job_id"] for application in applications})

    def test_incomplete_refresh_never_removes_stale_jobs(self):
        with patch("backend.job_collection.collect_candidates", return_value=("Generic", False, [], ["Pagination unclear"], False)):
            report = run_collection(1)
        self.assertFalse(report["collection_complete"])
        self.assertEqual(report["removed_count"], 0)
        self.assertEqual(report["tracked_no_longer_posted_count"], 0)
        with database.get_connection() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM jobs WHERE id=?", (self.stale_id,)).fetchone())
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id=?", (self.tracked_id,)).fetchone()["status"], "Open")


if __name__ == "__main__":
    unittest.main()
