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
from backend.operation_gate import GLOBAL_MUTATION_GATE, MutationGate, MutationLease, OperationConflictError
from backend.utility_tasks import UtilityCancelled


Worker = Callable[[Callable[..., None], threading.Event], dict[str, Any]]
SummaryFormatter = Callable[[dict[str, Any]], str]
SuccessCallback = Callable[[dict[str, Any]], Any]
FinishCallback = Callable[[], Any]
ACTIVE_STATUSES = {"Queued", "Running", "Cancelling"}


@contextmanager
def connection_scope(
    database_path: Path,
    *,
    readonly: bool = False,
    require_existing: bool = False,
):
    with closing(
        connect(
            database_path,
            readonly=readonly,
            require_existing=require_existing,
        )
    ) as connection:
        if readonly:
            yield connection
        else:
            with connection:
                yield connection


class UtilityRunManager:
    def __init__(
        self,
        database_path: Path,
        *,
        mutation_gate: MutationGate | None = None,
        initialize: bool = True,
        reconcile: bool = True,
        require_existing: bool = False,
    ) -> None:
        self.database_path = Path(database_path)
        self.require_existing = require_existing
        self._lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._mutation_gate = mutation_gate or GLOBAL_MUTATION_GATE
        self._accepting = True
        if initialize or reconcile:
            with connection_scope(
                self.database_path, require_existing=self.require_existing
            ) as connection:
                if initialize:
                    initialize_schema(connection)
                if reconcile:
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
        after_finish: FinishCallback | None = None,
    ) -> dict[str, Any]:
        run_id = f"maintenance-{uuid4()}"
        created_at = timestamp()
        cancel_event = threading.Event()
        with self._lock:
            if not self._accepting:
                raise OperationConflictError("The application is shutting down and cannot start new work.")
            lease = self._mutation_gate.acquire(action, operation_id=run_id)
            try:
                with connection_scope(
                    self.database_path, require_existing=self.require_existing
                ) as connection:
                    connection.execute(
                        """INSERT INTO maintenance_job_runs (
                        id,job_key,task_name,trigger_type,progress_verb,progress_unit,status,
                        current_message,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,'Queued','Waiting to start.',?,?)""",
                        (run_id, action, task_name, trigger_type, progress_verb, progress_unit, created_at, created_at),
                    )
            except sqlite3.IntegrityError as exc:
                lease.release()
                raise RuntimeError(f"{task_name} is already running.") from exc
            except Exception:
                lease.release()
                raise
            self._cancel_events[run_id] = cancel_event
            try:
                thread = threading.Thread(
                    target=self._execute,
                    args=(run_id, worker, format_summary, cancel_event, after_success, after_finish, lease),
                    name=f"maintenance-{action}",
                    daemon=True,
                )
                self._threads[run_id] = thread
                thread.start()
            except Exception:
                self._threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
                try:
                    self._update_run(
                        run_id,
                        status="Failed",
                        completed_at=timestamp(),
                        runtime_seconds=0,
                        error="The background worker could not be started.",
                        current_message=f"{task_name} could not be started.",
                    )
                finally:
                    lease.release()
                raise RuntimeError(f"{task_name} could not be started.") from None
        return self.get(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        with connection_scope(
            self.database_path,
            readonly=True,
            require_existing=self.require_existing,
        ) as connection:
            row = connection.execute("SELECT * FROM maintenance_job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return run_snapshot(row)

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with connection_scope(
            self.database_path,
            readonly=True,
            require_existing=self.require_existing,
        ) as connection:
            rows = connection.execute(
                "SELECT * FROM maintenance_job_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [run_snapshot(row) for row in rows]

    def history(self, job_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with connection_scope(
            self.database_path,
            readonly=True,
            require_existing=self.require_existing,
        ) as connection:
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
            with connection_scope(
                self.database_path, require_existing=self.require_existing
            ) as connection:
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

    def shutdown(self, *, join_timeout: float = 5.0) -> None:
        """Prevent new work, request cancellation, and briefly await cooperative workers."""
        self._mutation_gate.stop_accepting()
        with self._lock:
            self._accepting = False
            events = list(self._cancel_events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, join_timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def start_accepting(self) -> None:
        with self._lock:
            self._accepting = True
        self._mutation_gate.start_accepting()

    def _execute(
        self,
        run_id: str,
        worker: Worker,
        format_summary: SummaryFormatter,
        cancel_event: threading.Event,
        after_success: SuccessCallback | None,
        after_finish: FinishCallback | None,
        lease: MutationLease,
    ) -> None:
        started_at = timestamp()
        started_clock = time.monotonic()
        try:
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
                    return
                self._update_run(
                    run_id,
                    status="Running",
                    started_at=started_at,
                    current_message=f"{run['taskName']} is running.",
                )

            latest_details: dict[str, Any] = {}

            def progress(current: int, total: int, item: str, details: dict[str, Any] | None = None) -> None:
                nonlocal latest_details
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
                    latest_details = dict(details)
                    updates["result_summary_json"] = json.dumps(details, default=str, sort_keys=True)
                self._update_run(run_id, **updates)

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
                    except Exception:
                        logging.exception("Post-success maintenance follow-up failed; maintenance results were retained.")
                        summary["scheduledFollowUp"] = {
                            "status": "Failed",
                            "error": "The post-run action could not be completed.",
                        }
            except (UtilityCancelled, InterruptedError):
                status = "Cancelled"
                summary = latest_details or summary
                message = f"{run['taskName']} was cancelled. No remaining items were processed."
            except Exception as exc:
                status = "Failed"
                summary = latest_details
                logging.exception("Maintenance operation %s failed.", run["action"])
                error = safe_error_summary(exc)
                message = f"{run['taskName']} could not be completed."
            self._update_run(
                run_id,
                status=status,
                completed_at=timestamp(),
                runtime_seconds=round(time.monotonic() - started_clock, 2),
                result_summary_json=json.dumps(summary, default=str, sort_keys=True),
                error=error,
                current_message=message,
            )
        except Exception:
            logging.exception("Maintenance lifecycle bookkeeping failed for %s.", run_id)
            try:
                self._update_run(
                    run_id,
                    status="Failed",
                    completed_at=timestamp(),
                    runtime_seconds=round(time.monotonic() - started_clock, 2),
                    error="The maintenance lifecycle could not be completed.",
                    current_message="The maintenance operation stopped because its state could not be recorded.",
                )
            except Exception:
                logging.exception("Maintenance failure state could not be persisted for %s.", run_id)
        finally:
            try:
                if after_finish is not None:
                    after_finish()
            except Exception:
                logging.exception("Maintenance cleanup failed for %s.", run_id)
            finally:
                with self._lock:
                    self._cancel_events.pop(run_id, None)
                    self._threads.pop(run_id, None)
                lease.release()

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
            with connection_scope(
                self.database_path, require_existing=self.require_existing
            ) as connection:
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


def reconcile_interrupted_runs(database_path: Path) -> int:
    """Mark stale active rows without creating or migrating persistence."""
    path = Path(database_path)
    if not path.is_file():
        return 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=rw",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_job_runs'"
        ).fetchone()
        if table is None:
            return 0
        count_row = connection.execute(
            "SELECT COUNT(*) FROM maintenance_job_runs WHERE status IN ('Queued','Running','Cancelling')"
        ).fetchone()
        count = int(count_row[0]) if count_row else 0
        with connection:
            UtilityRunManager._mark_interrupted_runs(connection)
        return count
    except sqlite3.Error:
        logging.exception("Could not reconcile interrupted maintenance records during startup.")
        return 0
    finally:
        if connection is not None:
            connection.close()


def safe_error_summary(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "The operation timed out."
    if isinstance(exc, OperationConflictError):
        return str(exc)[:240]
    if isinstance(exc, ValueError):
        message = " ".join(str(exc).split())
        if message and not any(marker in message for marker in ("\\", "/", ":\\")):
            return message[:240]
    return "The operation failed. Review server logs for details."
