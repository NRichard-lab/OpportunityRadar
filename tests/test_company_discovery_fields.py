import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.job_board_discovery import DiscoveryResult


class CompanyDiscoveryFieldsTest(unittest.TestCase):
    def setUp(self):
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(tempfile.gettempdir()) / "company_discovery_fields_test.db"
        database.DATABASE_PATH.unlink(missing_ok=True)
        database.initialize_database()
        with database.get_connection() as conn:
            conn.execute(
                """UPDATE companies SET careers_page_url=?, verified_job_board_url=?,
                   job_board_type='Workday', discovery_status='Verified',
                   classification_confidence='High', discovery_method=?,
                   last_verified_at='2026-08-22T12:00:00+00:00', needs_manual_refresh=0
                   WHERE name='BECU'""",
                ("https://www.becu.org/careers", "https://becu.wd1.myworkdayjobs.com/External", "Followed Careers → Apply link"),
            )
            conn.execute(
                """UPDATE companies SET careers_page_url=?, verified_job_board_url=?,
                   job_board_type='ADP', discovery_status='Verified',
                   classification_confidence='High', discovery_method=?,
                   last_verified_at='2026-08-22T12:00:00+00:00', needs_manual_refresh=0
                   WHERE name='WECU'""",
                ("https://www.wecu.com/about-wecu/careers/", "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=wecu", "Followed Careers → Current Openings link"),
            )
            conn.execute(
                """INSERT INTO companies(name,company_website,careers_page_url,discovery_status)
                   VALUES('Bellco','https://www.bellco.org','https://www.bellco.org/careers','Needs Review')"""
            )

    def tearDown(self):
        database.DATABASE_PATH.unlink(missing_ok=True)
        database.DATABASE_PATH = self.original_path

    def test_sqlite_and_api_keep_becu_wecu_bellco_fields_separate(self):
        bellco = DiscoveryResult(
            company_website="https://www.bellco.org",
            careers_page_url="https://www.bellco.org/careers",
            job_board_url="https://www.bellco.org/careers/openings",
            platform="Self-Hosted / In-House",
            job_board_type="Self-Hosted / In-House",
            classification_confidence="High",
            discovery_method="Classified Careers Page → Self-Hosted / In-House (6 public job links)",
        )
        from fastapi.testclient import TestClient
        from backend.main import app

        with database.get_connection() as conn:
            bellco_id = conn.execute("SELECT id FROM companies WHERE name='Bellco'").fetchone()["id"]
        with patch("backend.main.discover_job_board", return_value=bellco):
            with TestClient(app) as client:
                refreshed = client.post(f"/api/companies/{bellco_id}/refresh-discovery")
                self.assertEqual(refreshed.status_code, 200)
                api_rows = {row["name"]: row for row in client.get("/api/companies").json()}

        expected = {
            "BECU": ("Workday", "Verified"),
            "WECU": ("ADP", "Verified"),
            "Bellco": ("Self-Hosted / In-House", "Verified"),
        }
        with database.get_connection() as conn:
            sql_rows = {row["name"]: dict(row) for row in conn.execute("SELECT * FROM companies")}
        for name, (board_type, status) in expected.items():
            for row in (sql_rows[name], api_rows[name]):
                self.assertEqual(row["job_board_type"], board_type)
                self.assertEqual(row["discovery_status"], status)
                self.assertEqual(row["classification_confidence"], "High")
                self.assertTrue(row["verified_job_board_url"])
                self.assertFalse(row["needs_manual_refresh"])
            self.assertEqual(sql_rows[name]["verified_job_board_url"], api_rows[name]["verified_job_board_url"])
            self.assertNotIn("job_board_url", api_rows[name])
            self.assertNotIn("search_status", api_rows[name])
            self.assertNotIn("source_refresh_required", api_rows[name])

    def test_verified_company_is_stable_during_normal_refresh_selection(self):
        with database.get_connection() as conn:
            before = dict(conn.execute("SELECT * FROM companies WHERE name='BECU'").fetchone())
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as client:
            after = next(row for row in client.get("/api/companies").json() if row["name"] == "BECU")
        for field in ("verified_job_board_url", "job_board_type", "discovery_status", "classification_confidence", "discovery_method", "last_verified_at"):
            self.assertEqual(after[field], before[field])


if __name__ == "__main__":
    unittest.main()
