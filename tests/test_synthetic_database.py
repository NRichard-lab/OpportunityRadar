from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.create_synthetic_database import create_synthetic_database


class SyntheticDatabaseTests(unittest.TestCase):
    def test_fixture_contains_only_bounded_synthetic_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "database" / "opportunity_radar.db"

            result = create_synthetic_database(database)

            self.assertEqual(result["status"], "created")
            self.assertTrue(result["synthetic"])
            self.assertEqual(result["integrityCheck"], "ok")
            self.assertEqual(result["schemaVersion"], 6)
            self.assertEqual(
                result["counts"],
                {"companies": 2, "jobs": 2, "applications": 1, "resumes": 1, "settings": 2},
            )
            with closing(sqlite3.connect(database)) as connection:
                company_names = [row[0] for row in connection.execute("SELECT name FROM companies")]
                domains = [row[0] for row in connection.execute("SELECT official_website FROM companies")]
                marker = connection.execute(
                    "SELECT value_json FROM settings WHERE key='phase2.synthetic_fixture'"
                ).fetchone()
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

            self.assertTrue(all(name.startswith("Synthetic ") for name in company_names))
            self.assertTrue(all(".example.invalid" in value for value in domains))
            self.assertEqual(marker, ("true",))

    def test_fixture_generator_never_replaces_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "opportunity_radar.db"
            create_synthetic_database(database)

            with self.assertRaises(FileExistsError):
                create_synthetic_database(database)


if __name__ == "__main__":
    unittest.main()
