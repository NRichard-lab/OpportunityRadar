from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from backend.backup_restore import (
    BACKUP_DATABASE_NAME,
    BACKUP_MANIFEST_NAME,
    BackupRestoreError,
    create_sqlite_backup,
    inspect_sqlite_database,
    restore_sqlite_backup,
    sha256_file,
    validate_sqlite_backup,
)
from backend.cli import main as cli_main
from backend.db import connect, initialize_schema
from backend.repository import OpportunityRepository
from backend.utility_tasks import UtilityCancelled, create_backup as create_utility_backup


class SQLiteBackupRestoreTests(unittest.TestCase):
    def test_online_backup_captures_committed_wal_and_records_bounded_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "opportunity_radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)

            writer = connect(database)
            try:
                insert_synthetic_job(writer, "job-synthetic-second", "Synthetic Support Engineer")
                writer.commit()
                self.assertTrue(database.with_name(database.name + "-wal").exists())

                result = create_sqlite_backup(
                    database,
                    backup_root,
                    deployment_version="a" * 40,
                    retention_count=3,
                )
            finally:
                writer.close()

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["schemaVersion"], 6)
            self.assertEqual(result["tableCounts"]["companies"], 1)
            self.assertEqual(result["tableCounts"]["jobs"], 2)
            artifact = Path(result["backupDirectory"])
            self.assertTrue(artifact.name.startswith("backup_"))
            self.assertEqual(list(backup_root.glob("*.pending")), [])

            validated = validate_sqlite_backup(artifact)
            self.assertEqual(validated["status"], "success")
            self.assertEqual(validated["tableCounts"]["jobs"], 2)
            manifest_text = (artifact / BACKUP_MANIFEST_NAME).read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["database"]["sha256"], result["sha256"])
            self.assertEqual(manifest["validation"]["integrityCheck"], "ok")
            self.assertEqual(manifest["validation"]["foreignKeyViolations"], 0)
            self.assertEqual(manifest["validation"]["schemaVersion"], 6)
            self.assertEqual(manifest["deploymentVersion"], "a" * 40)
            self.assertNotIn("Synthetic resume-derived content", manifest_text)
            self.assertNotIn("Synthetic application note", manifest_text)

    def test_retention_prunes_only_after_success_and_keeps_unknown_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)

            first = create_sqlite_backup(
                database, backup_root, deployment_version="release-1", retention_count=10
            )
            unknown = backup_root / "backup_unknown_or_damaged"
            unknown.mkdir()
            (unknown / BACKUP_MANIFEST_NAME).write_text("{}", encoding="utf-8")

            second = create_sqlite_backup(
                database, backup_root, deployment_version="release-2", retention_count=2
            )
            third = create_sqlite_backup(
                database, backup_root, deployment_version="release-3", retention_count=2
            )

            self.assertFalse(Path(first["backupDirectory"]).exists())
            self.assertTrue(Path(second["backupDirectory"]).exists())
            self.assertTrue(Path(third["backupDirectory"]).exists())
            self.assertTrue(unknown.exists())
            known_good = [
                child
                for child in backup_root.iterdir()
                if child.is_dir() and child.name.startswith("backup_") and child != unknown
            ]
            self.assertEqual(len(known_good), 2)
            for artifact in known_good:
                self.assertEqual(validate_sqlite_backup(artifact)["status"], "success")

    def test_failed_new_backup_retains_prior_known_good_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            first = create_sqlite_backup(
                database, backup_root, deployment_version="release-good", retention_count=1
            )

            with patch(
                "backend.backup_restore._perform_online_backup",
                side_effect=sqlite3.OperationalError("synthetic destination failure"),
            ):
                with self.assertRaisesRegex(BackupRestoreError, "previous backups were retained"):
                    create_sqlite_backup(
                        database,
                        backup_root,
                        deployment_version="release-failed",
                        retention_count=1,
                    )

            self.assertTrue(Path(first["backupDirectory"]).exists())
            self.assertEqual(validate_sqlite_backup(Path(first["backupDirectory"]))["status"], "success")
            self.assertEqual(list(backup_root.glob(".*.pending")), [])

    def test_restore_round_trip_preserves_current_database_and_returns_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            baseline = create_sqlite_backup(
                database, backup_root, deployment_version="release-baseline", retention_count=7
            )
            source_hash_before = sha256_file(Path(baseline["databaseFile"]))

            with closing(connect(database)) as connection:
                insert_synthetic_job(connection, "job-after-backup", "Synthetic Changed Job")
                connection.commit()
            self.assertEqual(count_rows(database, "jobs"), 2)

            restored = restore_sqlite_backup(
                Path(baseline["backupDirectory"]),
                database,
                backup_root,
                deployment_version="release-current",
                writes_stopped=True,
            )

            self.assertEqual(restored["status"], "success")
            self.assertEqual(restored["writesStoppedGuard"], "operator-confirmed")
            self.assertEqual(restored["tableCounts"]["jobs"], 1)
            self.assertEqual(count_rows(database, "jobs"), 1)
            self.assertEqual(inspect_sqlite_database(database, immutable=False)["schemaVersion"], 6)
            self.assertEqual(sha256_file(Path(baseline["databaseFile"])), source_hash_before)
            self.assertEqual(sha256_file(database), source_hash_before)

            preserved = restored["preservedCurrent"]
            self.assertIsNotNone(preserved)
            preserved_validation = validate_sqlite_backup(Path(preserved["backupDirectory"]))
            self.assertEqual(preserved_validation["tableCounts"]["jobs"], 2)
            self.assertEqual(list(database.parent.glob(".*.restore-*.tmp")), [])

    def test_restore_requires_explicit_writes_stopped_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="guard-test", retention_count=7
            )
            before = sha256_file(database)

            with self.assertRaisesRegex(BackupRestoreError, "stop all application writes"):
                restore_sqlite_backup(
                    Path(backup["backupDirectory"]),
                    database,
                    backup_root,
                    deployment_version="guard-test",
                    writes_stopped=False,
                )

            self.assertEqual(sha256_file(database), before)

    def test_restore_rejects_final_component_symlink_without_touching_referent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="symlink-test", retention_count=7
            )
            with closing(connect(database)) as connection:
                connection.execute(
                    "UPDATE jobs SET title='Referent must remain unchanged' WHERE id='job-synthetic'"
                )
                connection.commit()
            link = database.with_name("restore-link.db")
            try:
                link.symlink_to(database)
            except OSError as exc:
                self.skipTest(f"Symbolic links are unavailable: {exc}")

            with self.assertRaisesRegex(BackupRestoreError, "symbolic link"):
                restore_sqlite_backup(
                    Path(backup["backupDirectory"]),
                    link,
                    backup_root,
                    deployment_version="symlink-test",
                    writes_stopped=True,
                )

            self.assertTrue(link.is_symlink())
            self.assertEqual(read_job_title(database), "Referent must remain unchanged")

    def test_restore_refuses_to_downgrade_a_valid_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="schema-6", retention_count=7
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = 7")
                connection.commit()

            with self.assertRaisesRegex(BackupRestoreError, "schema versions differ"):
                restore_sqlite_backup(
                    Path(backup["backupDirectory"]),
                    database,
                    backup_root,
                    deployment_version="schema-7-current",
                    writes_stopped=True,
                    expected_schema_version=6,
                )

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_corrupt_backup_is_retained_and_never_replaces_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="corruption-test", retention_count=7
            )
            backup_database = Path(backup["databaseFile"])
            corrupt_bytes = b"synthetic-corrupt-backup"
            backup_database.write_bytes(corrupt_bytes)
            current_hash = sha256_file(database)

            with self.assertRaises(BackupRestoreError):
                restore_sqlite_backup(
                    Path(backup["backupDirectory"]),
                    database,
                    backup_root,
                    deployment_version="corruption-test",
                    writes_stopped=True,
                )

            self.assertEqual(backup_database.read_bytes(), corrupt_bytes)
            self.assertEqual(sha256_file(database), current_hash)
            self.assertEqual(count_rows(database, "jobs"), 1)

    def test_corrupt_current_database_is_preserved_for_diagnosis_before_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="known-good", retention_count=7
            )
            corrupt_bytes = b"synthetic-corrupt-current-database"
            database.write_bytes(corrupt_bytes)
            stale_wal = b"synthetic-stale-wal-for-diagnosis"
            stale_shm = b"synthetic-stale-shm-for-diagnosis"
            database.with_name(database.name + "-wal").write_bytes(stale_wal)
            database.with_name(database.name + "-shm").write_bytes(stale_shm)

            restored = restore_sqlite_backup(
                Path(backup["backupDirectory"]),
                database,
                backup_root,
                deployment_version="repair-release",
                writes_stopped=True,
            )

            self.assertEqual(restored["status"], "success")
            self.assertFalse(restored["preservedCurrentWasValid"])
            diagnostic = restored["preservedCurrent"]
            self.assertEqual(diagnostic["status"], "diagnostic-unvalidated")
            diagnostic_dir = Path(diagnostic["backupDirectory"])
            self.assertEqual((diagnostic_dir / database.name).read_bytes(), corrupt_bytes)
            self.assertEqual((diagnostic_dir / (database.name + "-wal")).read_bytes(), stale_wal)
            self.assertEqual((diagnostic_dir / (database.name + "-shm")).read_bytes(), stale_shm)
            current_wal = database.with_name(database.name + "-wal")
            current_shm = database.with_name(database.name + "-shm")
            if current_wal.exists():
                self.assertNotEqual(current_wal.read_bytes(), stale_wal)
            if current_shm.exists():
                self.assertNotEqual(current_shm.read_bytes(), stale_shm)
            self.assertEqual(count_rows(database, "jobs"), 1)

    def test_missing_target_with_stale_same_count_wal_restores_validated_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="stale-target-wal", retention_count=7
            )

            writer = sqlite3.connect(database)
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    "UPDATE jobs SET title='STALE WAL OVERRIDE' WHERE id='job-synthetic'"
                )
                writer.commit()
                wal_bytes = database.with_name(database.name + "-wal").read_bytes()
                shm_bytes = database.with_name(database.name + "-shm").read_bytes()
            finally:
                writer.close()

            database.unlink(missing_ok=True)
            database.with_name(database.name + "-wal").write_bytes(wal_bytes)
            database.with_name(database.name + "-shm").write_bytes(shm_bytes)
            restored = restore_sqlite_backup(
                Path(backup["backupDirectory"]),
                database,
                backup_root,
                deployment_version="stale-target-wal",
                writes_stopped=True,
            )

            self.assertEqual(restored["status"], "success")
            self.assertEqual(read_job_title(database), "Synthetic Operations Analyst")
            self.assertIsNotNone(restored["rawPreservedCurrent"])

    def test_post_switch_validation_failure_rolls_back_exact_prior_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="rollback-test", retention_count=7
            )
            with closing(connect(database)) as connection:
                connection.execute(
                    "UPDATE jobs SET title='Synthetic current state' WHERE id='job-synthetic'"
                )
                connection.commit()

            from backend import backup_restore as module

            real_verify = module._verify_inspection_against_manifest
            calls = 0

            def fail_activated_validation(inspection, manifest):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise BackupRestoreError("synthetic post-switch validation failure")
                return real_verify(inspection, manifest)

            with patch(
                "backend.backup_restore._verify_inspection_against_manifest",
                side_effect=fail_activated_validation,
            ):
                with self.assertRaisesRegex(BackupRestoreError, "prior database state was restored"):
                    restore_sqlite_backup(
                        Path(backup["backupDirectory"]),
                        database,
                        backup_root,
                        deployment_version="rollback-test",
                        writes_stopped=True,
                    )

            self.assertEqual(read_job_title(database), "Synthetic current state")
            self.assertEqual(inspect_sqlite_database(database, immutable=False)["schemaVersion"], 6)

    def test_manifest_count_tampering_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="manifest-test", retention_count=7
            )
            manifest_path = Path(backup["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["tableCounts"]["jobs"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(BackupRestoreError, "record counts"):
                validate_sqlite_backup(Path(backup["backupDirectory"]))

    def test_added_backup_wal_is_rejected_even_when_it_keeps_record_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="wal-tamper-test", retention_count=7
            )
            backup_database = Path(backup["databaseFile"])
            manifest_hash = backup["sha256"]

            writer = sqlite3.connect(backup_database)
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.execute(
                    "UPDATE jobs SET title='Synthetic WAL-tampered title' WHERE id='job-synthetic'"
                )
                writer.commit()
                self.assertEqual(sha256_file(backup_database), manifest_hash)
                self.assertTrue(backup_database.with_name(backup_database.name + "-wal").exists())

                with self.assertRaisesRegex(BackupRestoreError, "unexpected SQLite sidecar"):
                    validate_sqlite_backup(Path(backup["backupDirectory"]))
                with self.assertRaisesRegex(BackupRestoreError, "unexpected SQLite sidecar"):
                    restore_sqlite_backup(
                        Path(backup["backupDirectory"]),
                        database,
                        backup_root,
                        deployment_version="wal-tamper-test",
                        writes_stopped=True,
                    )
            finally:
                writer.close()

            self.assertEqual(read_job_title(database), "Synthetic Operations Analyst")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not enforced on Windows")
    def test_backup_artifact_and_restored_database_use_private_posix_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            auxiliary = root / "synthetic-private-key"
            create_synthetic_database(database)
            auxiliary.write_bytes(b"synthetic-only")
            auxiliary.chmod(0o644)

            backup = create_sqlite_backup(
                database,
                backup_root,
                deployment_version="permissions-test",
                retention_count=7,
                additional_files={"synthetic-private-key": auxiliary},
            )
            artifact = Path(backup["backupDirectory"])
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o700)
            for protected_file in (
                artifact / BACKUP_DATABASE_NAME,
                artifact / BACKUP_MANIFEST_NAME,
                artifact / "synthetic-private-key",
            ):
                self.assertEqual(stat.S_IMODE(protected_file.stat().st_mode), 0o600)

            restore_sqlite_backup(
                artifact,
                database,
                backup_root,
                deployment_version="permissions-test",
                writes_stopped=True,
            )
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_cli_emits_machine_readable_backup_and_validation_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)

            output = io.StringIO()
            with redirect_stdout(output):
                status = cli_main(
                    [
                        "backup-sqlite",
                        "--database",
                        str(database),
                        "--backup-dir",
                        str(backup_root),
                        "--deployment-version",
                        "cli-synthetic",
                        "--retain",
                        "2",
                    ]
                )
            self.assertEqual(status, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "success")

            output = io.StringIO()
            with redirect_stdout(output):
                status = cli_main(
                    ["validate-sqlite-backup", "--backup", report["backupDirectory"]]
                )
            self.assertEqual(status, 0)
            validation = json.loads(output.getvalue())
            self.assertEqual(validation["action"], "validate-backup")
            self.assertNotIn("manifest", validation)

    def test_existing_utility_action_uses_validated_sqlite_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            progress_events: list[tuple[int, int, str]] = []
            snapshots = {
                "master_path": root / "snapshots" / "master.xlsx",
                "companies_json_path": root / "snapshots" / "companies.json",
                "jobs_json_path": root / "snapshots" / "jobs.json",
                "applications_json_path": root / "snapshots" / "applications.json",
            }
            for path in snapshots.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic compatibility snapshot")
            database.with_name(".email_secret.key").write_bytes(b"synthetic-test-key")
            exporter = SimpleNamespace(
                **snapshots,
                export_all=lambda **_kwargs: {"companies": 1, "jobs": 1, "applications": 1},
            )

            report = create_utility_backup(
                OpportunityRepository(database),
                exporter,
                backup_root,
                lambda current, total, item: progress_events.append((current, total, item)),
                Event(),
            )

            self.assertEqual(report["status"], "success")
            self.assertEqual(report["filesBackedUp"], 6)
            self.assertEqual(progress_events, [(1, 1, database.name)])
            validated = validate_sqlite_backup(Path(report["backupDirectory"]))
            self.assertEqual(validated["status"], "success")
            self.assertEqual(
                set(validated["auxiliaryFiles"]),
                {"master.xlsx", "companies.json", "jobs.json", "applications.json", ".email_secret.key"},
            )

    def test_utility_rechecks_cancellation_after_snapshot_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            create_synthetic_database(database)
            cancelled = Event()
            progress_events: list[tuple[int, int, str]] = []

            def export_then_cancel(**_kwargs):
                cancelled.set()
                return {"companies": 1, "jobs": 1, "applications": 1}

            exporter = SimpleNamespace(export_all=export_then_cancel)
            with self.assertRaises(UtilityCancelled):
                create_utility_backup(
                    OpportunityRepository(database),
                    exporter,
                    root / "backups",
                    lambda current, total, item: progress_events.append((current, total, item)),
                    cancelled,
                )

            self.assertEqual(progress_events, [])
            self.assertFalse((root / "backups").exists())

    def test_utility_does_not_report_completion_when_validated_backup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            create_synthetic_database(database)
            snapshots = {
                "master_path": root / "master.xlsx",
                "companies_json_path": root / "companies.json",
                "jobs_json_path": root / "jobs.json",
                "applications_json_path": root / "applications.json",
            }
            exporter = SimpleNamespace(
                **snapshots,
                export_all=lambda **_kwargs: {"companies": 1, "jobs": 1, "applications": 1},
            )
            progress_events: list[tuple[int, int, str]] = []

            with patch(
                "backend.utility_tasks.create_sqlite_backup",
                side_effect=BackupRestoreError("synthetic validated backup failure"),
            ):
                with self.assertRaises(BackupRestoreError):
                    create_utility_backup(
                        OpportunityRepository(database),
                        exporter,
                        root / "backups",
                        lambda current, total, item: progress_events.append((current, total, item)),
                        Event(),
                    )

            self.assertEqual(progress_events, [])

    def test_raw_pre_restore_files_are_fsynced_before_artifact_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "database" / "radar.db"
            backup_root = root / "backups"
            create_synthetic_database(database)
            backup = create_sqlite_backup(
                database, backup_root, deployment_version="raw-fsync-test", retention_count=7
            )
            before = sha256_file(database)

            from backend import backup_restore as module

            real_fsync_file = module._fsync_file

            def fail_raw_fileset_fsync(path):
                if Path(path).parent.name.startswith(".pre_restore_diagnostic_"):
                    raise OSError("synthetic raw-fileset fsync failure")
                return real_fsync_file(path)

            with patch(
                "backend.backup_restore._fsync_file", side_effect=fail_raw_fileset_fsync
            ):
                with self.assertRaisesRegex(
                    BackupRestoreError, "current database could not be preserved"
                ):
                    restore_sqlite_backup(
                        Path(backup["backupDirectory"]),
                        database,
                        backup_root,
                        deployment_version="raw-fsync-test",
                        writes_stopped=True,
                    )

            self.assertEqual(sha256_file(database), before)
            self.assertEqual(list(backup_root.glob("pre_restore_diagnostic_*")), [])
            self.assertEqual(list(backup_root.glob(".pre_restore_diagnostic_*.pending")), [])

    @unittest.skipIf(os.name == "nt", "Windows does not expose portable directory fsync")
    def test_posix_directory_fsync_failure_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            from backend import backup_restore as module

            with patch("backend.backup_restore.os.fsync", side_effect=OSError("synthetic I/O")):
                with self.assertRaisesRegex(BackupRestoreError, "durably flushed"):
                    module._fsync_directory(Path(temp_dir))


def create_synthetic_database(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database)) as connection:
        initialize_schema(connection)
        now = "2026-08-27T12:00:00+00:00"
        connection.execute(
            "INSERT INTO companies (id,name,city,state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (
                "company-synthetic",
                "Synthetic Signal Cooperative",
                "Example City",
                "EX",
                now,
                now,
            ),
        )
        insert_synthetic_job(connection, "job-synthetic", "Synthetic Operations Analyst")
        connection.execute(
            """INSERT INTO applications
            (job_id,applied,application_status,date_applied,notes,updated_at)
            VALUES (?,1,'Applied',?,'Synthetic application note',?)""",
            ("job-synthetic", "2026-08-27", now),
        )
        connection.execute(
            """INSERT INTO resumes
            (id,version,name,file_name,uploaded_at,extracted_text,skills_json,payload_json,updated_at)
            VALUES ('current','synthetic-v1','Synthetic Candidate','synthetic-resume.txt',?, ?, ?, ?, ?)""",
            (
                now,
                "Synthetic resume-derived content for backup testing only.",
                '["synthetic-analysis"]',
                '{"fixture":true}',
                now,
            ),
        )
        connection.execute(
            """INSERT INTO resume_fit_results
            (id,resume_id,job_id,score,status,resume_version,job_fingerprint,
             algorithm_version,matched_at,payload_json,created_at)
            VALUES ('fit-synthetic','current','job-synthetic',75,'Matched','synthetic-v1',
                    'synthetic-fingerprint','synthetic-algorithm',?,'{"fixture":true}',?)""",
            (now, now),
        )
        connection.execute(
            "INSERT INTO settings (key,value_json,updated_at) VALUES ('synthetic_preference','true',?)",
            (now,),
        )
        connection.commit()


def insert_synthetic_job(connection: sqlite3.Connection, job_id: str, title: str) -> None:
    now = "2026-08-27T12:00:00+00:00"
    connection.execute(
        """INSERT INTO jobs
        (id,legacy_id,company_id,company_name,title,location,description,
         description_snippet,collected_at,first_seen_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id,
            job_id,
            "company-synthetic",
            "Synthetic Signal Cooperative",
            title,
            "Remote Synthetic",
            "Synthetic job content used only for backup and restore verification.",
            "Synthetic job content.",
            now,
            now,
            now,
            now,
        ),
    )


def count_rows(database: Path, table: str) -> int:
    if table not in {"companies", "jobs", "applications", "resumes", "settings"}:
        raise ValueError("Unsupported test table.")
    with closing(sqlite3.connect(database)) as connection:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def read_job_title(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        return str(
            connection.execute("SELECT title FROM jobs WHERE id='job-synthetic'").fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
