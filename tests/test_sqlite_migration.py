from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.db import connect
from backend.migration import apply_migration, build_migration_plan
from excel_tools import write_results


class SQLiteMigrationTests(unittest.TestCase):
    def test_preview_is_read_only_and_apply_backs_up_imports_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "data" / "opportunity_radar.db"
            create_fixture(root)
            source_hash = sha256(root / "data" / "master.xlsx")

            plan = build_migration_plan(root, database)
            self.assertFalse(database.exists())
            self.assertFalse((root / "data" / "exports").exists())
            self.assertEqual(plan.report["counts"]["source"]["companies"], 1)
            self.assertEqual(plan.report["counts"]["source"]["jobs"], 2)
            self.assertEqual(plan.report["counts"]["findings"]["stableIdConflicts"], 1)
            self.assertEqual(plan.report["counts"]["actions"]["jobs"]["update-id"], 1)

            report = apply_migration(root, database)
            self.assertTrue(report["validation"]["passed"])
            self.assertTrue(database.exists())
            self.assertEqual(list((root / "data").glob("*.migrating-*")), [])
            self.assertEqual(sha256(root / "data" / "master.xlsx"), source_hash)
            manifest = json.loads(Path(report["manifest"]).read_text())
            self.assertTrue(any(item["path"] == "data\\master.xlsx" or item["path"] == "data/master.xlsx" for item in manifest["files"]))
            self.assertTrue(Path(report["reportJson"]).exists())
            self.assertTrue(Path(report["reportXlsx"]).exists())

            with closing(connect(database, readonly=True)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM utility_runs").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"resumes", "resume_fit_results", "settings", "import_history"}.issubset(names))


def create_fixture(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "output").mkdir()
    (root / "frontend" / "public" / "data").mkdir(parents=True)
    write_results(root / "data" / "master.xlsx", [{
        "Company ID": "company-stable", "Company Name": "Stable Bank", "Industry": "Financial Services",
        "Country": "United States", "Official Website": "https://stable.example",
    }])
    companies = [{"id": "company-stable", "name": "Stable Bank"}]
    jobs = [
        {"id": "job-collision", "companyId": "company-stable", "companyName": "Stable Bank", "title": "Analyst", "sourceUrl": "https://jobs/1"},
        {"id": "job-collision", "companyId": "company-stable", "companyName": "Stable Bank", "title": "Manager", "sourceUrl": "https://jobs/2"},
    ]
    write_json(root / "data" / "companies.json", companies)
    write_json(root / "data" / "jobs.json", jobs)
    write_json(root / "data" / "applications.json", [])
    write_json(root / "frontend" / "public" / "data" / "companies.json", companies)
    write_json(root / "frontend" / "public" / "data" / "jobs.json", jobs)
    write_json(root / "logs" / "rejected_job_candidates.json", [{"companyName": "Stable Bank", "candidateText": "Bad link"}])
    write_json(root / "logs" / "job_collection_diagnostics.json", [{"status": "Completed"}])


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
