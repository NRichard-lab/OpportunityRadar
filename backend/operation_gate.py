from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Iterator
from uuid import uuid4


class OperationConflictError(RuntimeError):
    """Raised when shared-data work cannot safely start."""


# operation id -> (iso timestamp, monotonic timestamp) of its last reported
# progress. Kept outside ActiveOperation so the record itself stays frozen.
_progress: dict[str, tuple[str, float]] = {}
_progress_lock = threading.Lock()


def report_progress(operation_id: str) -> None:
    """Record forward progress for a running operation.

    Progress is advisory observability only. It never expires a lease and never
    releases the gate: a stalled operation still owns its data.
    """
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _progress_lock:
        _progress[operation_id] = (stamp, monotonic())


@dataclass(frozen=True)
class ActiveOperation:
    id: str
    operation_type: str
    started_at: str
    # Identity of the worker that owns this operation. A stranded gate is only
    # diagnosable if the holder can be named, so an incident responder can decide
    # whether the owner is still doing work or is wedged.
    owner_thread_id: int = 0
    owner_thread_name: str = ""
    started_monotonic: float = 0.0

    def public_status(self) -> dict[str, object]:
        status: dict[str, object] = {
            "id": self.id,
            "type": self.operation_type,
            "startedAt": self.started_at,
            "ownerThreadId": self.owner_thread_id,
            "ownerThreadName": self.owner_thread_name,
            "ownerAlive": self.owner_alive(),
            "elapsedSeconds": self.elapsed_seconds(),
        }
        progress = _progress.get(self.id)
        if progress is not None:
            status["lastProgressAt"] = progress[0]
            status["secondsSinceProgress"] = round(monotonic() - progress[1], 1)
        return status

    def elapsed_seconds(self) -> float:
        if not self.started_monotonic:
            return 0.0
        return round(monotonic() - self.started_monotonic, 1)

    def owner_alive(self) -> bool:
        """Is the thread that acquired this lease still running?

        A dead owner with a held lease is a leaked lease; a live owner may simply
        be slow. The two need different responses, so never conflate them.
        """
        if not self.owner_thread_id:
            return True
        return any(thread.ident == self.owner_thread_id for thread in threading.enumerate())


class MutationLease:
    def __init__(self, gate: "MutationGate", operation: ActiveOperation) -> None:
        self._gate = gate
        self.operation = operation
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate.release(self.operation.id)

    def __enter__(self) -> "MutationLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class MutationGate:
    """Single-process serialization for shared database and snapshot mutations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: ActiveOperation | None = None
        self._accepting = True

    def acquire(self, operation_type: str, *, operation_id: str | None = None) -> MutationLease:
        safe_type = _safe_operation_type(operation_type)
        with self._lock:
            if not self._accepting:
                raise OperationConflictError("The application is shutting down and cannot start new work.")
            if self._active is not None:
                active = self._active
                raise OperationConflictError(
                    f"Another mutating operation is active ({active.operation_type}, "
                    f"started {active.started_at}, {active.elapsed_seconds():.0f}s ago, "
                    f"owner {'alive' if active.owner_alive() else 'GONE'}). "
                    "Try again after it finishes."
                )
            current = threading.current_thread()
            operation = ActiveOperation(
                id=operation_id or f"operation-{uuid4()}",
                operation_type=safe_type,
                started_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                owner_thread_id=current.ident or 0,
                owner_thread_name=current.name[:60],
                started_monotonic=monotonic(),
            )
            self._active = operation
            return MutationLease(self, operation)

    def release(self, operation_id: str) -> None:
        with self._lock:
            if self._active is not None and self._active.id == operation_id:
                self._active = None
        with _progress_lock:
            _progress.pop(operation_id, None)

    def active_status(self) -> dict[str, object] | None:
        with self._lock:
            return self._active.public_status() if self._active is not None else None

    def diagnostics(self) -> dict[str, object]:
        """Everything an incident responder needs about the gate, and nothing secret.

        Deliberately read-only. There is no time-based expiry and no forced
        release, because this gate is what serialises writes to the shared SQLite
        database and the export snapshots. Clearing it while the original
        operation might still be running would admit a second writer and corrupt
        exactly the state it exists to protect.

        A held gate whose ``ownerAlive`` is ``False`` is a genuine leak; a held
        gate whose owner is alive but whose ``secondsSinceProgress`` is large is a
        wedged worker. Neither can be resolved from inside this process without
        proving the owner has stopped writing, which a thread cannot prove about
        another thread. The supported recovery is a backend restart, which ends
        every owner definitively. See ``docs/BROWSER_PAGE_CRASH.md``.
        """
        with self._lock:
            active = self._active
            accepting = self._accepting
        return {
            "accepting": accepting,
            "active": active.public_status() if active is not None else None,
        }

    def stop_accepting(self) -> None:
        with self._lock:
            self._accepting = False

    def start_accepting(self) -> None:
        with self._lock:
            self._accepting = True

    @contextmanager
    def hold(self, operation_type: str, *, operation_id: str | None = None) -> Iterator[ActiveOperation]:
        lease = self.acquire(operation_type, operation_id=operation_id)
        try:
            yield lease.operation
        finally:
            lease.release()


class StalledOperationWatcher:
    """Logs -- and only logs -- when a mutation has held the gate too long.

    This exists because the readiness probe checks storage, so a wedged worker
    leaves the container reporting ``healthy`` indefinitely while every mutating
    request is rejected with a conflict. A periodic log line makes that state
    visible without the watcher ever touching the lease.
    """

    def __init__(
        self,
        gate: "MutationGate",
        *,
        warn_after_seconds: float = 900.0,
        interval_seconds: float = 60.0,
        log: "logging.Logger | None" = None,
    ) -> None:
        self._gate = gate
        self._warn_after = warn_after_seconds
        self._interval = interval_seconds
        self._log = log or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="mutation-stall-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    def check_once(self) -> dict[str, object] | None:
        status = self._gate.active_status()
        if status is None:
            self._warned.clear()
            return None
        elapsed = float(status.get("elapsedSeconds") or 0.0)
        if elapsed < self._warn_after:
            return status
        operation_id = str(status.get("id"))
        owner_alive = bool(status.get("ownerAlive"))
        if operation_id not in self._warned:
            self._warned.add(operation_id)
            self._log.critical(
                "Mutation gate has been held by %s (id=%s) for %.0fs. Owner thread %s (%s) is %s. "
                "Every mutating request is being rejected with a conflict while this lasts. "
                "The gate is deliberately not cleared automatically: releasing it while the owner "
                "may still write would admit a second writer. Recover with a backend restart.",
                status.get("type"),
                operation_id,
                elapsed,
                status.get("ownerThreadId"),
                status.get("ownerThreadName"),
                "alive" if owner_alive else "GONE (leaked lease)",
            )
        return status

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - a watcher must never die
                self._log.exception("Mutation stall watcher iteration failed.")


def _safe_operation_type(value: str) -> str:
    normalized = "-".join(str(value or "operation").strip().lower().split())
    safe = "".join(character for character in normalized if character.isalnum() or character in {"-", "_"})
    return (safe or "operation")[:80]


GLOBAL_MUTATION_GATE = MutationGate()
GLOBAL_STALL_WATCHER = StalledOperationWatcher(GLOBAL_MUTATION_GATE)
