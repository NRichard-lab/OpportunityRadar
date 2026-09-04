"""A wedged Playwright teardown must never peg a core or strand the gate.

Production evidence (2026-09-04T21:23Z): the Playwright *node driver* died from
an uncaught assertion, and the worker thread then spun inside ``Browser.close()``
at ~91-97% of one core for 37 minutes. It held the mutation gate the whole time,
so every later company refresh was rejected, while ``/api/health`` -- which
checks storage only -- kept reporting healthy.

The spin lives in Playwright's sync pump::

    while not task.done():
        self._dispatcher_fiber.switch()

It never blocks in a syscall, so no signal, timeout or process kill reaches it.
These tests prove the outer hard bound does.
"""

from __future__ import annotations

import threading
import time
import unittest

from backend.operation_gate import (
    MutationGate,
    OperationConflictError,
    StalledOperationWatcher,
    report_progress,
)
from backend.outbound_security import (
    BrowserTeardownAbandoned,
    browser_diagnostics,
    hard_bounded_playwright_call,
)


def _thread_cpu_seconds() -> float:
    return time.process_time()


class HardBoundTests(unittest.TestCase):
    def test_fast_call_is_untouched(self) -> None:
        with hard_bounded_playwright_call("fast", 30.0):
            result = 21 * 2
        self.assertEqual(result, 42)

    def test_normal_exception_propagates_unchanged(self) -> None:
        with self.assertRaises(ValueError):
            with hard_bounded_playwright_call("raising", 30.0):
                raise ValueError("real failure")

    def test_busy_spin_is_unwound(self) -> None:
        """The production failure mode, reproduced exactly: a pure-Python busy loop."""
        started = time.monotonic()
        with self.assertRaises(BrowserTeardownAbandoned):
            with hard_bounded_playwright_call("spinning", 1.0):
                # Byte-for-byte the shape of Playwright's pump: no syscall, no
                # sleep, nothing interruptible except an async exception.
                while True:
                    pass
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20.0, "the spin was not broken promptly")

    def test_thread_returns_to_idle_after_abandonment(self) -> None:
        """After unwinding, the worker must stop burning CPU."""
        with self.assertRaises(BrowserTeardownAbandoned):
            with hard_bounded_playwright_call("spinning", 1.0):
                while True:
                    pass
        before = _thread_cpu_seconds()
        time.sleep(2.0)
        burned = _thread_cpu_seconds() - before
        self.assertLess(burned, 0.5, f"process still burning CPU after abandonment: {burned:.2f}s")

    def test_abandonment_is_recorded_for_diagnosis(self) -> None:
        with self.assertRaises(BrowserTeardownAbandoned):
            with hard_bounded_playwright_call("recorded-spin", 1.0):
                while True:
                    pass
        failure = browser_diagnostics()["last_driver_failure"]
        self.assertIsNotNone(failure)
        self.assertIn("recorded-spin", failure["label"])

    def test_bound_does_not_leak_into_later_work(self) -> None:
        """A fired watchdog must not strike an unrelated later call on this thread."""
        with self.assertRaises(BrowserTeardownAbandoned):
            with hard_bounded_playwright_call("spinning", 1.0):
                while True:
                    pass
        for _ in range(5):
            with hard_bounded_playwright_call("later", 30.0):
                pass
        total = 0
        for index in range(200000):
            total += index
        self.assertGreater(total, 0)


class AbandonedLoopSilencerTests(unittest.TestCase):
    """The silencer reaches into Playwright internals, so it must never raise."""

    def test_tolerates_objects_without_a_loop(self) -> None:
        from backend.outbound_security import _silence_abandoned_playwright_loop

        _silence_abandoned_playwright_loop(object())
        _silence_abandoned_playwright_loop(None)

    def test_mutes_a_real_loop(self) -> None:
        import asyncio

        from backend.outbound_security import _silence_abandoned_playwright_loop

        class FakeImpl:
            def __init__(self, loop):
                self._loop = loop

        class FakeBrowser:
            def __init__(self, loop):
                self._impl_obj = FakeImpl(loop)

        loop = asyncio.new_event_loop()
        try:
            self.assertIsNone(loop.get_exception_handler())
            _silence_abandoned_playwright_loop(FakeBrowser(loop))
            self.assertIsNotNone(loop.get_exception_handler())
            # The installed handler must swallow, not re-raise.
            loop.get_exception_handler()(loop, {"message": "Task exception was never retrieved"})
        finally:
            loop.close()

    def test_tolerates_a_loop_that_rejects_the_handler(self) -> None:
        from backend.outbound_security import _silence_abandoned_playwright_loop

        class HostileLoop:
            def set_exception_handler(self, _handler):
                raise RuntimeError("loop is closed")

        class Holder:
            def __init__(self):
                self._loop = HostileLoop()

        _silence_abandoned_playwright_loop(Holder())


class LaunchBoundTests(unittest.TestCase):
    def test_launch_has_its_own_generous_bound(self) -> None:
        """A cold Chromium start on a contended vCPU is legitimately slow, so the
        launch bound must be well clear of the teardown bound."""
        from backend.outbound_security import (
            _browser_close_bound_seconds,
            _browser_launch_bound_seconds,
        )

        self.assertGreaterEqual(_browser_launch_bound_seconds(), 60.0)
        self.assertGreater(_browser_launch_bound_seconds(), _browser_close_bound_seconds())


class MutationGateTests(unittest.TestCase):
    def test_lease_is_released_when_the_owner_exits(self) -> None:
        gate = MutationGate()
        with gate.hold("refresh-company"):
            self.assertIsNotNone(gate.active_status())
        self.assertIsNone(gate.active_status())

    def test_lease_is_released_when_the_owner_raises(self) -> None:
        gate = MutationGate()
        with self.assertRaises(RuntimeError):
            with gate.hold("refresh-company"):
                raise RuntimeError("collector blew up")
        self.assertIsNone(gate.active_status())

    def test_lease_is_released_when_the_owner_is_abandoned(self) -> None:
        """A teardown abandonment must still free the gate for the next refresh."""
        gate = MutationGate()
        with self.assertRaises(BrowserTeardownAbandoned):
            with gate.hold("refresh-company"):
                raise BrowserTeardownAbandoned("hard bound exceeded")
        self.assertIsNone(gate.active_status())
        with gate.hold("refresh-company"):
            pass

    def test_second_operation_is_refused_while_one_is_active(self) -> None:
        gate = MutationGate()
        with gate.hold("refresh-company"):
            with self.assertRaises(OperationConflictError) as caught:
                gate.acquire("refresh-company")
        self.assertIn("refresh-company", str(caught.exception))

    def test_status_names_the_owner_and_its_age(self) -> None:
        gate = MutationGate()
        with gate.hold("refresh-company") as operation:
            status = gate.active_status()
            self.assertEqual(status["type"], "refresh-company")
            self.assertEqual(status["ownerThreadId"], threading.get_ident())
            self.assertTrue(status["ownerAlive"])
            self.assertGreaterEqual(status["elapsedSeconds"], 0.0)
            report_progress(operation.id)
            self.assertIn("lastProgressAt", gate.active_status())

    def test_a_dead_owner_is_reported_as_gone(self) -> None:
        """A leaked lease is distinguishable from a merely slow one."""
        gate = MutationGate()
        holder: dict[str, object] = {}

        def acquire_and_die() -> None:
            holder["lease"] = gate.acquire("refresh-company")

        thread = threading.Thread(target=acquire_and_die)
        thread.start()
        thread.join()
        status = gate.active_status()
        self.assertIsNotNone(status)
        self.assertFalse(status["ownerAlive"], "a dead owner must not report as alive")

    def test_gate_is_never_cleared_automatically(self) -> None:
        """Fail-closed: no elapsed time releases another thread's lease."""
        gate = MutationGate()
        thread = threading.Thread(target=lambda: gate.acquire("refresh-company"))
        thread.start()
        thread.join()
        watcher = StalledOperationWatcher(gate, warn_after_seconds=0.0, interval_seconds=0.01)
        watcher.check_once()
        watcher.check_once()
        self.assertIsNotNone(
            gate.active_status(), "the watcher must observe the gate, never release it"
        )
        with self.assertRaises(OperationConflictError):
            gate.acquire("refresh-company")

    def test_watcher_reports_a_stalled_operation(self) -> None:
        gate = MutationGate()
        watcher = StalledOperationWatcher(gate, warn_after_seconds=0.0, interval_seconds=0.01)
        self.assertIsNone(watcher.check_once())
        with gate.hold("refresh-company"):
            status = watcher.check_once()
            self.assertIsNotNone(status)
            self.assertEqual(status["type"], "refresh-company")
        self.assertIsNone(watcher.check_once())

    def test_diagnostics_shape(self) -> None:
        gate = MutationGate()
        idle = gate.diagnostics()
        self.assertTrue(idle["accepting"])
        self.assertIsNone(idle["active"])
        with gate.hold("refresh-company"):
            busy = gate.diagnostics()
            self.assertEqual(busy["active"]["type"], "refresh-company")


class BrowserDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_are_serialisable_and_non_secret(self) -> None:
        import json

        snapshot = browser_diagnostics()
        json.dumps(snapshot)
        for key in ("leaseHeld", "leaseOwnerActive", "liveBrowserProcesses", "driver_failures"):
            self.assertIn(key, snapshot)
        self.assertNotIn("OPPORTUNITY_RADAR_SECRET_KEY", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
