from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Condition
from time import monotonic
from typing import Any, Callable
from uuid import uuid4


SERVICE_NAME = "opportunity-radar"
MINIMUM_SCHEMA_VERSION = 7
REQUIRED_CORE_COLUMNS = {
    "applications": frozenset(
        {
            "job_id", "applied", "application_status", "date_applied", "follow_up_date",
            "notes", "not_interested", "payload_json", "archived_at", "updated_at",
        }
    ),
    "companies": frozenset(
        {
            "id", "name", "normalized_name", "industry", "company_description",
            "city", "state", "country", "known_website",
            "official_website", "website_discovery_method", "website_candidate_urls",
            "website_verification_notes", "website_verified", "careers_page_url",
            "job_board_url", "job_board_discovery_method", "jobs_rss_feed_url",
            "job_platform", "feed_found", "search_status", "confidence", "last_checked",
            "notes", "founded_year", "total_assets", "assets_as_of_date",
            "company_info_last_checked", "created_at", "updated_at",
        }
    ),
    "jobs": frozenset(
        {
            "id", "legacy_id", "company_id", "company_name", "title", "location",
            "work_type", "pay_min", "pay_max", "pay_text", "pay_period", "pay_currency",
            "posted_date", "source_url", "job_platform", "description",
            "description_snippet", "collected_at", "status", "role_type",
            "role_type_reason", "raw_data_json", "first_seen_at", "created_at", "updated_at",
        }
    ),
    "resume_fit_results": frozenset(
        {
            "id", "resume_id", "job_id", "score", "status", "resume_version",
            "job_fingerprint", "algorithm_version", "matched_at", "error", "payload_json",
            "created_at",
        }
    ),
    "resumes": frozenset(
        {
            "id", "version", "name", "file_name", "uploaded_at", "extracted_text",
            "skills_json", "payload_json", "updated_at",
        }
    ),
    "settings": frozenset({"key", "value_json", "updated_at"}),
}
CORE_TABLES = frozenset(REQUIRED_CORE_COLUMNS)

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
NOT_CHECKED = "notChecked"

_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}\Z")
_PROBE_PREFIX = ".opportunity-radar-health-"
DEFAULT_CACHE_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class ReadinessResult:
    status_code: int
    payload: dict[str, Any]


def check_readiness(
    *,
    database_path: Path,
    data_dir: Path,
    export_dir: Path | None,
    version: str,
    checked_at: datetime | None = None,
) -> ReadinessResult:
    """Inspect local runtime dependencies without initializing or migrating them."""
    components = {
        "process": {"status": HEALTHY, "required": True},
        "database": _check_database(Path(database_path)),
        "dataStorage": _check_directory(Path(data_dir), required=True),
        "exportStorage": (
            _check_directory(Path(export_dir), required=False)
            if export_dir is not None
            else {
                "status": DEGRADED,
                "required": False,
                "checks": {"exists": NOT_CHECKED, "writable": NOT_CHECKED, "cleanup": NOT_CHECKED},
            }
        ),
    }

    required_failed = any(
        component.get("required") and component["status"] != HEALTHY
        for component in components.values()
    )
    optional_failed = any(
        not component.get("required") and component["status"] != HEALTHY
        for component in components.values()
    )
    status = UNHEALTHY if required_failed else DEGRADED if optional_failed else HEALTHY
    observed_at = (checked_at or datetime.now(UTC)).astimezone(UTC)
    payload = {
        "status": status,
        "service": SERVICE_NAME,
        "version": _safe_version(version),
        "checkedAt": observed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "components": components,
    }
    return ReadinessResult(status_code=503 if status == UNHEALTHY else 200, payload=payload)


class ReadinessCache:
    """Short-lived keyed cache that coalesces concurrent readiness probes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        checker: Callable[..., ReadinessResult] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Readiness cache TTL must be positive.")
        self._ttl_seconds = float(ttl_seconds)
        self._checker = checker or check_readiness
        self._clock = clock
        self._condition = Condition()
        self._entries: dict[tuple[str, str, str | None, str], tuple[float, ReadinessResult]] = {}
        self._in_flight: set[tuple[str, str, str | None, str]] = set()

    def get(
        self,
        *,
        database_path: Path,
        data_dir: Path,
        export_dir: Path | None,
        version: str,
    ) -> ReadinessResult:
        database = Path(database_path)
        data = Path(data_dir)
        exports = Path(export_dir) if export_dir is not None else None
        key = (
            _cache_path(database),
            _cache_path(data),
            _cache_path(exports) if exports is not None else None,
            str(version),
        )

        with self._condition:
            while True:
                cached = self._entries.get(key)
                if cached is not None and self._clock() < cached[0]:
                    return _clone_result(cached[1])
                self._entries.pop(key, None)
                if key not in self._in_flight:
                    self._in_flight.add(key)
                    break
                self._condition.wait()

        try:
            result = self._checker(
                database_path=database,
                data_dir=data,
                export_dir=exports,
                version=version,
            )
        except BaseException:
            with self._condition:
                self._in_flight.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._entries[key] = (self._clock() + self._ttl_seconds, _clone_result(result))
            self._in_flight.discard(key)
            self._condition.notify_all()
        return _clone_result(result)

    def clear(self) -> None:
        with self._condition:
            self._entries.clear()


def _cache_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _clone_result(result: ReadinessResult) -> ReadinessResult:
    return ReadinessResult(status_code=result.status_code, payload=deepcopy(result.payload))


def _check_database(database_path: Path) -> dict[str, Any]:
    checks = {
        "exists": UNHEALTHY,
        "read": NOT_CHECKED,
        "schema": NOT_CHECKED,
        "write": NOT_CHECKED,
    }
    component = {"status": UNHEALTHY, "required": True, "checks": checks}
    if not database_path.is_file():
        return component
    checks["exists"] = HEALTHY

    connection: sqlite3.Connection | None = None
    try:
        # mode=rw is deliberate: unlike sqlite3.connect(path), it cannot create a missing DB.
        connection = sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=2.0,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 2000")
        checks["read"] = HEALTHY if connection.execute("SELECT 1").fetchone() == (1,) else UNHEALTHY
        checks["schema"] = HEALTHY if _schema_is_ready(connection) else UNHEALTHY
        if checks["read"] == HEALTHY and checks["schema"] == HEALTHY:
            checks["write"] = HEALTHY if _write_probe_rolls_back(connection) else UNHEALTHY
    except (OSError, sqlite3.Error, ValueError):
        # The public response intentionally exposes no path or exception detail.
        pass
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                checks["write"] = UNHEALTHY

    if all(value == HEALTHY for value in checks.values()):
        component["status"] = HEALTHY
    return component


def _schema_is_ready(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {str(row[0]) for row in rows}
    version_row = connection.execute("PRAGMA user_version").fetchone()
    schema_version = int(version_row[0]) if version_row else 0
    if not CORE_TABLES.issubset(tables) or schema_version < MINIMUM_SCHEMA_VERSION:
        return False
    for table, required_columns in REQUIRED_CORE_COLUMNS.items():
        column_rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        actual_columns = {str(row[1]) for row in column_rows}
        if not required_columns.issubset(actual_columns):
            return False
        selected_columns = ", ".join(f'"{column}"' for column in sorted(required_columns))
        connection.execute(f'SELECT {selected_columns} FROM "{table}" LIMIT 0').fetchone()
    return True


def _write_probe_rolls_back(connection: sqlite3.Connection) -> bool:
    probe_key = f"__health_probe__:{uuid4().hex}"
    inserted = False
    rollback_succeeded = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
            (probe_key, "{}", "health-check"),
        )
        inserted = connection.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (probe_key,),
        ).fetchone() == (1,)
    except sqlite3.Error:
        inserted = False
    finally:
        try:
            connection.rollback()
            rollback_succeeded = True
        except sqlite3.Error:
            rollback_succeeded = False

    if not inserted or not rollback_succeeded:
        return False
    try:
        return connection.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (probe_key,),
        ).fetchone() is None
    except sqlite3.Error:
        return False


def _check_directory(path: Path, *, required: bool) -> dict[str, Any]:
    failed_status = UNHEALTHY if required else DEGRADED
    checks = {"exists": failed_status, "writable": NOT_CHECKED, "cleanup": NOT_CHECKED}
    component = {"status": failed_status, "required": required, "checks": checks}
    if not path.is_dir():
        return component
    checks["exists"] = HEALTHY

    descriptor: int | None = None
    probe_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=path)
        probe_path = Path(raw_path)
        with os.fdopen(descriptor, "wb") as probe_file:
            descriptor = None
            probe_file.write(b"opportunity-radar-health\n")
            probe_file.flush()
            os.fsync(probe_file.fileno())
        checks["writable"] = HEALTHY
    except OSError:
        checks["writable"] = failed_status
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
                checks["cleanup"] = HEALTHY
            except OSError:
                checks["cleanup"] = failed_status

    if checks["writable"] == HEALTHY and checks["cleanup"] == HEALTHY:
        component["status"] = HEALTHY
    return component


def _safe_version(value: str) -> str:
    candidate = value.strip()
    return candidate if _SAFE_VERSION.fullmatch(candidate) else "unknown"
