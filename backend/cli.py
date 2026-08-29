from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path
from typing import Sequence

from backend.backup_restore import (
    DEFAULT_RETENTION_COUNT,
    create_sqlite_backup,
    restore_sqlite_backup,
    validate_sqlite_backup,
)
from backend.db import connect, initialize_schema
from backend.health import MINIMUM_SCHEMA_VERSION
from backend.migration import apply_migration, build_migration_plan
from config import BACKUP_DIR, BASE_DIR, DEFAULT_DATABASE, DEPLOYMENT_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-to-sqlite", help="Preview or apply the file-to-SQLite migration.")
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Inspect sources and planned actions without writing anything.")
    mode.add_argument("--apply", action="store_true", help="Back up sources, migrate transactionally, validate, and activate SQLite.")
    migrate.add_argument("--project-root", type=Path, default=BASE_DIR)
    migrate.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    upgrade = commands.add_parser(
        "upgrade-schema",
        help="Transactionally apply idempotent schema upgrades to an existing SQLite database.",
    )
    upgrade.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    backup = commands.add_parser(
        "backup-sqlite", help="Create, validate, and atomically publish a SQLite backup."
    )
    backup.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    backup.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    backup.add_argument("--deployment-version", default=DEPLOYMENT_VERSION)
    backup.add_argument(
        "--retain",
        type=int,
        default=DEFAULT_RETENTION_COUNT,
        help="Number of validated regular backups to retain (minimum 1).",
    )

    validate = commands.add_parser(
        "validate-sqlite-backup", help="Validate a managed SQLite backup artifact."
    )
    validate.add_argument("--backup", type=Path, required=True)

    restore = commands.add_parser(
        "restore-sqlite", help="Stage, validate, preserve, and atomically restore SQLite."
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    restore.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    restore.add_argument("--deployment-version", default=DEPLOYMENT_VERSION)
    restore.add_argument(
        "--expected-schema-version", type=int, default=MINIMUM_SCHEMA_VERSION
    )
    restore.add_argument(
        "--confirm-writes-stopped",
        action="store_true",
        help="Required assertion that every process capable of writing the target DB is stopped.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate-to-sqlite":
            if args.preview:
                report = build_migration_plan(args.project_root, args.database).report
            else:
                report = apply_migration(args.project_root, args.database)
        elif args.command == "upgrade-schema":
            with closing(connect(args.database, require_existing=True)) as connection:
                initialize_schema(connection)
                version_row = connection.execute("PRAGMA user_version").fetchone()
                report = {
                    "status": "completed",
                    "schemaVersion": int(version_row[0]) if version_row else 0,
                }
        elif args.command == "backup-sqlite":
            report = create_sqlite_backup(
                args.database,
                args.backup_dir,
                deployment_version=args.deployment_version,
                retention_count=args.retain,
            )
        elif args.command == "validate-sqlite-backup":
            report = validate_sqlite_backup(args.backup)
            report.pop("manifest", None)
        elif args.command == "restore-sqlite":
            report = restore_sqlite_backup(
                args.backup,
                args.database,
                args.backup_dir,
                deployment_version=args.deployment_version,
                writes_stopped=args.confirm_writes_stopped,
                expected_schema_version=args.expected_schema_version,
            )
        else:
            raise RuntimeError("Unsupported command.")
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
