from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backend.db import connect, initialize_schema
from backend.utility_tasks import UtilityCancelled


Worker = Callable[[Callable[..., None], threading.Event], dict[str, Any]]
SummaryFormatter = Callable[[dict[str, Any]], str]
SuccessCallback = Callable[[dict[str, Any]], Any]
ACTIVE_STATUSES = {"Queued", "Running", "Cancelling"}


@contextmanager
def connection_scope(database_path: Path, *, readonly: bool = False):
    with closing(connect(database_path, readonly=readonly)) as connection:
        if readonly:
            yield connection
        else:
            with connection:
                yield connection


class UtilityRunManager:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        with connection_scope(self.database_path) as connection:
            initialize_schema(connection)
            self._mark_interrupted_runs(connection)

    def start(
        self,
        *,
        action: str,
        task_name: str,
        progress_verb: str,
        progress_unit: str,
        worker: Worker,
        format_summary: SummaryFormatter,
        trigger_type: str = "manual",
        after_success: SuccessCallback | None = None,
    ) -> dict[str, Any]:
        run_id = f"maintenance-{uuid4()}"
        created_at = timestamp()
        cancel_event = threading.Event()
        with self._lock:
            try:
                with connection_scope(self.database_path) as connection:
                    connection.execute(
                        """INSERT INTO maintenance_job_runs (
                        id,job_key,task_name,trigger_type,progress_verb,progress_unit,status,
                        current_message,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,'Queued','Waiting to start.',?,?)""",
                        (run_id, action, task_name, trigger_type, progress_verb, progress_unit, created_at, created_at),
                    )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(f"{task_name} is already running.") from exc
            self._cancel_events[run_id] = cancel_event
        threading.Thread(
            target=self._execute,
            args=(run_id, worker, format_summary, cancel_event, after_success),
            name=f"maintenance-{action}",
            daemon=True,
        ).start()
        return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        with connection_scope(self.database_path, readonly=True) as connection:
            row = connection.execute("SELECT * FROM maintenance_job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return run_snapshot(row)

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with connection_scope(self.database_path, readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_job_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [run_snapshot(row) for row in rows]

    def history(self, job_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with connection_scope(self.database_path, readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_job_runs WHERE job_key = ? ORDER BY created_at DESC LIMIT ?",
                (job_key, min(100, max(1, limit))),
            ).fetchall()
        return [run_snapshot(row) for row in rows]

    def statistics(self, job_key: str) -> dict[str, Any]:
        history = self.history(job_key, limit=100)
        latest = next((run for run in history if run["status"] != "Skipped"), None)
        successful = [
            float(run["runtimeSeconds"])
            for run in history
            if run["status"] == "Completed" and run["runtimeSeconds"] is not None
        ][:20]
        average = round(sum(successful) / len(successful), 2) if successful else None
        return {"lastRun": latest, "averageRuntimeSeconds": average}

    def record_skipped(self, *, action: str, task_name: str, trigger_type: str, reason: str) -> dict[str, Any]:
        run_id = f"maintenance-{uuid4()}"
        now = timestamp()
        with self._lock:
            with connection_scope(self.database_path) as connection:
                connection.execute(
                    """INSERT INTO maintenance_job_runs (
                    id,job_key,task_name,trigger_type,status,current_message,error,
                    started_at,completed_at,runtime_seconds,created_at,updated_at)
                    VALUES (?,?,?,?, 'Skipped',?,?, ?,?,0,?,?)""",
                    (run_id, action, task_name, trigger_type, reason, reason, now, now, now, now),
                )
        return self.get(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.get(run_id)
            if current["status"] in {"Queued", "Running"}:
                event = self._cancel_events.get(run_id)
                if event is not None:
                    event.set()
                self._update_run(
                    run_id,
                    status="Cancelling",
                    current_message="Stopping after the current item finishes.",
                )
        return self.get(run_id)

    def _execute(
        self,
        run_id: str,
        worker: Worker,
        format_summary: SummaryFormatter,
        cancel_event: threading.Event,
        after_success: SuccessCallback | None,
    ) -> None:
        started_at = timestamp()
        started_clock = time.monotonic()
        run = self.get(run_id)
        with self._lock:
            if cancel_event.is_set():
                self._update_run(
                    run_id,
                    status="Cancelled",
                    started_at=started_at,
                    completed_at=timestamp(),
                    runtime_seconds=0,
                    current_message=f"{run['taskName']} was cancelled before it started.",
                )
                self._cancel_events.pop(run_id, None)
                return
            self._update_run(
                run_id,
                status="Running",
                started_at=started_at,
                current_message=f"{run['taskName']} is running.",
            )

        def progress(current: int, total: int, item: str, details: dict[str, Any] | None = None) -> None:
            progress_text = (
                f"{run['progressVerb']} {current} of {total} {run['progressUnit']}"
                if total else "Running..."
            )
            updates: dict[str, Any] = {
                "progress_current": max(0, current),
                "progress_total": max(0, total),
                "current_item": item,
                "current_message": progress_text,
            }
            if details is not None:
                updates["result_summary_json"] = json.dumps(details, default=str, sort_keys=True)
            self._update_run(
                run_id,
                **updates,
            )

        status = "Completed"
        summary: dict[str, Any] = {}
        error = ""
        message = ""
        try:
            summary = worker(progress, cancel_event)
            if cancel_event.is_set():
                raise UtilityCancelled("Cancelled by user.")
            message = format_summary(summary)
            if after_success is not None:
                try:
                    summary["scheduledFollowUp"] = after_success(summary)
                except Exception as exc:
                    logging.exception("Post-success maintenance follow-up failed; maintenance results were retained.")
                    summary["scheduledFollowUp"] = {"status": "Failed", "error": str(exc)}
        except (UtilityCancelled, InterruptedError):
            status = "Cancelled"
            message = f"{run['taskName']} was cancelled. No remaining items were processed."
        except Exception as exc:
            status = "Failed"
            error = str(exc)
            message = f"{run['taskName']} could not be completed: {exc}"
        finally:
            self._update_run(
                run_id,
                status=status,
                completed_at=timestamp(),
                runtime_seconds=round(time.monotonic() - started_clock, 2),
                result_summary_json=json.dumps(summary, default=str, sort_keys=True),
                error=error,
                current_message=message,
            )
            with self._lock:
                self._cancel_events.pop(run_id, None)
    def _update_run(self, run_id: str, **updates: Any) -> None:
        allowed = {
            "status", "progress_current", "progress_total", "current_item",
            "current_message", "started_at", "completed_at", "runtime_seconds",
            "result_summary_json", "error",
        }
        values = [(key, value) for key, value in updates.items() if key in allowed]
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in values)
        parameters = [value for _, value in values]
        parameters.extend([timestamp(), run_id])
        with self._lock:
            with connection_scope(self.database_path) as connection:
                cursor = connection.execute(
                    f"UPDATE maintenance_job_runs SET {assignments}, updated_at = ? WHERE id = ?",
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise KeyError(run_id)

    @staticmethod
    def _mark_interrupted_runs(connection: sqlite3.Connection) -> None:
        now = timestamp()
        rows = connection.execute(
            "SELECT id,task_name,started_at,created_at FROM maintenance_job_runs WHERE status IN ('Queued','Running','Cancelling')"
        ).fetchall()
        for row in rows:
            started_at = row["started_at"] or row["created_at"]
            connection.execute(
                """UPDATE maintenance_job_runs SET status='Failed',completed_at=?,runtime_seconds=?,
                current_message=?,error='Backend restarted while this maintenance job was running.',updated_at=?
                WHERE id=?""",
                (
                    now,
                    elapsed_between(started_at, now),
                    f"{row['task_name']} was interrupted because the backend restarted.",
                    now,
                    row["id"],
                ),
            )


def run_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    current = int(row["progress_current"] or 0)
    total = int(row["progress_total"] or 0)
    status = str(row["status"] or "")
    started_at = str(row["started_at"] or "")
    runtime = row["runtime_seconds"]
    if runtime is None and started_at and status in ACTIVE_STATUSES:
        runtime = elapsed_between(started_at, timestamp())
    progress_text = (
        f"{row['progress_verb']} {current} of {total} {row['progress_unit']}"
        if total else "Running..." if status in ACTIVE_STATUSES else "No itemized progress reported."
    )
    summary = json.loads(row["result_summary_json"] or "{}")
    return {
        "id": row["id"], "run_id": row["id"],
        "action": row["job_key"], "job_key": row["job_key"],
        "triggerType": row["trigger_type"], "trigger_type": row["trigger_type"],
        "taskName": row["task_name"], "task_name": row["task_name"],
        "progressVerb": row["progress_verb"], "progressUnit": row["progress_unit"],
        "status": status, "running": status in ACTIVE_STATUSES,
        "current": current, "total": total,
        "progress": round((current / total) * 100) if total else None,
        "progressText": progress_text, "currentCompany": row["current_item"],
        "currentMessage": row["current_message"], "message": row["current_message"],
        "summary": summary, "resultSummary": summary, "error": row["error"],
        "startedAt": started_at, "completedAt": row["completed_at"],
        "runtimeSeconds": round(float(runtime), 2) if runtime is not None else None,
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def elapsed_between(start: str, end: str) -> float:
    try:
        return round(max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()), 2)
    except (TypeError, ValueError):
        return 0.0


def timestamp() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
