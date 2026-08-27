from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.migration import apply_migration, build_migration_plan
from config import BASE_DIR, DEFAULT_DATABASE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate-to-sqlite", help="Preview or apply the file-to-SQLite migration.")
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Inspect sources and planned actions without writing anything.")
    mode.add_argument("--apply", action="store_true", help="Back up sources, migrate transactionally, validate, and activate SQLite.")
    migrate.add_argument("--project-root", type=Path, default=BASE_DIR)
    migrate.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.preview:
            report = build_migration_plan(args.project_root, args.database).report
        else:
            report = apply_migration(args.project_root, args.database)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
