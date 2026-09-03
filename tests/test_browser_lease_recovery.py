"""Regression tests for browser-launch lease robustness.

Covers the production failure where an OOM-killed / hung Chromium left the
process-global launch lease held for the life of the worker, so every later
browser discovery and collection failed with "Only one Chromium process may run
at a time" and silently degraded to an empty (falsely authoritative) result.

Process identity is verified by ``(pid, start_time)``; termination is restricted
to the recorded tree; a stale callback can never release a newer lease; the
close guard is joined before the lease is released; and if termination cannot be
proven the lease is held (fail-closed), not force-released.

These are fake-process tests. Real-Chromium coverage lives in
``tests/integration/test_browser_lease_real_chromium.py`` (run inside the
production-equivalent Linux container).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

import backend.outbound_security as ob
from backend.outbound_security import (
    BROWSER_EGRESS_MODE,
    BROWSER_PROXY_PORT,
    BrowserEgressConfigurationError,
    BrowserRuntimeBoundary,
    launch_playwright_chromium,
)


RUNTIME = BrowserRuntimeBoundary(
    playwright_version="1.62.0",
    chromium_revision="1234",
    chromium_version="151.0.7922.34",
    chromium_executable="/ms-playwright/chromium-1234/chrome-linux64/chrome",
    wrapper_executable="/usr/local/bin/opportunity-radar-chromium-netns",
    unshare_executable="/usr/bin/unshare",
    unix_socket="/tmp/radar-browser-test/egress.sock",
    proxy_port=BROWSER_PROXY_PORT,
)


class FakeBrowser:
    def __init__(self, close_behaviour=None) -> None:
        self._handlers: dict[str, list] = {}
        self.close_calls = 0
        self._close_behaviour = close_behaviour

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, *args) -> None:
        for handler in list(self._handlers.get(event, [])):
            handler(*args)

    def close(self, *_args, **_kwargs):
        self.close_calls += 1
        behaviour = self._close_behaviour
        if isinstance(behaviour, BaseException):
            raise behaviour
        if callable(behaviour):
            behaviour(self)
        return None


def fake_playwright(browser=None, launch_error=None) -> Mock:
    playwright = Mock()
    if launch_error is not None:
        playwright.chromium.launch.side_effect = launch_error
    else:
        playwright.chromium.launch.return_value = browser
    return playwright


class _LeaseTestBase(unittest.TestCase):
    def tearDown(self) -> None:
        with ob._browser_lease_state_lock:
            ob._current_leased_browser = None
        if ob._browser_launch_lease.locked():
            with contextlib.suppress(RuntimeError):
                ob._browser_launch_lease.release()

    @contextlib.contextmanager
    def protected_env(
        self,
        *,
        procs=None,
        driver_proc=None,
        proc_alive=lambda _pid, _start: False,
        approved_chromium_alive=lambda: False,
        signal_pids=None,
        proc_available=True,
        wait_seconds="0",
        chromium_grace="0.2",
        driver_grace="0.2",
        kill_grace="0.2",
    ):
        procs = dict(procs or {})
        recorded_signals: list[tuple[tuple[int, ...], int]] = []

        def default_signal(pids, sig):
            recorded_signals.append((tuple(sorted(pids)), sig))

        env = {
            "APP_ENV": "production",
            "APP_BROWSER_EGRESS_MODE": BROWSER_EGRESS_MODE,
            "OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS": str(wait_seconds),
            "OPPORTUNITY_RADAR_BROWSER_CLOSE_GRACE_SECONDS": str(chromium_grace),
            "OPPORTUNITY_RADAR_BROWSER_DRIVER_GRACE_SECONDS": str(driver_grace),
            "OPPORTUNITY_RADAR_BROWSER_KILL_GRACE_SECONDS": str(kill_grace),
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(ob, "validate_browser_runtime_boundary", return_value=RUNTIME))
            stack.enter_context(patch.object(ob, "_require_browser_proxy_ready"))
            stack.enter_context(patch.object(ob, "_browser_parent_netns_inode", return_value="12345"))
            stack.enter_context(patch.object(ob, "_is_proc_filesystem_available", return_value=proc_available))
            stack.enter_context(patch.object(ob, "_capture_browser_procs", side_effect=lambda _exe: dict(procs)))
            stack.enter_context(patch.object(ob, "_find_playwright_driver_proc", return_value=driver_proc))
            stack.enter_context(patch.object(ob, "_proc_alive", side_effect=proc_alive))
            stack.enter_context(
                patch.object(ob, "_any_approved_chromium_alive", side_effect=lambda _exe: approved_chromium_alive())
            )
            stack.enter_context(patch.object(ob, "_signal_pids", side_effect=signal_pids or default_signal))
            yield recorded_signals


class BrowserLeaseRecoveryTests(_LeaseTestBase):
    def test_launch_failure_releases_the_lease(self) -> None:
        with self.protected_env():
            with self.assertRaises(RuntimeError):
                launch_playwright_chromium(fake_playwright(launch_error=RuntimeError("spawn failed")), headless=True)
        self.assertFalse(ob._browser_launch_lease.locked())
        self.assertIsNone(ob._current_leased_browser)

    def test_process_identity_is_pid_plus_start_time(self) -> None:
        # A recorded PID whose start-time no longer matches is treated as dead
        # (PID reuse), so the lease is reclaimable.
        recorded = {"4242": None}
        alive = {(4242, 100)}
        with self.protected_env(
            procs={4242: 100},
            proc_alive=lambda pid, start: (pid, start) in alive,
            wait_seconds="0.3",
        ):
            first = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertEqual(first._procs, {4242: 100})
            # PID 4242 still exists but is now a *different* process (start-time 999).
            alive.clear()
            alive.add((4242, 999))
            with self.assertLogs("backend.outbound_security", level="WARNING") as logs:
                second = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertTrue(any("Reclaiming a leaked Chromium launch lease" in m for m in logs.output))
            self.assertIs(ob._current_leased_browser, second)
            second.close()

    def test_abnormal_exit_then_next_launch_reclaims(self) -> None:
        alive = {(4242, 100)}
        with self.protected_env(
            procs={4242: 100},
            proc_alive=lambda pid, start: (pid, start) in alive,
            approved_chromium_alive=lambda: bool(alive),
            wait_seconds="0.3",
        ):
            first = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertTrue(first._process_confirmed)
            alive.clear()  # OOM kill; owner never released, disconnected never fired
            second = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertIs(ob._current_leased_browser, second)
            second.close()
        self.assertFalse(ob._browser_launch_lease.locked())

    def test_a_live_owner_is_never_displaced(self) -> None:
        alive = {(777, 5)}
        with self.protected_env(
            procs={777: 5},
            proc_alive=lambda pid, start: (pid, start) in alive,
            approved_chromium_alive=lambda: True,
            wait_seconds="0.4",
        ):
            first = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            started = time.monotonic()
            with self.assertRaisesRegex(BrowserEgressConfigurationError, "Only one Chromium"):
                launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            self.assertTrue(ob._browser_launch_lease.locked())
            self.assertIs(ob._current_leased_browser, first)
            first.close()

    def test_legitimate_contention_waits_then_succeeds(self) -> None:
        with self.protected_env(procs={}, wait_seconds="3"):
            first = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)

            def release_soon() -> None:
                time.sleep(0.3)
                first.close()

            releaser = threading.Thread(target=release_soon)
            releaser.start()
            started = time.monotonic()
            second = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            releaser.join()
            waited = time.monotonic() - started
            self.assertGreaterEqual(waited, 0.25)
            self.assertLess(waited, 3.0)
            self.assertIs(ob._current_leased_browser, second)
            second.close()
        self.assertFalse(ob._browser_launch_lease.locked())

    def test_stale_disconnected_callback_cannot_release_new_lease(self) -> None:
        alive = {(11, 1)}
        with self.protected_env(
            procs={11: 1},
            proc_alive=lambda pid, start: (pid, start) in alive,
            wait_seconds="0.3",
        ):
            first = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            alive.clear()
            second = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertIs(ob._current_leased_browser, second)

            # Force the guard: pretend `first` was never released, then fire its
            # stale disconnected callback. The identity check must stop it from
            # releasing `second`'s lease.
            first._released = False
            first._release()
            self.assertTrue(ob._browser_launch_lease.locked())
            self.assertIs(ob._current_leased_browser, second)
            second.close()


class CloseCleanupBoundingTests(_LeaseTestBase):
    def test_close_hang_needs_only_chromium_kill(self) -> None:
        killed = threading.Event()
        can_return = threading.Event()

        def signal_pids(pids, sig):
            if 999 in set(pids) and sig == ob._SIGKILL:
                killed.set()
                can_return.set()

        with self.protected_env(
            procs={999: 9},
            driver_proc=(555, 55),
            proc_alive=lambda pid, start: (pid == 999 and not killed.is_set()),
            signal_pids=signal_pids,
            chromium_grace="0.2",
            driver_grace="0.5",
            kill_grace="0.2",
        ):
            hanging = FakeBrowser(close_behaviour=lambda _s: can_return.wait(timeout=5))
            leased = launch_playwright_chromium(fake_playwright(browser=hanging), headless=True)
            started = time.monotonic()
            leased.close()
            elapsed = time.monotonic() - started
            self.assertTrue(killed.is_set())
            self.assertLess(elapsed, 3.0)
            self.assertFalse(ob._browser_launch_lease.locked())

    def test_close_hang_after_chromium_exit_kills_wedged_driver(self) -> None:
        chromium_killed = threading.Event()
        driver_killed = threading.Event()
        can_return = threading.Event()

        def signal_pids(pids, sig):
            pidset = set(pids)
            if 999 in pidset and sig == ob._SIGKILL:
                chromium_killed.set()  # Chromium dies, but close() stays blocked
            if 555 in pidset and sig == ob._SIGKILL:
                driver_killed.set()
                can_return.set()  # killing the driver unblocks the pipe read

        def proc_alive(pid, start):
            if pid == 999:
                return not chromium_killed.is_set()
            if pid == 555:
                return not driver_killed.is_set()
            return False

        with self.protected_env(
            procs={999: 9},
            driver_proc=(555, 55),
            proc_alive=proc_alive,
            signal_pids=signal_pids,
            chromium_grace="0.2",
            driver_grace="0.2",
            kill_grace="0.2",
        ):
            hanging = FakeBrowser(close_behaviour=lambda _s: can_return.wait(timeout=5))
            leased = launch_playwright_chromium(fake_playwright(browser=hanging), headless=True)
            with self.assertLogs("backend.outbound_security", level="ERROR") as logs:
                started = time.monotonic()
                leased.close()
                elapsed = time.monotonic() - started
            self.assertTrue(chromium_killed.is_set())
            self.assertTrue(driver_killed.is_set())
            self.assertTrue(any("Playwright driver (pid=555) is wedged" in m for m in logs.output))
            self.assertLess(elapsed, 4.0)
            self.assertFalse(ob._browser_launch_lease.locked())

    def test_unconfirmed_termination_fails_closed_and_recovers_later(self) -> None:
        # PID 321 refuses to die (models an un-reapable process). Cleanup must
        # NOT release the lease.
        pid_dead = threading.Event()

        with self.protected_env(
            procs={321: 3},
            proc_alive=lambda pid, start: (pid == 321 and not pid_dead.is_set()),
            approved_chromium_alive=lambda: not pid_dead.is_set(),
            wait_seconds="0",
            chromium_grace="0.1",
            driver_grace="0.1",
            kill_grace="0.1",
        ):
            leased = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            with self.assertLogs("backend.outbound_security", level="CRITICAL") as logs:
                leased.close()
            self.assertTrue(any("could not confirm process-tree termination" in m for m in logs.output))
            # Fail-closed: lease still held, holder unchanged.
            self.assertTrue(ob._browser_launch_lease.locked())
            self.assertIs(ob._current_leased_browser, leased)

            # A subsequent launch also fails closed while 321 is alive.
            with self.assertRaisesRegex(BrowserEgressConfigurationError, "Only one Chromium"):
                launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)

            # Once 321 is really gone, the next launch reclaims cleanly.
            pid_dead.set()
            recovered = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
            self.assertIs(ob._current_leased_browser, recovered)
            recovered.close()
        self.assertFalse(ob._browser_launch_lease.locked())

    def test_close_error_is_swallowed_when_tree_confirmed_gone(self) -> None:
        class TargetClosedError(Exception):
            pass

        broken = FakeBrowser(close_behaviour=TargetClosedError("Target page, context or browser has been closed"))
        with self.protected_env(procs={123: 1}, proc_alive=lambda pid, start: False):
            leased = launch_playwright_chromium(fake_playwright(browser=broken), headless=True)
            leased.close()  # must not raise
            self.assertEqual(broken.close_calls, 1)
            self.assertFalse(ob._browser_launch_lease.locked())

    def test_lease_recovers_even_if_close_never_returns(self) -> None:
        # Worst case: browser.close() blocks forever AND killing Chromium + the
        # driver does not unblock it. The owning close() call stays stuck, but the
        # guard still kills the whole tree, so a *fresh* launch must be able to
        # reclaim the lease (recorded tree provably gone). Only the one stuck
        # thread is lost -- the lease itself is bounded.
        chromium_killed = threading.Event()
        driver_killed = threading.Event()
        never_returns = threading.Event()  # deliberately never set

        def signal_pids(pids, sig):
            pidset = set(pids)
            if 900 in pidset and sig == ob._SIGKILL:
                chromium_killed.set()
            if 901 in pidset and sig == ob._SIGKILL:
                driver_killed.set()

        def proc_alive(pid, start):
            if pid == 900:
                return not chromium_killed.is_set()
            if pid == 901:
                return not driver_killed.is_set()
            return False

        try:
            with self.protected_env(
                procs={900: 90},
                driver_proc=(901, 91),
                proc_alive=proc_alive,
                signal_pids=signal_pids,
                approved_chromium_alive=lambda: not chromium_killed.is_set(),
                wait_seconds="0",
                chromium_grace="0.2",
                driver_grace="0.2",
                kill_grace="0.2",
            ):
                stuck_browser = FakeBrowser(close_behaviour=lambda _s: never_returns.wait())
                leased = launch_playwright_chromium(fake_playwright(browser=stuck_browser), headless=True)

                stuck_thread = threading.Thread(target=leased.close, daemon=True)
                stuck_thread.start()

                # The guard escalates on its own timeline, independent of the
                # wedged owning thread.
                deadline = time.monotonic() + 3
                while not (chromium_killed.is_set() and driver_killed.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(chromium_killed.is_set(), "guard did not SIGKILL Chromium")
                self.assertTrue(driver_killed.is_set(), "guard did not SIGKILL the wedged driver")

                # close() is still stuck; lease still held by the wedged browser...
                self.assertFalse(leased._released)
                self.assertTrue(ob._browser_launch_lease.locked())

                # ...but a fresh launch reclaims it: the recorded tree is gone.
                with self.assertLogs("backend.outbound_security", level="WARNING") as logs:
                    recovered = launch_playwright_chromium(fake_playwright(browser=FakeBrowser()), headless=True)
                self.assertTrue(any("Reclaiming a leaked Chromium launch lease" in m for m in logs.output))
                self.assertIs(ob._current_leased_browser, recovered)
                recovered.close()
        finally:
            never_returns.set()  # let the stuck thread unwind


class SpaShellHeuristicTests(unittest.TestCase):
    def test_unrendered_phenom_shell_detected(self) -> None:
        from collectors.generic_collector import looks_like_unrendered_spa

        html = (
            '<html><body><div id="root"></div>'
            '<script src="/main.js"></script><script>window.pageStateData={}</script>'
            'No results for "${pageStateData.searchKeyword}"</body></html>'
        )
        self.assertTrue(looks_like_unrendered_spa(html, ""))

    def test_rendered_page_with_explicit_no_openings_is_not_a_shell(self) -> None:
        from collectors.generic_collector import looks_like_unrendered_spa, page_states_no_openings

        text = "Careers. We have no current openings at this time. Please check back later."
        self.assertTrue(page_states_no_openings(text))
        html = f"<html><body><main><h1>Careers</h1><p>{text}</p></main></body></html>"
        self.assertFalse(looks_like_unrendered_spa(html, text))

    def test_rendered_listing_page_is_not_a_shell(self) -> None:
        from collectors.generic_collector import looks_like_unrendered_spa

        html = (
            "<html><body><ul>"
            + "".join(f"<li>Job {i} - Location {i} - apply</li>" for i in range(8))
            + "</ul></body></html>"
        )
        self.assertFalse(looks_like_unrendered_spa(html, "Job 1 Location 1 Job 2 Location 2 Job 3 Location 3"))


class GenericCollectorAuthoritativeTests(unittest.TestCase):
    def _company(self) -> dict:
        return {
            "Company Name": "Example Bank",
            "Company ID": "company-example",
            "Job Board URL": "https://careers.example.com/us/en/search-results",
            "Job Platform": "Company Careers Site",
        }

    def test_browser_failure_with_empty_http_fallback_is_not_authoritative(self) -> None:
        from collectors.generic_collector import GenericCollector
        from job_tools import CollectionNotAuthoritative

        collector = GenericCollector(delay_seconds=0)
        with (
            patch.object(GenericCollector, "collect_with_browser", side_effect=RuntimeError("lease held")),
            patch.object(GenericCollector, "collect_with_http", return_value=[]),
        ):
            with self.assertRaises(CollectionNotAuthoritative) as ctx:
                collector.collect(self._company())
        self.assertEqual(ctx.exception.partial_jobs, [])

    def test_browser_failure_with_partial_http_is_still_not_authoritative(self) -> None:
        from collectors.generic_collector import GenericCollector
        from job_tools import CollectionNotAuthoritative

        collector = GenericCollector(delay_seconds=0)
        partial = ["job-a", "job-b"]
        with (
            patch.object(GenericCollector, "collect_with_browser", side_effect=RuntimeError("lease held")),
            patch.object(GenericCollector, "collect_with_http", return_value=partial),
        ):
            with self.assertRaises(CollectionNotAuthoritative) as ctx:
                collector.collect(self._company())
        # Partial finds are carried for additive retention, NOT returned as a
        # complete result.
        self.assertEqual(ctx.exception.partial_jobs, partial)

    def test_spa_shell_render_raises_not_authoritative_without_http_retry(self) -> None:
        from collectors.generic_collector import GenericCollector
        from job_tools import CollectionNotAuthoritative

        shell = (
            '<html><body><div id="app"></div>'
            '<script src="/a.js"></script><script src="/b.js"></script>'
            '<script>var x = "${pageStateData.searchKeyword}"</script></body></html>'
        )

        class _Page:
            url = "https://careers.example.com/us/en/search-results"

            def goto(self, *a, **k):
                return None

            def wait_for_load_state(self, *a, **k):
                return None

            def content(self):
                return shell

            def locator(self, *_a, **_k):
                return Mock(inner_text=Mock(return_value=""))

        class _Ctx:
            def set_default_timeout(self, _ms):
                return None

            def set_default_navigation_timeout(self, _ms):
                return None

            def new_page(self):
                return _Page()

            def close(self):
                return None

        fake_browser = Mock()
        fake_browser.new_context.return_value = _Ctx()
        fake_browser.close.return_value = None

        pw_cm = Mock()
        pw_cm.__enter__ = Mock(return_value=Mock())
        pw_cm.__exit__ = Mock(return_value=False)

        collector = GenericCollector(delay_seconds=0)
        http_called = Mock()
        with (
            patch("playwright.sync_api.sync_playwright", return_value=pw_cm),
            patch("collectors.generic_collector.launch_playwright_chromium", return_value=fake_browser),
            patch("collectors.generic_collector.install_playwright_url_guard"),
            patch("collectors.generic_collector.safe_page_goto"),
            patch.object(GenericCollector, "collect_with_http", side_effect=http_called),
        ):
            with self.assertRaises(CollectionNotAuthoritative):
                collector.collect(self._company())
        http_called.assert_not_called()
        fake_browser.close.assert_called_once()

    def test_context_gets_a_default_action_timeout_and_content_hang_is_bounded(self) -> None:
        # A pre-close call without an explicit timeout= (page.content()) must not
        # hang the owning thread: the context default timeout gives it a ceiling,
        # and a resulting PlaywrightTimeoutError degrades to a non-authoritative
        # result (existing jobs retained), never a stuck run.
        from collectors.generic_collector import BROWSER_ACTION_TIMEOUT_MS, GenericCollector
        from job_tools import CollectionNotAuthoritative

        class _FakeTimeout(Exception):
            pass

        recorded = {}

        class _Page:
            url = "https://careers.example.com/jobs"

            def wait_for_load_state(self, *a, **k):
                return None

            def content(self):
                raise _FakeTimeout("Timeout 60000ms exceeded")

            def locator(self, *_a, **_k):
                return Mock(inner_text=Mock(return_value=""))

        class _Ctx:
            def set_default_timeout(self, ms):
                recorded["action"] = ms

            def set_default_navigation_timeout(self, ms):
                recorded["nav"] = ms

            def new_page(self):
                return _Page()

            def close(self):
                return None

        fake_browser = Mock()
        fake_browser.new_context.return_value = _Ctx()
        pw_cm = Mock()
        pw_cm.__enter__ = Mock(return_value=Mock())
        pw_cm.__exit__ = Mock(return_value=False)

        collector = GenericCollector(delay_seconds=0)
        with (
            patch("playwright.sync_api.sync_playwright", return_value=pw_cm),
            patch("collectors.generic_collector.launch_playwright_chromium", return_value=fake_browser),
            patch("collectors.generic_collector.install_playwright_url_guard"),
            patch("collectors.generic_collector.safe_page_goto"),
            patch.object(GenericCollector, "collect_with_http", return_value=[]),
        ):
            with self.assertRaises(CollectionNotAuthoritative):
                collector.collect(self._company())

        self.assertEqual(recorded.get("action"), BROWSER_ACTION_TIMEOUT_MS)
        self.assertEqual(recorded.get("nav"), BROWSER_ACTION_TIMEOUT_MS)
        fake_browser.close.assert_called_once()


class PaycorCollectorAuthoritativeTests(unittest.TestCase):
    def test_missing_listing_frame_raises_not_authoritative_and_closes_browser(self) -> None:
        from collectors import paycor_collector as pc
        from job_tools import CollectionNotAuthoritative

        browser = Mock()
        pw_cm = Mock()
        pw_cm.__enter__ = Mock(return_value=Mock())
        pw_cm.__exit__ = Mock(return_value=False)

        collector = pc.PaycorCollector(delay_seconds=0)
        company = {
            "Company Name": "Frame Missing CU",
            "Company ID": "company-frame-missing",
            "Job Board URL": "https://recruiting.paylocity.com/recruiting/jobs/All/x",
            "Job Platform": "Paycor",
        }
        with (
            patch("playwright.sync_api.sync_playwright", return_value=pw_cm),
            patch.object(pc, "launch_playwright_chromium", return_value=browser),
            patch.object(pc, "install_playwright_url_guard"),
            patch.object(pc, "safe_page_goto"),
            patch.object(pc, "find_paycor_frame", return_value=None),
            patch.object(pc.PaycorCollector, "resolve_embedded_job_board_url", side_effect=lambda url, _p: url),
        ):
            with self.assertRaises(CollectionNotAuthoritative):
                collector.collect(company)
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
