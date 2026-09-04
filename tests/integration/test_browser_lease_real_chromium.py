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
import subprocess
import sys
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
    import subprocess
    import sys
    import textwrap

    floor = ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft == resource.RLIM_INFINITY or soft >= floor, (
        f"the production-equivalent container must grant >= {floor} open files; got {soft}"
    )

    # Lowering the *hard* limit is irreversible without privilege, so the
    # fail-closed case runs in a throwaway interpreter. In-process it would pin
    # this container's ceiling at 1024 and break every later browser test.
    script = textwrap.dedent(
        """
        import resource
        from backend import outbound_security as ob

        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        try:
            ob._require_browser_open_file_headroom()
        except ob.BrowserEgressConfigurationError as exc:
            assert str(ob.MINIMUM_BROWSER_OPEN_FILE_LIMIT) in str(exc), str(exc)
            print("RAISED")
        else:
            print("NOT_RAISED")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, cwd="/app"
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "RAISED"

    # A low soft limit under a high hard ceiling is lifted, not rejected. Only the
    # soft limit changes here, so it is restorable in this process.
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        ob._require_browser_open_file_headroom()
        lifted, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert lifted == resource.RLIM_INFINITY or lifted >= floor
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    restored_soft, restored_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert (restored_soft, restored_hard) == (soft, hard), (
        "the preflight check must leave this container's file-descriptor limits intact"
    )


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


# --- Regression: 2026-09-04 Playwright driver death -> 100% CPU spin ----------
#
# The driver died from an uncaught assertion ("Invalid InterceptionId" at
# coreBundle.js:34801) and the worker thread then spun inside Browser.close() at
# ~91-97% of one core for 37 minutes, holding the mutation gate the whole time
# while /api/health -- which checks storage only -- kept reporting healthy.
#
# Each scenario runs in its OWN process. A dead driver poisons the Playwright
# instance that owns it, so these cannot share the module-scoped fixture; and a
# separate process is the only honest way to assert "this work returned to idle",
# because the whole failure mode is a thread that never returns.

_DRIVER_DEATH_SCENARIO = '''
import os, signal, sys, time
sys.path.insert(0, "/app")
import backend.outbound_security as ob
from playwright.sync_api import sync_playwright

ROUNDS = int(sys.argv[1])


def ticks():
    with open("/proc/self/stat") as handle:
        rest = handle.read().split(") ", 1)[1].split()
    return int(rest[11]) + int(rest[12])


for attempt in range(ROUNDS):
    playwright = sync_playwright().start()
    leased = ob.launch_playwright_chromium(playwright, headless=True)
    context = leased.new_context()
    ob.install_playwright_url_guard(context)
    page = context.new_page()
    page.goto("data:text/html,<h1>careers</h1>", wait_until="domcontentloaded")

    driver_pid, driver_start = leased._driver_proc
    assert ob._proc_alive(driver_pid, driver_start), "driver not alive before the kill"
    os.kill(driver_pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while ob._proc_alive(driver_pid, driver_start) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not ob._proc_alive(driver_pid, driver_start), "driver did not die"
    print("DRIVER_DEAD attempt=%d" % attempt, flush=True)

    started = time.monotonic()
    before = ticks()
    try:
        leased.close()
        outcome = "returned"
    except ob.BrowserTeardownAbandoned:
        outcome = "abandoned"
    except Exception as exc:
        outcome = "raised:%s" % type(exc).__name__
    elapsed = time.monotonic() - started
    during = ticks() - before
    print("CLOSE attempt=%d outcome=%s elapsed=%.1f ticks=%d" % (attempt, outcome, elapsed, during), flush=True)

    try:
        playwright.stop()
    except Exception:
        pass

    ob._reset = None
    with ob._browser_lease_state_lock:
        ob._current_leased_browser = None
    if ob._browser_launch_lease.locked():
        try:
            ob._browser_launch_lease.release()
        except RuntimeError:
            pass

    idle_before = ticks()
    time.sleep(3.0)
    idle = ticks() - idle_before
    print("IDLE attempt=%d ticks_per_3s=%d" % (attempt, idle), flush=True)

print("SCENARIO_COMPLETE", flush=True)
'''


def _run_driver_death_scenario(rounds: int, timeout: int):
    completed = subprocess.run(
        [sys.executable, "-c", _DRIVER_DEATH_SCENARIO, str(rounds)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/app",
    )
    return completed


def _parse(prefix: str, stdout: str) -> list[dict]:
    rows = []
    for line in stdout.splitlines():
        if not line.startswith(prefix + " "):
            continue
        row = {}
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            row[key] = value
        rows.append(row)
    return rows


def test_driver_death_teardown_is_bounded_and_returns_to_idle(runtime):
    """The exact production failure: kill the driver, then tear down.

    The process must finish at all (the bug was an unbounded spin), close() must
    complete inside its hard bound, and the process must go back to idle.
    """
    completed = _run_driver_death_scenario(rounds=1, timeout=300)
    assert "SCENARIO_COMPLETE" in completed.stdout, (
        f"scenario did not finish\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr[-3000:]}"
    )
    assert completed.returncode == 0, completed.stderr[-3000:]

    close_rows = _parse("CLOSE", completed.stdout)
    assert close_rows, completed.stdout
    bound = 200.0
    for row in close_rows:
        assert float(row["elapsed"]) < bound, f"close() took {row['elapsed']}s: {completed.stdout}"
        assert row["outcome"] in {"returned", "abandoned"} or row["outcome"].startswith("raised:"), row

    for row in _parse("IDLE", completed.stdout):
        burned = int(row["ticks_per_3s"])
        assert burned < 100, (
            f"process burned {burned} ticks in 3s after teardown "
            f"({burned / 3.0:.0f}% of one core) -- it is spinning, not idle\n{completed.stdout}"
        )


def test_repeated_driver_death_always_returns_to_idle(runtime):
    """Stress: recovery must hold over repeated crashes, not just the first."""
    completed = _run_driver_death_scenario(rounds=3, timeout=600)
    assert "SCENARIO_COMPLETE" in completed.stdout, (
        f"scenario did not finish\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr[-3000:]}"
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    idle_rows = _parse("IDLE", completed.stdout)
    assert len(idle_rows) == 3, completed.stdout
    for index, row in enumerate(idle_rows):
        burned = int(row["ticks_per_3s"])
        assert burned < 100, f"attempt {index} still burning CPU ({burned} ticks/3s)\n{completed.stdout}"


_RELAUNCH_SPIN_SCENARIO = '''
import os, signal, sys, time
sys.path.insert(0, "/app")
import backend.outbound_security as ob
from playwright.sync_api import sync_playwright


def ticks():
    with open("/proc/self/stat") as handle:
        rest = handle.read().split(") ", 1)[1].split()
    return int(rest[11]) + int(rest[12])


playwright = sync_playwright().start()
leased = ob.launch_playwright_chromium(playwright, headless=True)
context = leased.new_context()
ob.install_playwright_url_guard(context)
page = context.new_page()
page.goto("data:text/html,<h1>careers</h1>", wait_until="domcontentloaded")

driver_pid, driver_start = leased._driver_proc
os.kill(driver_pid, signal.SIGKILL)
deadline = time.monotonic() + 15
while ob._proc_alive(driver_pid, driver_start) and time.monotonic() < deadline:
    time.sleep(0.1)
assert not ob._proc_alive(driver_pid, driver_start), "driver did not die"

try:
    leased.close()
except Exception:
    pass
with ob._browser_lease_state_lock:
    ob._current_leased_browser = None
if ob._browser_launch_lease.locked():
    try:
        ob._browser_launch_lease.release()
    except RuntimeError:
        pass

# Reusing this Playwright instance is what spun a whole core in production.
start = time.monotonic()
before = ticks()
try:
    ob.launch_playwright_chromium(playwright, headless=True)
    outcome = "returned"
except ob.BrowserTeardownAbandoned:
    outcome = "abandoned"
except BaseException as exc:
    outcome = "raised:%s" % type(exc).__name__
print("RELAUNCH outcome=%s elapsed=%.1f ticks=%d" % (outcome, time.monotonic() - start, ticks() - before), flush=True)

idle_before = ticks()
time.sleep(3.0)
print("IDLE ticks_per_3s=%d" % (ticks() - idle_before), flush=True)
print("SCENARIO_COMPLETE", flush=True)
'''


def test_relaunch_after_driver_death_cannot_spin_a_core(runtime):
    """Regression for the measured 99%-of-a-core spin.

    A Playwright instance whose driver has died is poisoned: a later
    ``chromium.launch()`` on it busy-loops in Playwright's sync pump instead of
    failing. It must be abandoned by the hard bound, and the process must return
    to idle rather than pinning a CPU.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _RELAUNCH_SPIN_SCENARIO],
        capture_output=True,
        text=True,
        timeout=400,
        env={**os.environ, "OPPORTUNITY_RADAR_BROWSER_LAUNCH_BOUND_SECONDS": "15"},
        cwd="/app",
    )
    assert "SCENARIO_COMPLETE" in completed.stdout, (
        f"the relaunch never finished -- it is still spinning\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr[-3000:]}"
    )
    relaunch = _parse("RELAUNCH", completed.stdout)
    assert relaunch, completed.stdout
    assert relaunch[0]["outcome"] in {"abandoned", "returned"} or relaunch[0]["outcome"].startswith(
        "raised:"
    ), relaunch[0]
    assert float(relaunch[0]["elapsed"]) < 60.0, completed.stdout

    idle = _parse("IDLE", completed.stdout)
    assert idle, completed.stdout
    burned = int(idle[0]["ticks_per_3s"])
    assert burned < 100, (
        f"still burning CPU after the abandonment: {burned} ticks in 3s "
        f"({burned / 3.0:.0f}% of one core)\n{completed.stdout}"
    )
