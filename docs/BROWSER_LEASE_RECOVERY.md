# Browser launch-lease recovery and non-authoritative collection

## Problem

`backend/outbound_security.py` gates Chromium behind a process-global
`threading.Lock` (`_browser_launch_lease`). The lock was acquired
**non-blocking** and released only by `_LeasedBrowser.close()` / the browser
`disconnected` event. In production the backend container's `mem_limit` (768 MiB)
is smaller than "Uvicorn baseline (~640 MiB) + one headless Chromium tree", so the
OOM killer repeatedly kills Chromium (`oom_score_adj: 300`). When that happened
the release path did not always complete, the lease stayed held for the life of
the single Uvicorn worker, and every later browser discovery/collection failed
with `Only one Chromium process may run at a time` and silently degraded to an
empty (falsely authoritative) result — pruning existing jobs. 156 companies were
observed in this state.

## Lease recovery design

* **PID + start-time identity.** `_capture_browser_procs()` records `pid ->
  start_time` (from `/proc/<pid>/stat` field 22) for the approved-Chromium
  processes that are **descendants of this process**. `_find_playwright_driver_proc()`
  records the node driver the same way. `_proc_alive(pid, start_time)` re-reads
  the stat file and requires the start-time to match, so a reused PID counts as
  dead. A zombie / dead task (`state` in `Z X x`) also counts as terminated.
* **Termination is restricted to the owned tree.** Signals only ever target
  `_live_tree(recorded)` — descendants of *still-alive recorded roots* plus those
  roots — recomputed from live `/proc` each pass, so it can never expand to an
  unrelated Chromium.
* **Bounded acquisition, then provable-termination reclaim.**
  `_acquire_browser_launch_lease()` waits up to
  `OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS`, then reclaims **only if** every
  recorded root is dead by identity **and** no approved Chromium process exists
  anywhere. It never force-releases based on lease age, owning-thread liveness, or
  `browser.is_connected()`. A bounded wait alone is not the fix.
* **Identity-guarded release.** `_LeasedBrowser._release()` is idempotent
  (`_released` flag) and releases the lock only while `_current_leased_browser is
  self`, so a late `disconnected` / `close` callback from a superseded browser
  cannot free a lease a newer browser now owns.
* **`_CloseGuard` — bounded cleanup compatible with Playwright thread ownership.**
  A dedicated thread per `close()` that sends **OS signals only** (never touches
  Playwright objects). Escalation: after `chromium_grace`, SIGKILL the recorded
  Chromium tree; if `close()` is still blocked `driver_grace` later (Chromium
  gone, node driver wedged), SIGKILL the driver so the owning thread's blocked
  pipe read returns. `close()` calls `guard.finish()` (which **joins** the guard
  thread) before releasing the lease, so the guard is always wound down before
  the next browser is admitted.
* **Fail closed on uncertainty.** `_reap_browser_process_tree()` returns a bool;
  `close()` releases the lease **only if termination is proven and the guard
  finished**. Otherwise it logs `CRITICAL` and *holds* the lease; a later launch
  reclaims it only once the recorded processes are actually gone.

### What the guard covers, and what it does not

`_CloseGuard` starts **inside `_LeasedBrowser.close()`**. It bounds the *close*
path and the *lease*. It does **not** bound an owning Playwright call that hangs
*before* `close()` is reached (context/page creation, navigation, `page.content()`,
locator actions):

* **Navigation and load-state waits are already bounded** by the explicit
  `timeout=` passed at every call site — `safe_page_goto(..., timeout=45000)`,
  `wait_for_load_state(..., timeout=15000)`, `locator(...).inner_text(timeout=...)`.
  A timeout raises `PlaywrightTimeoutError`, which the collectors handle (either
  swallow, or fall through to the non-authoritative path — existing jobs retained).
* **Calls without an explicit `timeout=`** — notably `page.content()` — now get a
  ceiling from `context.set_default_timeout(BROWSER_ACTION_TIMEOUT_MS)` /
  `set_default_navigation_timeout(...)` set immediately after `new_context()` in
  `GenericCollector.collect_with_browser`. A `page.content()` hang therefore
  raises after `BROWSER_ACTION_TIMEOUT_MS` (60 s) and degrades to a
  non-authoritative result. Verified by
  `tests/test_browser_lease_recovery.py::GenericCollectorAuthoritativeTests::test_context_gets_a_default_action_timeout_and_content_hang_is_bounded`.
* **`browser.new_context()` and `context.new_page()` are still not independently
  bounded** — Playwright exposes no timeout on them. In practice they are instant;
  a hang there requires an already-wedged browser process.

**Lease recovery does not make a stuck maintenance run complete.** If an owning
Playwright call hangs before `close()` (and no `timeout=` or default-timeout
applies, e.g. a wedged `new_page()`), the owning `ThreadPoolExecutor` worker
blocks, `run_collection_group`'s `shutdown(wait=True)` blocks, and that
`collect_jobs` invocation never returns. The `_CloseGuard` never started, so no
process reaping happens from that path; the lease stays held. Recovery is only for
the *next* run: once that browser's process tree actually dies (crash / OOM /
operator kill), a subsequent launch's reclaim check sees the recorded roots gone
and proceeds — the earlier run and its worker thread are lost until the Uvicorn
worker restarts.

If `close()` itself never returns **and** killing both the Chromium tree and the
node driver does not unblock it (no known Playwright code path), the same applies:
the guard has still killed the whole tree so the next launch reclaims, but the
stuck `close()` thread is lost until restart. Covered by
`tests/test_browser_lease_recovery.py::CloseCleanupBoundingTests::test_lease_recovers_even_if_close_never_returns`.

## Non-authoritative collection

`job_tools.CollectionNotAuthoritative(message, *, partial_jobs=None)` marks a
refresh as blocked / incomplete / uncertain. `run_collection_group` treats it as a
collector failure: the company is **not** counted successful, so
`merge_with_existing_jobs` retains its existing jobs; `partial_jobs` are enriched,
validated and merged **additively** (union by source URL); the run is reported
`outcome="partial"`, `dataDisposition="retained"`.

* `GenericCollector.collect()` — a browser failure **always** raises
  `CollectionNotAuthoritative` (carrying any HTTP-fallback finds as
  `partial_jobs`), even when the fallback found postings: an incomplete static
  scrape is never authoritative.
* `GenericCollector.collect_with_browser()` — if zero listings parsed, the page
  did not state an explicit "no openings" message (`page_states_no_openings()`),
  and the document looks like an unrendered single-page-app shell
  (`looks_like_unrendered_spa()`), it raises `CollectionNotAuthoritative` without
  an HTTP retry (the fallback would fetch the same shell).
* A fully rendered page with zero cards, or an explicit "no openings" message, is
  still an **authoritative zero** and prunes as before.
* `PaycorCollector` — browser lifetime wrapped in `try/finally: browser.close()`
  (was a real lease-leak site); a missing listing frame raises
  `CollectionNotAuthoritative` instead of returning `[]`.

## New environment settings

All optional; safe defaults; **not** set in the production env file by this
change. Read at call time.

| Variable | Default | Bounds |
| --- | --- | --- |
| `OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS` | `30` | How long a new launch waits for a busy lease before it fails / attempts reclaim. `0` = fail fast (no wait). Only relevant under genuine contention, which the single Uvicorn worker + `APP_MAX_BROWSER_WORKERS=1` + `APP_MAX_ACTIVE_MAINTENANCE=1` makes rare. |
| `OPPORTUNITY_RADAR_BROWSER_CLOSE_GRACE_SECONDS` | `12` | Seconds `close()` may run before `_CloseGuard` force-kills the recorded Chromium tree. Too low ⇒ a slow-but-healthy `close()` is killed; too high ⇒ a hung `close()` blocks its worker thread longer. |
| `OPPORTUNITY_RADAR_BROWSER_DRIVER_GRACE_SECONDS` | `6` | Extra seconds after the Chromium tree is gone before the guard SIGKILLs the wedged node driver. |
| `OPPORTUNITY_RADAR_BROWSER_KILL_GRACE_SECONDS` | `5` | Wait after each escalation signal (SIGTERM, then SIGKILL) in `_reap_browser_process_tree`. |

`collectors/generic_collector.py` also sets `BROWSER_ACTION_TIMEOUT_MS = 60000` as
the Playwright context default timeout (see "What the guard covers" above).

## Production memory limit

`compose.production.yaml` raises the backend `mem_limit` from `768m` to `1536m`.
The no-browser baseline is ~640 MiB; a headless Chromium tree adds ~300–500 MiB,
so 768 MiB caused the OOM killer to take Chromium mid-run (`oom_score_adj: 300`),
which is what stranded the lease. This is the **only** production-config change
and it changes **no** other limit: one backend replica (no `deploy.replicas`),
one Uvicorn worker (image `CMD ... --workers 1`), one browser
(`APP_MAX_BROWSER_WORKERS=1`), and every security restriction (`user: 10001:10001`,
`cap_drop: ALL`, `no-new-privileges`, `seccomp`, `read_only`, tmpfs mounts,
`pids_limit`, `cpus`) are unchanged. Frontend `mem_limit` (128m), Blue Ash Portal
limits, and host swap are untouched. `compose.smoke.yaml` still specifies `768m`
for the local rehearsal stack — updating it for parity is an optional follow-up.

Worst-case bounded `close()` on the hung path ≈ `CLOSE_GRACE + DRIVER_GRACE +
3·KILL_GRACE` ≈ 33 s with defaults; the healthy path adds ~0 ms (the guard's
done-event is set the moment `close()` returns).

## Reproducing the real-Chromium integration tests

These run **only** inside a production-equivalent Linux container and are skipped
elsewhere (including the normal `tests/` run). They do not use production data or
weaken any security control — the protected browser boundary must *validate* for
them to run.

Image identity (from `docker/backend/Dockerfile`):

```
base image: mcr.microsoft.com/playwright/python:v1.62.0-noble
            @sha256:51d31fdfacb0cff99a1a724152e34ae408d2bd4e7da310ff157450f49261cc59
Playwright 1.62.0 · Chromium 151.0.7922.34 (revision 1234)
```

Build the backend image and a throwaway test image, then run:

```sh
# 1. Production backend image
docker build --file docker/backend/Dockerfile \
  --build-arg DEPLOYMENT_VERSION="$(git rev-parse HEAD)" \
  --tag opportunity-radar-backend:lease-review .

# 2. Test image: same source + pytest + the integration test
#    (.dockerignore excludes tests/, so stage the one file at the repo root)
cp tests/integration/test_browser_lease_real_chromium.py ./_lease_real_test.py
cat > Dockerfile.leasetest <<'DOCKER'
FROM opportunity-radar-backend:lease-review
USER 0:0
RUN python -m pip install --no-cache-dir pytest==9.1.1
COPY _lease_real_test.py /app/_lease_real_test.py
RUN chown 10001:10001 /app/_lease_real_test.py
USER 10001:10001
DOCKER
docker build -f Dockerfile.leasetest -t opportunity-radar-backend:lease-test .
rm -f ./_lease_real_test.py Dockerfile.leasetest

# 3. Run under the production security posture
docker run --rm --init \
  --user 10001:10001 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --security-opt seccomp="$(pwd)/docker/backend/seccomp_profile.json" \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1770,uid=10001,gid=10001 \
  --tmpfs /var/lib/opportunity-radar/tmp:rw,nosuid,nodev,size=64m,mode=1770,uid=10001,gid=10001 \
  --tmpfs /dev/shm:rw,nosuid,nodev,size=512m,mode=1770,uid=10001,gid=10001 \
  -e APP_ENV=production \
  -e APP_BROWSER_EGRESS_MODE=network_namespace_dns_pinned_proxy_v1 \
  -e HOME=/tmp -e TMPDIR=/tmp \
  opportunity-radar-backend:lease-test \
  python -m pytest -rs -v -o addopts= /app/_lease_real_test.py
```

Coverage: real process-tree + driver capture, "one Chromium at a time"
enforcement, SIGKILL → bounded `close()` → lease recovery, 3× repeated
kill/recover with no overlapping Chromium, truthful termination reporting.

On Windows/Git-Bash prefix the `docker run` with `MSYS_NO_PATHCONV=1` and pass the
seccomp path Windows-style. Docker Desktop's LinuxKit VM is sufficient; the VPS's
own kernel is not exercised until this runs there or in its CI.
