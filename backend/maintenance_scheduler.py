from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.utility_runs import UtilityRunManager, connection_scope, timestamp


DEFAULT_TIMEZONE = "America/Denver"
RegistryProvider = Callable[[], dict[str, dict[str, Any]]]


class MaintenanceScheduler:
    def __init__(
        self,
        database_path: Path,
        run_manager: UtilityRunManager,
        registry_provider: RegistryProvider,
        *,
        poll_seconds: float = 15.0,
    ) -> None:
        self.database_path = Path(database_path)
        self.run_manager = run_manager
        self.registry_provider = registry_provider
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._schedule_lock = threading.RLock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="maintenance-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.poll_seconds + 1))

    def ensure_schedule_rows(self) -> None:
        registry = self.registry_provider()
        now = timestamp()
        timezone = self.application_timezone()
        with connection_scope(self.database_path) as connection:
            for job_key, definition in registry.items():
                if not definition.get("supports_scheduling", True):
                    continue
                connection.execute(
                    """INSERT OR IGNORE INTO maintenance_schedules
                    (job_key,enabled,frequency,run_time,timezone,created_at,updated_at)
                    VALUES (?,0,'daily','02:00',?,?,?)""",
                    (job_key, timezone, now, now),
                )

    def application_timezone(self) -> str:
        with connection_scope(self.database_path, readonly=True) as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='scheduler_timezone'"
            ).fetchone()
        if row is None:
            return DEFAULT_TIMEZONE
        try:
            value = str(json.loads(row["value_json"]))
            ZoneInfo(value)
            return value
        except (json.JSONDecodeError, ZoneInfoNotFoundError):
            return DEFAULT_TIMEZONE

    def list_schedules(self) -> dict[str, dict[str, Any]]:
        with connection_scope(self.database_path, readonly=True) as connection:
            rows = connection.execute("SELECT * FROM maintenance_schedules ORDER BY job_key").fetchall()
        schedules = {row["job_key"]: schedule_snapshot(row) for row in rows}
        timezone = self.application_timezone()
        for job_key, definition in self.registry_provider().items():
            if definition.get("supports_scheduling", True) and job_key not in schedules:
                schedules[job_key] = {
                    "jobKey": job_key,
                    "enabled": False,
                    "frequency": "daily",
                    "runTime": "02:00",
                    "timezone": timezone,
                    "lastScheduledDate": "",
                    "updatedAt": "",
                }
        return dict(sorted(schedules.items()))

    def update_schedule(self, job_key: str, *, enabled: bool, run_time: str, timezone: str) -> dict[str, Any]:
        registry = self.registry_provider()
        definition = registry.get(job_key)
        if definition is None or not definition.get("supports_scheduling", True):
            raise KeyError(job_key)
        validate_run_time(run_time)
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Choose a valid IANA timezone, such as America/Denver.") from exc

        with self._schedule_lock:
            now = datetime.now(zone)
            last_scheduled_date = ""
            if enabled and now.strftime("%H:%M") >= run_time:
                last_scheduled_date = now.date().isoformat()
            updated_at = timestamp()
            with connection_scope(self.database_path) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO settings (key,value_json,updated_at) VALUES ('scheduler_timezone',?,?)",
                    (json.dumps(timezone), updated_at),
                )
                # Avoid invalidating another occurrence claim when the shared
                # timezone value did not actually change.
                connection.execute(
                    "UPDATE maintenance_schedules SET timezone=?,updated_at=? WHERE timezone<>?",
                    (timezone, updated_at, timezone),
                )
                connection.execute(
                    """INSERT INTO maintenance_schedules
                    (job_key,enabled,frequency,run_time,timezone,last_scheduled_date,created_at,updated_at)
                    VALUES (?,?,'daily',?,?,?,?,?)
                    ON CONFLICT(job_key) DO UPDATE SET enabled=excluded.enabled,frequency='daily',
                    run_time=excluded.run_time,timezone=excluded.timezone,
                    last_scheduled_date=CASE WHEN excluded.enabled=1 THEN excluded.last_scheduled_date ELSE maintenance_schedules.last_scheduled_date END,
                    updated_at=excluded.updated_at""",
                    (job_key, int(enabled), run_time, timezone, last_scheduled_date, updated_at, updated_at),
                )
            return self.list_schedules()[job_key]

    def run_due_once(self, now_utc: datetime | None = None) -> list[dict[str, Any]]:
        # Schedule edits and due-run claims share this lock. The supported
        # deployment is one process, so a user edit cannot race a stale read or
        # prevent a failed global-gate claim from being released for retry.
        with self._schedule_lock:
            return self._run_due_once_locked(now_utc)

    def _run_due_once_locked(self, now_utc: datetime | None = None) -> list[dict[str, Any]]:
        registry = self.registry_provider()
        outcomes: list[dict[str, Any]] = []
        for job_key, schedule in self.list_schedules().items():
            definition = registry.get(job_key)
            if not definition or not schedule["enabled"]:
                continue
            zone = ZoneInfo(schedule["timezone"])
            local_now = (now_utc or datetime.now().astimezone()).astimezone(zone)
            local_date = local_now.date().isoformat()
            if local_now.strftime("%H:%M") < schedule["runTime"] or schedule["lastScheduledDate"] == local_date:
                continue
            previous_scheduled_date = str(schedule["lastScheduledDate"])
            claim_marker = datetime.now().astimezone().isoformat(timespec="microseconds")
            if not self._claim_occurrence(
                job_key,
                local_date,
                previous_scheduled_date=previous_scheduled_date,
                claim_marker=claim_marker,
                observed_run_time=str(schedule["runTime"]),
                observed_timezone=str(schedule["timezone"]),
                observed_updated_at=str(schedule["updatedAt"]),
            ):
                continue
            if not definition.get("enabled", True):
                reason = definition.get("disabled_reason") or f"{definition['task_name']} is disabled."
                run = self.run_manager.record_skipped(
                    action=job_key,
                    task_name=definition["task_name"],
                    trigger_type="scheduled",
                    reason=reason,
                )
                outcomes.append(run)
                logging.info(reason)
                continue
            try:
                run = self.run_manager.start(
                    action=job_key,
                    task_name=definition["task_name"],
                    progress_verb=definition["progress_verb"],
                    progress_unit=definition["progress_unit"],
                    worker=definition["worker"],
                    format_summary=definition["format_summary"],
                    trigger_type="scheduled",
                    after_success=definition.get("after_scheduled_success"),
                )
            except RuntimeError:
                self._release_occurrence(
                    job_key,
                    local_date,
                    previous_scheduled_date=previous_scheduled_date,
                    claim_marker=claim_marker,
                )
                reason = f"{definition['task_name']} was skipped because another mutating operation is active."
                run = self.run_manager.record_skipped(
                    action=job_key,
                    task_name=definition["task_name"],
                    trigger_type="scheduled",
                    reason=reason,
                )
                logging.info(reason)
            except Exception:
                self._release_occurrence(
                    job_key,
                    local_date,
                    previous_scheduled_date=previous_scheduled_date,
                    claim_marker=claim_marker,
                )
                raise
            outcomes.append(run)
        return outcomes

    def _claim_occurrence(
        self,
        job_key: str,
        local_date: str,
        *,
        previous_scheduled_date: str,
        claim_marker: str,
        observed_run_time: str,
        observed_timezone: str,
        observed_updated_at: str,
    ) -> bool:
        with connection_scope(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE maintenance_schedules SET last_scheduled_date=?,updated_at=?
                WHERE job_key=? AND enabled=1 AND last_scheduled_date=?
                AND last_scheduled_date<>? AND run_time=? AND timezone=? AND updated_at=?""",
                (
                    local_date,
                    claim_marker,
                    job_key,
                    previous_scheduled_date,
                    local_date,
                    observed_run_time,
                    observed_timezone,
                    observed_updated_at,
                ),
            )
            return cursor.rowcount == 1

    def _release_occurrence(
        self,
        job_key: str,
        local_date: str,
        *,
        previous_scheduled_date: str,
        claim_marker: str,
    ) -> bool:
        with connection_scope(self.database_path) as connection:
            cursor = connection.execute(
                """UPDATE maintenance_schedules SET last_scheduled_date=?,updated_at=?
                WHERE job_key=? AND enabled=1 AND last_scheduled_date=? AND updated_at=?""",
                (previous_scheduled_date, timestamp(), job_key, local_date, claim_marker),
            )
            return cursor.rowcount == 1

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due_once()
            except Exception:
                logging.exception("Maintenance scheduler cycle failed.")
            self._stop_event.wait(self.poll_seconds)


def validate_run_time(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("Run time must use a valid 24-hour HH:MM value.") from exc
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Run time must use a valid 24-hour HH:MM value.")


def schedule_snapshot(row: Any) -> dict[str, Any]:
    return {
        "jobKey": row["job_key"],
        "enabled": bool(row["enabled"]),
        "frequency": row["frequency"],
        "runTime": row["run_time"],
        "timezone": row["timezone"],
        "lastScheduledDate": row["last_scheduled_date"],
        "updatedAt": row["updated_at"],
    }
