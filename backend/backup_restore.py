from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.file_security import atomic_write_text
from backend.health import CORE_TABLES, MINIMUM_SCHEMA_VERSION, REQUIRED_CORE_COLUMNS


BACKUP_FORMAT_VERSION = 1
BACKUP_DATABASE_NAME = "opportunity_radar.db"
BACKUP_MANIFEST_NAME = "manifest.json"
DEFAULT_RETENTION_COUNT = 7
MAX_COUNTED_TABLES = 64
MAX_ROWS_PER_TABLE_COUNT = 1_000_000
MAX_AUXILIARY_FILES = 32
MAX_INTEGRITY_ERRORS = 10
MAX_FOREIGN_KEY_ERRORS = 20
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class BackupRestoreError(RuntimeError):
    """A safe, operator-facing backup or restore failure."""


def create_sqlite_backup(
    database_path: Path,
    backup_root: Path,
    *,
    deployment_version: str,
    retention_count: int | None = DEFAULT_RETENTION_COUNT,
    kind: str = "backup",
    additional_files: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Create and validate an atomic SQLite backup artifact.

    The database is read with SQLite's online backup API so committed WAL content is
    included. A directory is made visible under its final name only after the copied
    database and manifest have both passed validation.
    """
    source = Path(database_path).resolve(strict=False)
    if not source.is_file():
        raise BackupRestoreError("The SQLite source database does not exist or is not a file.")
    if retention_count is not None and retention_count < 1:
        raise BackupRestoreError("Backup retention must keep at least one known-good backup.")
    if kind not in {"backup", "pre_restore"}:
        raise BackupRestoreError("Unsupported backup artifact kind.")
    auxiliary_sources = _prepare_auxiliary_sources(additional_files or {})
    if any(
        auxiliary_source == source
        or auxiliary_source in {
            source.with_name(source.name + "-wal"),
            source.with_name(source.name + "-shm"),
        }
        for _, auxiliary_source in auxiliary_sources
    ):
        raise BackupRestoreError("The live SQLite database cannot be an auxiliary backup file.")

    root = Path(backup_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise BackupRestoreError("The configured backup location is not a directory.")

    created_at = _utc_now()
    token = uuid4().hex[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    final_directory = root / f"{kind}_{stamp}_{token}"
    pending_directory = root / f".{kind}_{stamp}_{token}.pending"
    pending_database = pending_directory / BACKUP_DATABASE_NAME
    completed = False

    try:
        pending_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=False, exist_ok=False)
        _set_private_directory_mode(pending_directory)
        _perform_online_backup(source, pending_database)
        _set_private_file_mode(pending_database)
        inspection = inspect_sqlite_database(pending_database, immutable=True)
        database_hash = sha256_file(pending_database)
        auxiliary_entries: list[dict[str, Any]] = []
        for name, auxiliary_source in auxiliary_sources:
            destination = pending_directory / name
            shutil.copy2(auxiliary_source, destination)
            _set_private_file_mode(destination)
            _fsync_file(destination)
            auxiliary_entries.append(
                {
                    "file": name,
                    "bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
            )
        manifest = {
            "formatVersion": BACKUP_FORMAT_VERSION,
            "status": "complete",
            "kind": kind,
            "createdAt": created_at,
            "deploymentVersion": _bounded_version(deployment_version),
            "database": {
                "file": BACKUP_DATABASE_NAME,
                "bytes": pending_database.stat().st_size,
                "sha256": database_hash,
            },
            "validation": {
                "integrityCheck": "ok",
                "foreignKeyViolations": 0,
                "schemaVersion": inspection["schemaVersion"],
                "schemaFingerprint": inspection["schemaFingerprint"],
                "coreSchema": "valid",
            },
            "tableCounts": inspection["tableCounts"],
            "tableCountsCapped": inspection["tableCountsCapped"],
            "tableCountsTruncated": inspection["tableCountsTruncated"],
            "auxiliaryFiles": auxiliary_entries,
        }
        atomic_write_text(
            pending_directory / BACKUP_MANIFEST_NAME,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        _set_private_file_mode(pending_directory / BACKUP_MANIFEST_NAME)
        _fsync_file(pending_database)
        _fsync_directory(pending_directory)
        os.replace(pending_directory, final_directory)
        completed = True
        _fsync_directory(root)
    except (OSError, sqlite3.Error, ValueError, BackupRestoreError) as exc:
        raise BackupRestoreError("SQLite backup did not complete; previous backups were retained.") from exc
    finally:
        if not completed and pending_directory.exists():
            _remove_incomplete_directory(pending_directory, root)

    retention = {"status": "not-requested", "kept": None, "removed": 0}
    if kind == "backup" and retention_count is not None:
        retention = prune_known_good_backups(root, keep=retention_count)

    return {
        "status": "success",
        "action": "backup",
        "backupDirectory": str(final_directory),
        "databaseFile": str(final_directory / BACKUP_DATABASE_NAME),
        "manifest": str(final_directory / BACKUP_MANIFEST_NAME),
        "createdAt": created_at,
        "deploymentVersion": _bounded_version(deployment_version),
        "schemaVersion": inspection["schemaVersion"],
        "tableCounts": inspection["tableCounts"],
        "tableCountsCapped": inspection["tableCountsCapped"],
        "sha256": database_hash,
        "auxiliaryFiles": [entry["file"] for entry in auxiliary_entries],
        "filesBackedUp": 1 + len(auxiliary_entries),
        "retention": retention,
    }


def validate_sqlite_backup(backup_directory: Path) -> dict[str, Any]:
    """Validate a managed backup without changing it."""
    artifact = Path(backup_directory).resolve(strict=False)
    if not artifact.is_dir():
        raise BackupRestoreError("The backup artifact directory does not exist.")
    manifest_path = artifact / BACKUP_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupRestoreError("The backup manifest is missing or invalid.") from exc
    _validate_manifest_shape(manifest)

    database_path = artifact / BACKUP_DATABASE_NAME
    if not database_path.is_file():
        raise BackupRestoreError("The backup database is missing.")
    if any(
        database_path.with_name(database_path.name + suffix).exists()
        for suffix in ("-wal", "-shm")
    ):
        raise BackupRestoreError("The backup artifact contains an unexpected SQLite sidecar.")
    expected_database = manifest["database"]
    try:
        actual_size = database_path.stat().st_size
        actual_hash = sha256_file(database_path)
    except OSError as exc:
        raise BackupRestoreError("The backup database could not be read.") from exc
    if actual_size != expected_database["bytes"] or actual_hash != expected_database["sha256"]:
        raise BackupRestoreError("The backup database does not match its manifest.")
    for entry in manifest["auxiliaryFiles"]:
        auxiliary_path = artifact / entry["file"]
        try:
            auxiliary_matches = (
                auxiliary_path.is_file()
                and auxiliary_path.stat().st_size == entry["bytes"]
                and sha256_file(auxiliary_path) == entry["sha256"]
            )
        except OSError as exc:
            raise BackupRestoreError("A backup auxiliary file could not be read.") from exc
        if not auxiliary_matches:
            raise BackupRestoreError("A backup auxiliary file does not match its manifest.")

    inspection = inspect_sqlite_database(database_path, immutable=True)
    expected_validation = manifest["validation"]
    if inspection["schemaVersion"] != expected_validation["schemaVersion"]:
        raise BackupRestoreError("The backup schema version does not match its manifest.")
    if inspection["schemaFingerprint"] != expected_validation["schemaFingerprint"]:
        raise BackupRestoreError("The backup schema does not match its manifest.")
    if inspection["tableCounts"] != manifest["tableCounts"]:
        raise BackupRestoreError("The backup record counts do not match its manifest.")
    if inspection["tableCountsCapped"] != manifest["tableCountsCapped"]:
        raise BackupRestoreError("The backup bounded count state does not match its manifest.")
    if inspection["tableCountsTruncated"] != manifest["tableCountsTruncated"]:
        raise BackupRestoreError("The backup count summary does not match its manifest.")

    return {
        "status": "success",
        "action": "validate-backup",
        "backupDirectory": str(artifact),
        "databaseFile": str(database_path),
        "createdAt": manifest["createdAt"],
        "deploymentVersion": manifest["deploymentVersion"],
        "schemaVersion": inspection["schemaVersion"],
        "tableCounts": inspection["tableCounts"],
        "tableCountsCapped": inspection["tableCountsCapped"],
        "auxiliaryFiles": [entry["file"] for entry in manifest["auxiliaryFiles"]],
        "sha256": actual_hash,
        "manifest": manifest,
    }


def restore_sqlite_backup(
    backup_directory: Path,
    database_path: Path,
    backup_root: Path,
    *,
    deployment_version: str,
    writes_stopped: bool,
    expected_schema_version: int = MINIMUM_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Validate, stage, preserve, and atomically activate a managed backup.

    ``writes_stopped`` is deliberately mandatory. The caller must stop all application
    processes that can open the target database before making that assertion.
    """
    if not writes_stopped:
        raise BackupRestoreError(
            "Restore refused: stop all application writes and pass the explicit confirmation guard."
        )
    if expected_schema_version < MINIMUM_SCHEMA_VERSION:
        raise BackupRestoreError("The expected schema version is below the supported production schema.")

    validated_source = validate_sqlite_backup(backup_directory)
    source_database = Path(validated_source["databaseFile"]).resolve(strict=True)
    if validated_source["schemaVersion"] != expected_schema_version:
        raise BackupRestoreError("The restore source has an unexpected schema version.")

    requested_target = Path(database_path)
    if requested_target.is_symlink():
        raise BackupRestoreError("The restore target must not be a symbolic link.")
    target = requested_target.resolve(strict=False)
    if target == source_database:
        raise BackupRestoreError("Restore source and target database must be different files.")
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise BackupRestoreError("The restore target must be a regular SQLite database file.")
    if not target.parent.is_dir():
        raise BackupRestoreError("The restore target directory does not exist.")

    root = Path(backup_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.restore-{uuid4().hex}.tmp"
    target_existed = target.exists()
    preserved: dict[str, Any] | None = None
    raw_preserved: dict[str, Any] | None = None
    current_was_valid = False
    switched = False
    quarantined_sidecars: list[tuple[Path, Path]] = []
    retained_sidecar_quarantine = False
    activated: dict[str, Any] | None = None

    try:
        _perform_online_backup(source_database, stage, immutable_source=True)
        _set_private_file_mode(stage)
        staged_inspection = inspect_sqlite_database(stage, immutable=True)
        _verify_inspection_against_manifest(staged_inspection, validated_source["manifest"])
        if sha256_file(stage) != validated_source["sha256"]:
            raise BackupRestoreError("The staged restore bytes do not match the validated backup.")
        if staged_inspection["schemaVersion"] != expected_schema_version:
            raise BackupRestoreError("The staged restore has an unexpected schema version.")

        if target_existed or _sqlite_sidecars_exist(target):
            # Preserve the stopped raw fileset before any SQLite connection can
            # rebuild or otherwise touch a stale shared-memory sidecar.
            raw_preserved = _preserve_unvalidated_database(
                target,
                root,
                deployment_version,
                reason="pre-restore-raw-fileset",
            )
        if target_existed:
            try:
                current_inspection = inspect_sqlite_database(target, immutable=False)
                current_was_valid = True
            except BackupRestoreError:
                current_inspection = None

            if current_inspection is not None:
                if current_inspection["schemaVersion"] != expected_schema_version:
                    raise BackupRestoreError(
                        "Restore refused because the current and backup schema versions differ."
                    )
                preserved = create_sqlite_backup(
                    target,
                    root,
                    deployment_version=deployment_version,
                    retention_count=None,
                    kind="pre_restore",
                )
                _checkpoint_target_for_switch(target)
            else:
                preserved = raw_preserved
        elif raw_preserved is not None:
            # Orphaned sidecars are not attached to the new database, but are kept
            # as a diagnostic fileset and restored if activation must roll back.
            preserved = raw_preserved

        # Sidecars must be quarantined even when the base DB is missing. A stale WAL
        # with the same schema/counts could otherwise override validated row values.
        quarantined_sidecars = _quarantine_sidecars(target)

        _fsync_file(stage)
        os.replace(stage, target)
        switched = True
        _fsync_directory(target.parent)
        if sha256_file(target) != validated_source["sha256"]:
            raise BackupRestoreError("The activated restore bytes do not match the validated backup.")
        activated = inspect_sqlite_database(target, immutable=True)
        _verify_inspection_against_manifest(activated, validated_source["manifest"])
        for _, quarantine in quarantined_sidecars:
            try:
                quarantine.unlink(missing_ok=True)
            except OSError:
                # It no longer has a canonical SQLite sidecar name and therefore
                # cannot be attached to the restored DB. Retain it for diagnosis.
                retained_sidecar_quarantine = True
    except (OSError, sqlite3.Error, ValueError, BackupRestoreError) as exc:
        if switched:
            try:
                _rollback_failed_activation(
                    target,
                    target_existed=target_existed,
                    raw_preserved=raw_preserved,
                    quarantined_sidecars=quarantined_sidecars,
                )
            except (OSError, BackupRestoreError) as rollback_exc:
                raise BackupRestoreError(
                    "Restore activation failed and automatic rollback could not complete; "
                    "the pre-restore fileset was retained for manual recovery."
                ) from rollback_exc
            raise BackupRestoreError(
                "Restore activation validation failed; the prior database state was restored."
            ) from exc
        if isinstance(exc, BackupRestoreError):
            raise
        raise BackupRestoreError("SQLite restore failed before a validated database could be activated.") from exc
    finally:
        if not switched:
            _restore_quarantined_sidecars(quarantined_sidecars)
        stage.unlink(missing_ok=True)
        stage.with_name(stage.name + "-wal").unlink(missing_ok=True)
        stage.with_name(stage.name + "-shm").unlink(missing_ok=True)

    if activated is None:
        raise BackupRestoreError("Restore activation did not produce a validation result.")
    return {
        "status": "success",
        "action": "restore",
        "databaseFile": str(target),
        "sourceBackupDirectory": str(Path(backup_directory).resolve(strict=False)),
        "sourceSha256": validated_source["sha256"],
        "preservedCurrent": preserved,
        "rawPreservedCurrent": raw_preserved,
        "preservedCurrentWasValid": current_was_valid,
        "schemaVersion": activated["schemaVersion"],
        "tableCounts": activated["tableCounts"],
        "tableCountsCapped": activated["tableCountsCapped"],
        "writesStoppedGuard": "operator-confirmed",
        "retainedSidecarQuarantine": retained_sidecar_quarantine,
    }


def inspect_sqlite_database(database_path: Path, *, immutable: bool) -> dict[str, Any]:
    """Run bounded integrity, FK, schema, and row-count checks."""
    path = Path(database_path).resolve(strict=False)
    if not path.is_file():
        raise BackupRestoreError("The SQLite database does not exist or is not a file.")

    connection: sqlite3.Connection | None = None
    try:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        connection = sqlite3.connect(
            f"{path.as_uri()}?{query}", uri=True, isolation_level=None, timeout=5.0
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")

        integrity_rows = connection.execute(
            f"PRAGMA integrity_check({MAX_INTEGRITY_ERRORS})"
        ).fetchall()
        if integrity_rows != [("ok",)]:
            raise BackupRestoreError("SQLite integrity validation failed.")
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchmany(
            MAX_FOREIGN_KEY_ERRORS + 1
        )
        if foreign_key_rows:
            raise BackupRestoreError("SQLite foreign-key validation failed.")

        version_row = connection.execute("PRAGMA user_version").fetchone()
        schema_version = int(version_row[0]) if version_row else 0
        if schema_version < MINIMUM_SCHEMA_VERSION:
            raise BackupRestoreError("SQLite schema version is not production-ready.")

        table_names = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        if not CORE_TABLES.issubset(table_names):
            raise BackupRestoreError("SQLite core schema tables are missing.")
        _verify_core_columns(connection)

        counted_names = table_names[:MAX_COUNTED_TABLES]
        table_counts: dict[str, int] = {}
        capped_tables: list[str] = []
        for table in counted_names:
            observed = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM (SELECT 1 FROM {_quote_identifier(table)} "
                    f"LIMIT {MAX_ROWS_PER_TABLE_COUNT + 1})"
                ).fetchone()[0]
            )
            if observed > MAX_ROWS_PER_TABLE_COUNT:
                table_counts[table] = MAX_ROWS_PER_TABLE_COUNT
                capped_tables.append(table)
            else:
                table_counts[table] = observed
        schema_fingerprint = _schema_fingerprint(connection)
        return {
            "schemaVersion": schema_version,
            "schemaFingerprint": schema_fingerprint,
            "tableCounts": table_counts,
            "tableCountsCapped": capped_tables,
            "tableCountsTruncated": len(table_names) > MAX_COUNTED_TABLES,
            "totalUserTables": len(table_names),
        }
    except BackupRestoreError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BackupRestoreError("SQLite validation could not be completed.") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def prune_known_good_backups(backup_root: Path, *, keep: int) -> dict[str, Any]:
    """Remove only old, fully validated regular backups, never the sole good one."""
    if keep < 1:
        raise BackupRestoreError("Backup retention must keep at least one known-good backup.")
    root = Path(backup_root).resolve(strict=False)
    if not root.is_dir():
        raise BackupRestoreError("The configured backup location is not a directory.")

    valid: list[tuple[str, Path]] = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir() or not child.name.startswith("backup_"):
            continue
        try:
            resolved = child.resolve(strict=True)
            if resolved.parent != root:
                continue
            result = validate_sqlite_backup(resolved)
            valid.append((str(result["createdAt"]), resolved))
        except (OSError, BackupRestoreError):
            # Unknown or damaged artifacts are retained for diagnosis.
            continue

    valid.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    removed = 0
    cleanup_failed = False
    for _, candidate in valid[keep:]:
        if candidate.parent != root or not candidate.name.startswith("backup_"):
            continue
        try:
            shutil.rmtree(candidate)
            removed += 1
        except OSError:
            cleanup_failed = True
    return {
        "status": "warning" if cleanup_failed else "success",
        "kept": len(valid) - removed,
        "knownGoodBeforePrune": len(valid),
        "removed": removed,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _perform_online_backup(
    source_path: Path, destination_path: Path, *, immutable_source: bool = False
) -> None:
    if destination_path.exists():
        raise BackupRestoreError("The SQLite backup destination already exists.")
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source_query = "mode=ro&immutable=1" if immutable_source else "mode=ro"
        source = sqlite3.connect(
            f"{Path(source_path).resolve().as_uri()}?{source_query}",
            uri=True,
            isolation_level=None,
            timeout=10.0,
        )
        source.execute("PRAGMA query_only = ON")
        source.execute("PRAGMA busy_timeout = 10000")
        descriptor = os.open(
            destination_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0),
            PRIVATE_FILE_MODE,
        )
        os.close(descriptor)
        _set_private_file_mode(destination_path)
        destination = sqlite3.connect(destination_path, isolation_level=None, timeout=10.0)
        source.backup(destination, pages=256, sleep=0.05)
        destination.commit()
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _verify_core_columns(connection: sqlite3.Connection) -> None:
    for table, required_columns in REQUIRED_CORE_COLUMNS.items():
        actual_columns = {
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            ).fetchall()
        }
        if not required_columns.issubset(actual_columns):
            raise BackupRestoreError("SQLite core schema columns are missing.")


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = [
        [str(value or "") for value in row]
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('index','table','trigger','view') "
            "ORDER BY type,name"
        ).fetchall()
    ]
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_manifest_shape(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise BackupRestoreError("The backup manifest is invalid.")
    if manifest.get("formatVersion") != BACKUP_FORMAT_VERSION or manifest.get("status") != "complete":
        raise BackupRestoreError("The backup manifest format or completion state is invalid.")
    if manifest.get("kind") not in {"backup", "pre_restore"}:
        raise BackupRestoreError("The backup manifest kind is invalid.")
    if not isinstance(manifest.get("createdAt"), str) or not manifest["createdAt"]:
        raise BackupRestoreError("The backup manifest timestamp is invalid.")
    if not isinstance(manifest.get("deploymentVersion"), str):
        raise BackupRestoreError("The backup deployment version is invalid.")

    database = manifest.get("database")
    if not isinstance(database, dict) or database.get("file") != BACKUP_DATABASE_NAME:
        raise BackupRestoreError("The backup manifest database entry is invalid.")
    if not isinstance(database.get("bytes"), int) or database["bytes"] < 1:
        raise BackupRestoreError("The backup manifest database size is invalid.")
    digest = database.get("sha256")
    if not _is_sha256(digest):
        raise BackupRestoreError("The backup manifest database digest is invalid.")

    validation = manifest.get("validation")
    if not isinstance(validation, dict):
        raise BackupRestoreError("The backup manifest validation entry is invalid.")
    if validation.get("integrityCheck") != "ok" or validation.get("foreignKeyViolations") != 0:
        raise BackupRestoreError("The backup manifest does not record successful validation.")
    if not isinstance(validation.get("schemaVersion"), int):
        raise BackupRestoreError("The backup manifest schema version is invalid.")
    fingerprint = validation.get("schemaFingerprint")
    if not _is_sha256(fingerprint):
        raise BackupRestoreError("The backup manifest schema fingerprint is invalid.")
    if validation.get("coreSchema") != "valid":
        raise BackupRestoreError("The backup manifest core schema state is invalid.")

    counts = manifest.get("tableCounts")
    if not isinstance(counts, dict) or len(counts) > MAX_COUNTED_TABLES:
        raise BackupRestoreError("The backup manifest table-count summary is invalid.")
    if any(
        not isinstance(table, str)
        or not table
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for table, count in counts.items()
    ):
        raise BackupRestoreError("The backup manifest table-count values are invalid.")
    capped = manifest.get("tableCountsCapped")
    if (
        not isinstance(capped, list)
        or len(capped) > MAX_COUNTED_TABLES
        or len(set(capped)) != len(capped)
        or any(not isinstance(table, str) or table not in counts for table in capped)
    ):
        raise BackupRestoreError("The backup manifest bounded count state is invalid.")
    if not isinstance(manifest.get("tableCountsTruncated"), bool):
        raise BackupRestoreError("The backup manifest table-count boundary is invalid.")

    auxiliary = manifest.get("auxiliaryFiles")
    if not isinstance(auxiliary, list) or len(auxiliary) > MAX_AUXILIARY_FILES:
        raise BackupRestoreError("The backup manifest auxiliary-file summary is invalid.")
    names: set[str] = set()
    for entry in auxiliary:
        if not isinstance(entry, dict):
            raise BackupRestoreError("The backup manifest auxiliary-file entry is invalid.")
        name = entry.get("file")
        if (
            not isinstance(name, str)
            or not _is_safe_artifact_name(name)
            or name in {BACKUP_DATABASE_NAME, BACKUP_MANIFEST_NAME}
            or name in names
        ):
            raise BackupRestoreError("The backup manifest auxiliary-file name is invalid.")
        names.add(name)
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] < 0:
            raise BackupRestoreError("The backup manifest auxiliary-file size is invalid.")
        if not _is_sha256(entry.get("sha256")):
            raise BackupRestoreError("The backup manifest auxiliary-file digest is invalid.")


def _verify_inspection_against_manifest(inspection: dict[str, Any], manifest: dict[str, Any]) -> None:
    validation = manifest["validation"]
    if inspection["schemaVersion"] != validation["schemaVersion"]:
        raise BackupRestoreError("The staged restore schema version does not match the backup.")
    if inspection["schemaFingerprint"] != validation["schemaFingerprint"]:
        raise BackupRestoreError("The staged restore schema does not match the backup.")
    if inspection["tableCounts"] != manifest["tableCounts"]:
        raise BackupRestoreError("The staged restore record counts do not match the backup.")
    if inspection["tableCountsCapped"] != manifest["tableCountsCapped"]:
        raise BackupRestoreError("The staged restore bounded count state does not match the backup.")
    if inspection["tableCountsTruncated"] != manifest["tableCountsTruncated"]:
        raise BackupRestoreError("The staged restore count summary does not match the backup.")


def _checkpoint_target_for_switch(target: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=2.0,
        )
        connection.execute("PRAGMA busy_timeout = 2000")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise BackupRestoreError(
                "Restore refused because the target database is still busy; application writes may not be stopped."
            )
    except BackupRestoreError:
        raise
    except sqlite3.Error as exc:
        raise BackupRestoreError("The target database could not be prepared for an atomic switch.") from exc
    finally:
        if connection is not None:
            connection.close()


def _quarantine_sidecars(target: Path) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    token = uuid4().hex
    try:
        for suffix in ("-wal", "-shm"):
            sidecar = target.with_name(target.name + suffix)
            if not sidecar.exists():
                continue
            quarantine = target.parent / f".{target.name}{suffix}.restore-quarantine-{token}"
            os.replace(sidecar, quarantine)
            _set_private_file_mode(quarantine)
            moved.append((sidecar, quarantine))
    except OSError as exc:
        _restore_quarantined_sidecars(moved)
        raise BackupRestoreError(
            "Restore refused because a SQLite sidecar is still in use; application writes may not be stopped."
        ) from exc
    return moved


def _sqlite_sidecars_exist(target: Path) -> bool:
    return any(
        target.with_name(target.name + suffix).exists()
        for suffix in ("-wal", "-shm")
    )


def _rollback_failed_activation(
    target: Path,
    *,
    target_existed: bool,
    raw_preserved: dict[str, Any] | None,
    quarantined_sidecars: list[tuple[Path, Path]],
) -> None:
    # Detach any sidecars produced by the failed activation before putting the
    # exact stopped pre-restore fileset back under canonical SQLite names.
    failed_sidecars = _quarantine_sidecars(target)
    for _, quarantine in failed_sidecars:
        quarantine.unlink(missing_ok=True)

    preserved_directory = (
        Path(str(raw_preserved["backupDirectory"]))
        if raw_preserved is not None
        else None
    )
    rollback_temporary: Path | None = None
    try:
        if target_existed:
            if preserved_directory is None:
                raise BackupRestoreError("The pre-restore database fileset is unavailable.")
            preserved_database = preserved_directory / target.name
            if not preserved_database.is_file():
                raise BackupRestoreError("The preserved pre-restore database is unavailable.")
            rollback_temporary = target.parent / f".{target.name}.rollback-{uuid4().hex}.tmp"
            shutil.copy2(preserved_database, rollback_temporary)
            _set_private_file_mode(rollback_temporary)
            _fsync_file(rollback_temporary)
            os.replace(rollback_temporary, target)
            rollback_temporary = None
        else:
            target.unlink(missing_ok=True)

        if preserved_directory is not None:
            for suffix in ("-wal", "-shm"):
                preserved_sidecar = preserved_directory / (target.name + suffix)
                if not preserved_sidecar.is_file():
                    continue
                restored_sidecar = target.with_name(target.name + suffix)
                shutil.copy2(preserved_sidecar, restored_sidecar)
                _set_private_file_mode(restored_sidecar)
                _fsync_file(restored_sidecar)

        for _, quarantine in quarantined_sidecars:
            quarantine.unlink(missing_ok=True)
        _fsync_directory(target.parent)
    finally:
        if rollback_temporary is not None:
            rollback_temporary.unlink(missing_ok=True)


def _restore_quarantined_sidecars(moved: list[tuple[Path, Path]]) -> None:
    for original, quarantine in reversed(moved):
        if not quarantine.exists() or original.exists():
            continue
        try:
            os.replace(quarantine, original)
        except OSError:
            # The original DB has not been switched. Keeping the uniquely named
            # quarantine is safer than deleting an unrecoverable sidecar.
            pass


def _preserve_unvalidated_database(
    target: Path,
    backup_root: Path,
    deployment_version: str,
    *,
    reason: str,
) -> dict[str, Any]:
    created_at = _utc_now()
    token = uuid4().hex[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    final_directory = backup_root / f"pre_restore_diagnostic_{stamp}_{token}"
    pending_directory = backup_root / f".pre_restore_diagnostic_{stamp}_{token}.pending"
    complete = False
    try:
        pending_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=False, exist_ok=False)
        _set_private_directory_mode(pending_directory)
        files: list[dict[str, Any]] = []
        for source in (
            target,
            target.with_name(target.name + "-wal"),
            target.with_name(target.name + "-shm"),
        ):
            if not source.is_file():
                continue
            destination = pending_directory / source.name
            shutil.copy2(source, destination)
            _set_private_file_mode(destination)
            _fsync_file(destination)
            files.append(
                {
                    "file": source.name,
                    "bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
            )
        if not files:
            raise BackupRestoreError("The current database could not be preserved before restore.")
        atomic_write_text(
            pending_directory / BACKUP_MANIFEST_NAME,
            json.dumps(
                {
                    "formatVersion": BACKUP_FORMAT_VERSION,
                    "status": "diagnostic-unvalidated",
                    "kind": "pre_restore_diagnostic",
                    "createdAt": created_at,
                    "deploymentVersion": _bounded_version(deployment_version),
                    "reason": reason,
                    "files": files,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        _set_private_file_mode(pending_directory / BACKUP_MANIFEST_NAME)
        _fsync_directory(pending_directory)
        os.replace(pending_directory, final_directory)
        complete = True
        _fsync_directory(backup_root)
    except (OSError, BackupRestoreError) as exc:
        raise BackupRestoreError("The current database could not be preserved before restore.") from exc
    finally:
        if not complete and pending_directory.exists():
            _remove_incomplete_directory(pending_directory, backup_root)
    return {
        "status": "diagnostic-unvalidated",
        "backupDirectory": str(final_directory),
        "manifest": str(final_directory / BACKUP_MANIFEST_NAME),
        "createdAt": created_at,
    }


def _remove_incomplete_directory(path: Path, expected_parent: Path) -> None:
    try:
        resolved = path.resolve(strict=False)
        if resolved.parent == expected_parent.resolve(strict=False) and resolved.name.startswith("."):
            shutil.rmtree(resolved)
    except OSError:
        # An unremovable hidden pending directory is never treated as a completed backup.
        pass


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _prepare_auxiliary_sources(files: dict[str, Path]) -> list[tuple[str, Path]]:
    if len(files) > MAX_AUXILIARY_FILES:
        raise BackupRestoreError("Too many auxiliary files were requested for one backup.")
    prepared: list[tuple[str, Path]] = []
    for name, source_value in sorted(files.items()):
        if (
            not _is_safe_artifact_name(name)
            or name in {BACKUP_DATABASE_NAME, BACKUP_MANIFEST_NAME}
        ):
            raise BackupRestoreError("An auxiliary backup filename is invalid.")
        source = Path(source_value).resolve(strict=False)
        if source.exists() and not source.is_file():
            raise BackupRestoreError("An auxiliary backup source is not a regular file.")
        if source.is_file():
            prepared.append((name, source))
    return prepared


def _is_safe_artifact_name(value: str) -> bool:
    return bool(
        value
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_version(value: str) -> str:
    normalized = str(value or "unknown").strip() or "unknown"
    return normalized[:256]


def _set_private_file_mode(path: Path) -> None:
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise BackupRestoreError("A private backup file permission could not be enforced.") from exc


def _set_private_directory_mode(path: Path) -> None:
    try:
        os.chmod(path, PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise BackupRestoreError("A private backup directory permission could not be enforced.") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/os.fsync.
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        # Windows does not provide a portable directory fsync operation. On POSIX,
        # especially the supported local-SSD model, a flush failure is material.
        if os.name != "nt":
            raise BackupRestoreError("A private backup directory could not be durably flushed.") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                if os.name != "nt":
                    raise BackupRestoreError("A private backup directory could not be closed safely.") from exc
