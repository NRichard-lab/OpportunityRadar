from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4


class OperationConflictError(RuntimeError):
    """Raised when shared-data work cannot safely start."""


@dataclass(frozen=True)
class ActiveOperation:
    id: str
    operation_type: str
    started_at: str

    def public_status(self) -> dict[str, str]:
        return {
            "id": self.id,
            "type": self.operation_type,
            "startedAt": self.started_at,
        }


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
                raise OperationConflictError(
                    f"Another mutating operation is active ({self._active.operation_type}). Try again after it finishes."
                )
            operation = ActiveOperation(
                id=operation_id or f"operation-{uuid4()}",
                operation_type=safe_type,
                started_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            self._active = operation
            return MutationLease(self, operation)

    def release(self, operation_id: str) -> None:
        with self._lock:
            if self._active is not None and self._active.id == operation_id:
                self._active = None

    def active_status(self) -> dict[str, str] | None:
        with self._lock:
            return self._active.public_status() if self._active is not None else None

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


def _safe_operation_type(value: str) -> str:
    normalized = "-".join(str(value or "operation").strip().lower().split())
    safe = "".join(character for character in normalized if character.isalnum() or character in {"-", "_"})
    return (safe or "operation")[:80]


GLOBAL_MUTATION_GATE = MutationGate()
