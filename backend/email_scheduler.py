from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.email_service import EmailService, WEEKDAYS
from backend.operation_gate import (
    GLOBAL_MUTATION_GATE,
    MutationGate,
    OperationConflictError,
)


EmailServiceFactory = Callable[[], EmailService]


class EmailScheduler:
    """Runs the calendar digest independently from maintenance schedules.

    The shared mutation gate deliberately remains unclaimed while a refresh is
    active. The next poll retries the same persisted occurrence after refresh
    completion, so the digest never observes a partially refreshed job set.
    """

    def __init__(
        self,
        service_factory: EmailServiceFactory,
        *,
        mutation_gate: MutationGate | None = None,
        poll_seconds: float = 15.0,
    ) -> None:
        self.service_factory = service_factory
        self.mutation_gate = mutation_gate or GLOBAL_MUTATION_GATE
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.RLock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="email-digest-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.poll_seconds + 1))

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def run_due_once(self, now_utc: datetime | None = None) -> dict[str, Any] | None:
        with self._run_lock:
            service = self.service_factory()
            settings = service.get_settings()
            due = due_occurrence(settings, now_utc)
            if due is None:
                return None
            try:
                lease = self.mutation_gate.acquire("scheduled-email-digest")
            except OperationConflictError:
                return {
                    "status": "Waiting for active refresh",
                    "scheduledFor": due["scheduledFor"],
                }
            try:
                # A settings edit can complete between the first read and gate
                # acquisition. Re-read and atomically claim exactly what is due.
                settings = service.get_settings()
                due = due_occurrence(settings, now_utc)
                if due is None:
                    return None
                if not service.claim_scheduled_occurrence(settings, due["localDate"]):
                    return None
                return service.send_job_digest(
                    trigger_type="scheduled",
                    scheduled_for=due["scheduledFor"],
                )
            finally:
                lease.release()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due_once()
            except Exception:
                logging.exception("Email digest scheduler cycle failed.")
            self._stop_event.wait(self.poll_seconds)


def due_occurrence(
    settings: dict[str, Any], now_utc: datetime | None = None
) -> dict[str, str] | None:
    if not settings.get("enabled"):
        return None
    timezone_name = str(settings.get("scheduleTimezone") or "")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    local_now = (now_utc or datetime.now().astimezone()).astimezone(zone)
    local_date = local_now.date().isoformat()
    schedule_time = str(settings.get("scheduleTime") or "")
    schedule_days = set(settings.get("scheduleDays") or [])
    if WEEKDAYS[local_now.weekday()] not in schedule_days:
        return None
    if local_now.strftime("%H:%M") < schedule_time:
        return None
    if str(settings.get("lastScheduledDate") or "") == local_date:
        return None
    return {
        "localDate": local_date,
        "scheduledFor": f"{local_date}T{schedule_time}:00[{timezone_name}]",
    }
