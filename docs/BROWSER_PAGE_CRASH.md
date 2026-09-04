# Chromium "Page crashed" on browser-dependent career sites

## Symptom

Browser collections and company-info discovery for heavier career pages
(First Shore Federal, Golden 1's Dayforce board, and a long tail of
`Company Careers Site` domains) fail with:

```
playwright._impl._errors.TargetClosedError: Page.goto: Page crashed
playwright._impl._errors.Error: Navigation failed because page crashed!
```

`run_collection_group` already treats this as a collector failure, so it is
non-authoritative: existing jobs are retained and the refresh reports
`Failed` / `partial` (see `docs/BROWSER_LEASE_RECOVERY.md`). No false zero is
recorded. The problem is that the page never renders, so those companies cannot
gain jobs at all.

## Diagnosis (what it is, and what it is not)

Captured crash signatures from 49 real production minidumps
(`/tmp/.config/google-chrome-for-testing/Crash Reports/`):

| count | signal | faulting RIP |
|------:|--------|--------------|
| 44 | `SIGTRAP` (0x5) | fixed address in `chrome` |
| 5 | `SIGABRT` (0x6) | `libc` `abort()` |

`SIGTRAP` at a fixed code address with `libc` + `linux-vdso` on the stack is
Chromium **deliberately aborting itself** (`IMMEDIATE_CRASH()` /
`base::debug::BreakDebugger()` / a failed `CHECK`), returning from a syscall. It
is not a segfault, not the cgroup OOM killer, and not a GPU/SwiftShader fault
(no `libGLESv2`/`libvk_swiftshader` frames; `--disable-gpu` did not fix it).

Ruled out by measurement under production-equivalent limits (pinned Playwright
image, UID 10001, `cap_drop: ALL`, `no-new-privileges`, the narrowed seccomp
profile, `--memory 1536m`, `--pids-limit`, real netns wrapper + DNS-pinned
proxy):

| hypothesis | result |
|---|---|
| `/dev/shm` size / `noexec` | **Not involved.** Playwright already launches Chromium with `--disable-dev-shm-usage`; `/dev/shm` peak usage was `0` in every run. Enlarging it or toggling `exec` changed nothing. |
| cgroup out-of-memory | **No.** `memory.events` `oom_kill 0` on every crash; RSS peak ~1.1-1.3 GiB against the 1536 MiB cap, identical for crashing and non-crashing runs. |
| GPU process / SwiftShader | **No.** `--disable-gpu` did not remove the crashes; no GL frames in the dumps. |
| URL guard (`context.route`) | **No.** Removing `install_playwright_url_guard` did not remove the crashes. |
| `pids_limit` exhaustion in a clean run | **Not the trigger by itself.** One clean Chromium tree peaks at ~175 tasks; `pids.events` `max 0` at limit 256. It *does* fire under concurrency / a leaked tree (`max` climbed under induced pressure), so it is a latent hazard, not the primary. |
| **`RLIMIT_NOFILE` soft = 1024** | **Confirmed contributor.** Raising it to 65535 (nothing else changed) cut the First Shore crash rate from ~60% to ~20% across repeated real-path runs. |
| **seccomp omits `clone3`** | **Latent contributor.** `defaultAction` is `SCMP_ACT_ERRNO` with no `errnoRet`, so `clone3` returns `EPERM`. glibc >= 2.34 issues `clone3` for `pthread_create` and only falls back to the (allow-listed, arg-filtered) `clone` on `ENOSYS`; on `EPERM` it propagates the failure and Chromium aborts the thread creation. |
| host CPU starvation | **Residual factor.** The VPS is 1 vCPU shared; `top` showed 60%+ steal, and a wedged 46%-CPU thread in the running backend (a `concurrent.futures` worker spinning after a Playwright *driver* assertion crash) made it worse. Under that starvation Chromium's own hang watchdog fires `IMMEDIATE_CRASH` regardless of the limits above. Clearing the spin (a backend restart) and reducing the crash rate breaks the feedback loop. |

### Why `RLIMIT_NOFILE` matters here specifically

`--disable-dev-shm-usage` (Playwright's default, and correct for a locked-down
container) makes Chromium back **every shared-memory region with a file
descriptor** instead of a `/dev/shm` path -- compositor tiles, raster and GPU
transfer buffers, mojo message pipes, the V8 code cache -- on top of the proxied
sockets and the `--remote-debugging-pipe` fds. A heavier page's peak concurrent
fd count crosses 1024, `socket()` / `open()` / `memfd_create()` returns `EMFILE`,
and Chromium's allocator / IPC layer treats it as unrecoverable -> `IMMEDIATE_CRASH`
(`SIGTRAP`) or glibc `abort()` (`SIGABRT`). It is intermittent because it depends
on the peak, which varies run to run, and much worse when a previous crashed
Chromium tree still holds fds.

## The fix (Release 3) -- smallest stable change, defence in depth

1. **`ulimits.nofile` -> `soft: 65535, hard: 65535`** on the backend service
   (`compose.production.yaml`, mirrored in `compose.smoke.yaml`). This is the
   proven primary lever. It is a resource ceiling, not an isolation control; the
   value is what Chromium and Playwright expect. Enlarging `/dev/shm` was
   rejected: Chromium never touches it.
2. **seccomp `clone3` -> `SCMP_ACT_ERRNO` / `errnoRet: 38` (`ENOSYS`)**
   (`docker/backend/seccomp_profile.json`), mirroring the upstream Docker and
   Playwright default profiles. Isolation-neutral: `clone3` stays unavailable for
   every purpose, only its error number changes, forcing glibc onto the existing
   argument-filtered `clone` rules. Profile SHA-256 recorded in `SECCOMP.md`.
3. **`pids_limit` 256 -> 512** on the backend service. Headroom for one Chromium
   tree plus transients (a second tree mid-reap, a slow lease release); still a
   tight fork-bomb bound for one Uvicorn worker + one browser.
4. **Fail-closed preflight**: `validate_browser_runtime_boundary()` now refuses to
   launch when the soft `RLIMIT_NOFILE` is below
   `MINIMUM_BROWSER_OPEN_FILE_LIMIT` (8192), raising the soft limit toward the
   hard ceiling first. A mis-sized container fails at startup with a clear
   message instead of as intermittent, load-dependent page crashes.

No frontend change. No database or schema change. No change to `cap_drop`,
`no-new-privileges`, `read_only`, the seccomp namespace scoping, the non-root
UID, the netns/proxy egress boundary, `mem_limit`, `cpus`, the replica count, or
`APP_MAX_BROWSER_WORKERS`.

## Reproduce / verify

Real production path, inside a container matching production
(`--user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges
--security-opt seccomp=docker/backend/seccomp_profile.json --pids-limit 512
--ulimit nofile=65535:65535 --cpus 1 --memory 1536m
-e APP_BROWSER_EGRESS_MODE=network_namespace_dns_pinned_proxy_v1 -e APP_ENV=production`),
launch through `backend.outbound_security.launch_playwright_chromium`, install
`install_playwright_url_guard`, and `safe_page_goto` the target career page.
Expect a usable DOM and no `page.on("crash")`.

`tests/integration/test_browser_lease_real_chromium.py` runs this
(`test_open_file_limit_preflight_is_enforced`,
`test_content_heavy_page_renders_without_a_page_crash`) and is not skipped when
the protected boundary validates.

## Release 4 -- the driver-crash spin, as it actually happened

Release 3 listed the Playwright *driver* assertion crash as a known follow-up. On
**2026-09-04T21:23:07Z** it happened in production and took company refresh down.

### What was observed

```
21:23:07  coreBundle.js:463  throw new Error(message || "Assertion error");
          Error: Invalid InterceptionId.
            at assert (coreBundle.js:463:11)
            at _CRSession._onMessage (coreBundle.js:34801:11)
            at CRConnection._onMessage (coreBundle.js:34738:20)
            at Immediate.<anonymous> (coreBundle.js:39185:28)
          Node.js v24.18.1                       <- driver process exits
21:23:12  Future exception was never retrieved
          TargetClosedError('Page crashed ... navigating to
          "https://careers.fcsamerica.com/", waiting until "domcontentloaded"')
```

For the next 37 minutes the backend held **88.9% -> 97.5% of its one CPU** with
resident memory byte-identical across samples (238,865,612.8 B) -- no allocation,
no work. `docker inspect` reported `healthy` throughout, because `/api/health`
checks storage only. Every company refresh in that window failed.

`py-spy dump` against the live container named the thread exactly:

```
Thread 1195 (active+gil): "AnyIO worker thread"
    _sync (playwright/_impl/_sync_base.py:113)
    close (playwright/sync_api/_generated.py:16425)      <- Browser.close()
    close (backend/outbound_security.py:1200)
    discover_job_board_with_browser (browser_tools.py:97)
    enrich_company (main.py:177)
    refresh_single_company_information (backend/utility_tasks.py:54)
    run_single_company_refresh (server.py:1270)
    refresh_company_endpoint (server.py:740)             <- inside api_mutation
```

### Diagnosis

**The driver death.** `CRSession._onMessage` ends with
`assert(!object.id, object?.error?.message)`. It fires when a CDP *error response*
arrives carrying an `id` whose callback has already been cleared by session
teardown, and whose error code is not the whitelisted `-32001`. A renderer crash
clears the callbacks; Chromium then answers the already-cancelled
`Fetch.continueRequest` with `Invalid InterceptionId`; the assert throws from
`processImmediate`, i.e. at Node's top level, so the driver process dies.

This is an upstream race, and **no Python-side check can prevent it**: our command
is already in flight when the renderer dies, and the throw happens in the driver's
*receive* path. Playwright **1.62.0 is the newest release**, so there is no
upstream fix to adopt. The window can be narrowed; the failure must be survivable.

**The spin.** Playwright's sync pump is

```python
task.add_done_callback(lambda _: g_self.switch())
while not task.done():
    self._dispatcher_fiber.switch()
```

Pure Python bytecode with no syscall. When the driver dies abruptly mid-dispatch
the dispatcher stops completing the task, `switch()` returns immediately, and the
loop runs forever at 100% of a core. Nothing external reaches it: it never blocks,
so signals, socket timeouts and `SIGKILL`ing the (already dead) driver all miss.
`_CloseGuard` cannot help either -- its escalation assumes `close()` is *blocked*
on a live-but-wedged driver, so it frees a blocked reader, never a spinner.

What was measured in the pinned image, under the full production posture:

| operation on a connection whose driver is dead | result |
|---|---|
| `BrowserContext.new_context`, `Page.goto` | raises in 0.01s |
| `Playwright.stop()` | returns |
| `Browser.close()` right after a `SIGKILL`ed driver | raises in 0.2s, 1 tick |
| **`chromium.launch()` after that close** | **1186 ticks / 12s = 99% of one core, forever** |

So a cleanly killed driver is handled; **reusing the Playwright instance
afterwards is what spins**. Two hypotheses were tested and rejected: file-descriptor
inheritance (no Chromium process holds the driver's stdio pipes, so death does
produce EOF) and `Browser.close()` awaiting a `_closed_future` (1.62.0 has no such
await). An exception escaping a route handler does not spin, but it does leave the
request unsettled and resurfaces on a later unrelated call -- also measured.

The production stack spun in `close()` rather than in a relaunch, so the exact
interleaving of that one incident (driver dying from its own assertion
*mid-dispatch*, rather than by signal) was not reproduced bit-for-bit. That is why
the fix is built to be indifferent to which call wedges: every Playwright call that
can wedge carries the same outer bound.

`tests/integration/test_browser_lease_real_chromium.py::test_relaunch_after_driver_death_cannot_spin_a_core`
pins the reproduced spin. An 8-round alternating crash/relaunch stress run finished
every round at 0% CPU with no Chromium left.

**The blast radius.** The wedged thread was inside `refresh_company_endpoint`,
which runs under `with api_mutation("refresh-company")`. Its `finally:
lease.release()` never ran, so `MutationGate._active` stayed set and every later
mutating request was rejected with `409 Another mutating operation is active`.
That 409 is what the Companies page rendered in its refresh panel.

### The fix

1. **`_settle_route`** (`backend/outbound_security.py`). Every intercepted request
   is completed exactly once, and the handler never raises. A route handler runs
   inside Playwright's dispatcher: an escaping exception leaves the request
   unsettled (the page load then hangs to its timeout) and resurfaces on an
   unrelated later call -- measured. Settlement is idempotent, and every failure
   path is fail-closed: an unverified request is never continued.
2. **`hard_bounded_playwright_call`** + **`protected_playwright_session`**. Every
   Playwright call that can wedge -- `chromium.launch()`, `Browser.close()`,
   `Playwright.stop()` -- carries an outer hard bound. On expiry the owning thread
   is unwound with `BrowserTeardownAbandoned` via `PyThreadState_SetAsyncExc`, the
   only mechanism that reaches a thread executing an uninterruptible pure-Python
   loop. Verified to land exactly at the spin site:

   ```
   File ".../playwright/_impl/_sync_base.py", line 113, in _sync
       self._dispatcher_fiber.switch()
   backend.outbound_security.BrowserTeardownAbandoned
   ```

   The Chromium tree and driver are then reaped (`_reap_orphaned_launch` covers an
   abandoned launch, which has no `_LeasedBrowser` to reap through), the abandoned
   connection's loop is muted so it cannot emit `Task exception was never
   retrieved` for orphaned tasks, and the lease is released only once termination
   is proven -- the existing fail-closed lease behaviour is unchanged.
3. **Gate observability** (`backend/operation_gate.py`). `ActiveOperation` now
   records the owning thread id/name, start time, elapsed seconds and last
   reported progress; `StalledOperationWatcher` logs a `CRITICAL` line once a
   mutation has been held past its threshold. The gate is **still never cleared
   automatically** -- see below.
4. **`GET /api/diagnostics/runtime`** (administrator-only) reports gate state,
   browser lease state, last browser progress/completion, and the last driver
   failure.

### Deliberate limitations

- **The gate is not auto-released, ever.** It serialises writes to the shared
  SQLite database and the export snapshots. Releasing it on a timer, while the
  original operation might still be writing, would admit a second writer and
  corrupt precisely what the gate protects. A thread cannot prove another thread
  has stopped writing. `ownerAlive: false` identifies a genuinely leaked lease and
  a large `secondsSinceProgress` identifies a wedged one, but the only recovery
  that conclusively ends every owner is a backend restart.
- **No CPU-spin check in the application.** A process cannot reliably tell its own
  busy loop from legitimate heavy work, and an ordinary spike must never mark the
  public service unhealthy. Spin detection belongs to host metrics: `docker stats`
  (sustained CPU with flat memory) and per-thread `utime` in
  `/proc/<pid>/task/*/stat`. `py-spy dump --pid <uvicorn pid>` from a
  `--pid=host --cap-add SYS_PTRACE` container names the wedged thread.
- **`/api/health` is unchanged** and stays storage-only: it is public, so it must
  not leak internal identifiers, and it must not flap on load.

## Known follow-ups

- **Host CPU contention.** 1 shared vCPU with heavy steal is marginal for
  headless Chromium; a dedicated core or a second vCPU would remove the residual
  watchdog-timeout crashes that no container limit can fix.
- **Process isolation for browser collection.** Running each company's collection
  in a child process would make driver death a `SIGKILL`-able unit and remove the
  need to unwind a thread at all. That is a design change, not an emergency fix.
