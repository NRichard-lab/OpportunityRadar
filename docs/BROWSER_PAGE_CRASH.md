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

## Known follow-ups (not Release 3)

- **Playwright *driver* assertion crash** (`coreBundle.js` `_CRSession._onMessage`
  assertion, Node driver exits) on a target-crash race can leave a
  `concurrent.futures` collection worker spinning a CPU. `cab3e46`'s
  `_CloseGuard` bounds the *lease*, not this. Recommended: a hard wall-clock
  timeout around each company's collection in `run_collection_group` so a dead
  driver cannot pin a core.
- **Host CPU contention.** 1 shared vCPU with heavy steal is marginal for
  headless Chromium; a dedicated core or a second vCPU would remove the residual
  watchdog-timeout crashes that no container limit can fix.
