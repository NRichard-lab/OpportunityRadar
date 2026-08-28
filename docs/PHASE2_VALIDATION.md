# Phase 2 local validation record

Validated on 2026-08-27 America/Denver (2026-08-28 UTC). This record covers only local,
synthetic validation. No image was pushed, no production service was changed, and no real runtime
data or secret was used.

## Artifact validation

- Python: 202 tests passed; three platform-specific POSIX/symlink tests skipped on Windows.
- Backup/restore on Linux as UID 10001: 20 of 20 focused tests passed, including POSIX modes,
  symlink rejection, directory durability, committed-WAL capture, stale-sidecar handling, atomic
  activation, exact rollback, cancellation, and failed-backup cleanup.
- Frontend: `tsc -b` and the Vite production build passed; 1,596 modules transformed.
- Backend image: approximately 66.4 MB, UID/GID 10001, exact one-worker Uvicorn command, read-only
  root, no Playwright module, and no `.env`, SQLite, private data, or bytecode-cache files under
  `/app`.
- Frontend image: approximately 23.1 MB, UID/GID 10001, read-only root, root-path asset URLs, and no
  `/data` mirror or persistent application volume.
- Python dependencies: `pip check` reported no broken requirements.
- Both production and smoke Compose configurations passed `docker compose config --quiet`.
- The reviewed Caddy example passed Caddy 2.10.2 validation.

Both smoke services reached Docker `healthy`. The root and a deep UI path returned the same SPA
shell, the hashed JavaScript asset returned `Cache-Control: public, max-age=31536000, immutable`,
and `index.html` returned `Cache-Control: no-cache`. Frontend `/api/*`, `/data/*`, and missing asset
paths returned 404.

A disposable Caddy container also exercised the complete future routing shape: `/` and a deep SPA
route returned 200, `/api/health` returned backend JSON, `/api/unknown` returned backend JSON 404
rather than SPA HTML, and `/data/private.json` returned 404.

The local smoke bridge intentionally permits only explicitly published loopback ports 18000 and
18080. Marking its only network `internal` prevented Docker Desktop 29.7.2 from installing those
loopback bindings, so that setting is not used. This does not affect production: production has no
host port publications and attaches only to the external `blueash-edge` network.

## Synthetic persistence and restore rehearsal

The deterministic fixture contained two synthetic companies, two jobs, one application, one
resume, and two settings. Every synthetic URL used the reserved `.example.invalid` namespace.

1. The running backend created an online SQLite backup using the SQLite backup API.
2. Independent validation passed integrity, foreign-key, schema, bounded-count, manifest, and
   SHA-256 checks. The published artifact directory was mode 0700 and its database and manifest
   were mode 0600 inside Linux.
3. A third synthetic company was created through the API.
4. Counts remained 3/2/1 after backend restart, backend force-recreation, and frontend
   force-recreation.
5. The backend was stopped before restore. The restore preserved the three-company current state as
   both validated and raw diagnostic artifacts, staged and validated the selected backup, switched
   atomically, and performed post-switch validation.
6. After restart, health was `healthy` and counts returned to 2 companies, 2 jobs, and 1
   application.

Retention runs only after a new artifact validates, retains seven regular backups by default,
never deletes the only known-good backup, and excludes pre-restore preservation artifacts from
regular pruning. Offsite upload and scheduling are deliberately outside Phase 2.

## Persistence failure matrix

All container-level failures used separate disposable synthetic directories.

| Scenario | Result |
| --- | --- |
| Empty database mount | Startup exited nonzero; no replacement database was created. |
| Read-only database bind mount | Startup exited nonzero; the source database remained present. |
| Missing required data mount | Startup exited nonzero; the image did not create fallback storage. |
| Read-only optional export mount | Service started with HTTP 200 `degraded`; database and required data remained healthy. |
| Corrupt SQLite database | Startup exited nonzero; corrupt input was retained for diagnosis. |
| Database removed after startup | Health changed to HTTP 503 `unhealthy`; no replacement appeared; health recovered when the same synthetic DB returned. |
| Backup with added WAL/SHM sidecar | Validation rejected it, including the same-record-count stale-WAL regression. |
| Injected backup/fsync failure | No artifact was published and previous known-good backups remained. |
| Restore activation/post-switch failure | Exact prior DB/WAL/SHM state was rolled back and retained. |

## SQLite and storage result

Production connections use SQLite URI `mode=rw` when the existing-database guard is active, so a
missing file cannot be created during a connection race. Every writable application connection
enables foreign keys, WAL, a 5,000 ms busy timeout, and `synchronous=FULL`. The supported topology
is one Uvicorn process and one backend replica on one host's local SSD; NFS, SMB, and shared or
distributed filesystems are unsupported.

The host contract is `/srv/opportunity-radar/{database,data,exports,backups,logs}` with imports at
`data/imports`. `/tmp`, `/var/lib/opportunity-radar/tmp`, browser profiles/caches, and extraction
workspace are ephemeral. Frontend data mirrors, developer runtime paths, source-tree databases,
and local `.env` files are absent from production images.

## Resource observation

Local container observations after startup and restore:

- Backend memory: approximately 63.5-64.0 MiB.
- Frontend memory: approximately 20.4-20.7 MiB.
- Backend steady idle CPU: approximately 0.18-0.23%, with short roughly 20% samples when the
  intentionally frequent five-second smoke health probe performed its SQLite write-and-fsync
  check.
- Frontend idle CPU: approximately 0.00-0.01%.
- Chromium contribution: 0; Chromium and Playwright are not installed.

Read-only Hostinger inventory showed a KVM 1 VPS with 1 vCPU, 4,096 MB RAM, and 51,200 MB disk.
Across 96 recent samples, the existing host averaged about 2.20% CPU and 739 MB RAM, with observed
maxima of 9.28% CPU and 806 MB RAM; observed disk usage reached about 6.84 GB. The browser-disabled
Opportunity Radar pair is therefore adequately sized for the initial one-administrator release,
with substantial RAM and disk margin. The single vCPU leaves limited concurrency headroom, so the
one-worker and disabled-maintenance constraints remain important. This is not approval or capacity
evidence for a later Chromium/browser-enabled image.

## Deferred production work

The production environment template deliberately sets unsupported `AUTH_MODE=portal_handoff`, so
an accidental pre-Phase-3 start fails closed. Production still requires the Phase 3 portal handoff,
real secrets in an external mode-0600 environment file, a reviewed migrated database placed with
UID/GID 10001 ownership, host directory preparation, release image publication, edge/Caddy/DNS
change approval, and an operator decision for scheduled/offsite backups and monitoring.
