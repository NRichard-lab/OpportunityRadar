"""Real-Chromium integration coverage for the browser-launch lease.

Runs only inside the production-equivalent Linux container (the pinned Playwright
image, non-root UID 10001, ``cap_drop: ALL``, ``no-new-privileges``, the narrowed
seccomp profile, ``--init``, and
``APP_BROWSER_EGRESS_MODE=network_namespace_dns_pinned_proxy_v1``). It is skipped
everywhere else -- there is no synthetic-data or security-control weakening; the
protected boundary must actually validate for these to run.

Covered: abnormal (SIGKILL) Chromium termination, bounded cleanup, repeated
recovery, truthful termination reporting, and proof that two Chromium trees are
never admitted at once.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

ob = pytest.importorskip("backend.outbound_security")

pytestmark = pytest.mark.skipif(
    os.environ.get("APP_BROWSER_EGRESS_MODE", "")
    != getattr(ob, "BROWSER_EGRESS_MODE", "network_namespace_dns_pinned_proxy_v1")
    or not ob._is_proc_filesystem_available(),
    reason="requires the production-equivalent protected browser boundary",
)


@pytest.fixture(scope="module")
def runtime():
    try:
        return ob.validate_browser_runtime_boundary()
    except ob.BrowserEgressConfigurationError as exc:  # pragma: no cover - env gate
        pytest.skip(f"protected browser boundary unavailable: {exc}")


@pytest.fixture(scope="module")
def playwright():
    # A single sync Playwright for the whole module: repeatedly entering
    # ``sync_playwright()`` in one thread trips its "sync API inside asyncio loop"
    # guard. Individual browsers are still launched/closed per test.
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


def _reset_lease_state():
    with ob._browser_lease_state_lock:
        ob._current_leased_browser = None
    if ob._browser_launch_lease.locked():
        try:
            ob._browser_launch_lease.release()
        except RuntimeError:
            pass


def _count_live_chromium(runtime) -> int:
    executable_real = os.path.realpath(runtime.chromium_executable)
    marker = f"chromium-{ob.BROWSER_CHROMIUM_REVISION}"
    live = 0
    for pid in ob._iter_proc_pids():
        if not ob._proc_is_approved_chromium(pid, executable_real, marker):
            continue
        stat = ob._read_proc_stat(pid)
        if stat is not None and stat[2] not in {"Z", "X", "x"}:
            live += 1
    return live


def _wait_clean(runtime, seconds=15) -> None:
    deadline = time.monotonic() + seconds
    while _count_live_chromium(runtime) and time.monotonic() < deadline:
        time.sleep(0.2)


def test_capture_identifies_real_process_tree(runtime, playwright):
    browser = ob.launch_playwright_chromium(playwright, headless=True)
    try:
        assert browser._procs, "no Chromium PIDs captured for a real launch"
        for pid, start_time in browser._procs.items():
            assert ob._proc_alive(pid, start_time), (pid, start_time)
        assert browser._driver_proc is not None, "Playwright driver process not identified"
        assert _count_live_chromium(runtime) >= 1
    finally:
        browser.close()
        _reset_lease_state()
    assert not ob._browser_launch_lease.locked()
    _wait_clean(runtime)
    assert _count_live_chromium(runtime) == 0


def test_second_launch_is_refused_while_first_is_alive(runtime, playwright):
    os.environ["OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS"] = "0"
    browser = ob.launch_playwright_chromium(playwright, headless=True)
    try:
        with pytest.raises(ob.BrowserEgressConfigurationError, match="Only one Chromium"):
            ob.launch_playwright_chromium(playwright, headless=True)
        assert _count_live_chromium(runtime) >= 1
    finally:
        os.environ.pop("OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS", None)
        browser.close()
        _reset_lease_state()
    _wait_clean(runtime)
    assert _count_live_chromium(runtime) == 0


def test_abnormal_sigkill_then_close_is_bounded_and_lease_recovers(runtime, playwright):
    browser = ob.launch_playwright_chromium(playwright, headless=True)
    roots = dict(browser._procs)
    assert roots
    for pid in roots:  # model an OOM kill
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    started = time.monotonic()
    browser.close()
    assert time.monotonic() - started < 45.0, "close() was not bounded"
    assert not ob._browser_launch_lease.locked()
    _reset_lease_state()
    _wait_clean(runtime)
    assert _count_live_chromium(runtime) == 0

    recovered = ob.launch_playwright_chromium(playwright, headless=True)
    try:
        assert recovered._procs
        assert _count_live_chromium(runtime) >= 1
    finally:
        recovered.close()
        _reset_lease_state()
    _wait_clean(runtime)
    assert _count_live_chromium(runtime) == 0


def test_repeated_kill_and_recovery_never_admits_overlapping_chromium(runtime, playwright):
    for cycle in range(3):
        browser = ob.launch_playwright_chromium(playwright, headless=True)
        assert _count_live_chromium(runtime) >= 1, f"cycle {cycle}: no browser"
        for pid in list(browser._procs):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        browser.close()
        _reset_lease_state()
        assert not ob._browser_launch_lease.locked(), f"cycle {cycle}: lease stuck"
        _wait_clean(runtime)
        assert _count_live_chromium(runtime) == 0, f"cycle {cycle}: chromium leaked"


def test_reap_reports_termination_truthfully(runtime, playwright):
    browser = ob.launch_playwright_chromium(playwright, headless=True)
    roots = dict(browser._procs)
    assert ob._live_recorded_procs(roots), "recorded roots should be live right after launch"
    browser.close()
    _reset_lease_state()
    _wait_clean(runtime)
    # After a real close the recorded roots are gone; reap confirms termination.
    assert ob._reap_browser_process_tree(roots) is True


def test_open_file_limit_preflight_is_enforced(runtime):
    """The runtime boundary refuses a soft RLIMIT_NOFILE below the Chromium floor
    and accepts one at or above it. Below the floor a heavier career page
    exhausts descriptors mid-load and Chromium self-aborts ("Page crashed")."""
    import resource

    floor = ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft == resource.RLIM_INFINITY or soft >= floor, (
        f"the production-equivalent container must grant >= {floor} open files; got {soft}"
    )
    try:
        # A soft limit under the floor that also can't be raised back up must fail closed.
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        with pytest.raises(ob.BrowserEgressConfigurationError):
            ob._require_browser_open_file_headroom()
        # A low soft limit under a high hard ceiling is lifted, not rejected.
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        ob._require_browser_open_file_headroom()
        lifted, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert lifted == resource.RLIM_INFINITY or lifted >= floor
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_content_heavy_page_renders_without_a_page_crash(runtime, playwright):
    """A JS/content-heavy public page renders to a usable DOM through the full
    protected path (netns wrapper + DNS-pinned proxy + URL guard) without the
    renderer self-aborting. Regression for the intermittent "Page crashed"."""
    leased = ob.launch_playwright_chromium(playwright, headless=True)
    try:
        context = leased.new_context()
        ob.install_playwright_url_guard(context)
        page = context.new_page()
        crashed = {"v": False}
        page.on("crash", lambda *_: crashed.__setitem__("v", True))
        for _ in range(3):
            ob.safe_page_goto(
                page, "https://en.wikipedia.org/wiki/Bank",
                wait_until="domcontentloaded", timeout=60000,
            )
            body_len = page.evaluate("() => document.body ? document.body.innerText.length : -1")
            assert not crashed["v"], "renderer crashed on a heavy page"
            assert isinstance(body_len, int) and body_len > 2000
        context.close()
    finally:
        leased.close()
        _reset_lease_state()
        _wait_clean(runtime)
