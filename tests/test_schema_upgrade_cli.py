from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli import main as cli_main
from backend.db import connect, initialize_schema


class SchemaUpgradeCliTests(unittest.TestCase):
    def test_email_upgrade_preserves_and_converts_legacy_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "opportunity_radar.db"
            with closing(connect(database)) as connection:
                initialize_schema(connection)
                connection.execute(
                    """INSERT INTO email_settings
                    (id,recipient_email,daily_enabled,send_after_refresh,tracking_started_at,updated_at)
                    VALUES ('default','legacy@example.test',1,1,'2026-08-01','2026-08-01')"""
                )
                connection.execute("DROP TABLE email_digest_job_changes")
                connection.execute("DROP TABLE email_snapshot_jobs")
                for column in (
                    "schedule_days_json", "schedule_time", "schedule_timezone", "recipients_json",
                    "last_scheduled_date", "checkpoint_established_at", "last_successful_at",
                ):
                    connection.execute(f"ALTER TABLE email_settings DROP COLUMN {column}")
                for column in ("recipients_json", "added_count", "removed_count", "scheduled_for"):
                    connection.execute(f"ALTER TABLE email_digests DROP COLUMN {column}")
                connection.execute("PRAGMA user_version = 6")
                connection.commit()

            code, report = run_cli("upgrade-schema", "--database", str(database))
            self.assertEqual(code, 0)
            self.assertEqual(report, {"status": "completed", "schemaVersion": 7})
            with closing(connect(database, readonly=True)) as connection:
                row = connection.execute(
                    "SELECT recipients_json,send_after_refresh FROM email_settings WHERE id='default'"
                ).fetchone()
                tables = {
                    item[0] for item in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(json.loads(row["recipients_json"]), ["legacy@example.test"])
            self.assertEqual(row["send_after_refresh"], 0)
            self.assertTrue({"email_snapshot_jobs", "email_digest_job_changes"}.issubset(tables))

    def test_upgrade_is_transactional_idempotent_and_preserves_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "opportunity_radar.db"
            with closing(connect(database)) as connection:
                initialize_schema(connection)
                connection.execute(
                    """INSERT INTO companies (id,name,notes,created_at,updated_at)
                    VALUES ('company-existing','Existing Bank','keep me','2000-01-01','2000-01-01')"""
                )
                connection.commit()
                connection.execute("DROP INDEX idx_companies_normalized_name_unique")
                connection.execute("ALTER TABLE companies DROP COLUMN normalized_name")
                connection.execute("ALTER TABLE companies DROP COLUMN company_description")
                connection.commit()

            with patch(
                "backend.db.backfill_company_normalized_names",
                side_effect=RuntimeError("simulated schema upgrade failure"),
            ):
                code, failed = run_cli("upgrade-schema", "--database", str(database))
            self.assertEqual(code, 1)
            self.assertEqual(failed["status"], "failed")
            with closing(connect(database, readonly=True)) as connection:
                failed_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(companies)")
                }
            self.assertNotIn("normalized_name", failed_columns)
            self.assertNotIn("company_description", failed_columns)

            code, report = run_cli("upgrade-schema", "--database", str(database))
            self.assertEqual(code, 0)
            self.assertEqual(report, {"status": "completed", "schemaVersion": 7})
            with closing(connect(database, readonly=True)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(companies)")}
                stored = connection.execute(
                    "SELECT id,name,notes,normalized_name,company_description FROM companies",
                ).fetchone()
                index = connection.execute(
                    """SELECT 1 FROM sqlite_master
                    WHERE type='index' AND name='idx_companies_normalized_name_unique'""",
                ).fetchone()
            self.assertTrue({"normalized_name", "company_description"}.issubset(columns))
            self.assertEqual(
                tuple(stored),
                ("company-existing", "Existing Bank", "keep me", "existing bank", ""),
            )
            self.assertIsNotNone(index)

            second_code, second_report = run_cli(
                "upgrade-schema", "--database", str(database),
            )
            self.assertEqual(second_code, 0)
            self.assertEqual(second_report, report)

    def test_upgrade_refuses_to_create_a_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing.db"
            code, report = run_cli("upgrade-schema", "--database", str(database))
            self.assertEqual(code, 1)
            self.assertEqual(report["status"], "failed")
            self.assertFalse(database.exists())


def run_cli(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(list(arguments))
    return code, json.loads(output.getvalue())


if __name__ == "__main__":
    unittest.main()
