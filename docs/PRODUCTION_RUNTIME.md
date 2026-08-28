# Opportunity Radar production runtime contract

Phase 2 supports a deliberately small first-release runtime. It does not authorize deployment by itself; portal launch/authentication handoff and release operations remain separate work. Container builds, storage, and backup/restore procedures are defined in [PRODUCTION_CONTAINERS.md](PRODUCTION_CONTAINERS.md).

## Process and identity boundary

- Run exactly one Uvicorn worker (`--workers 1`). Do not use `--reload` in production.
- The in-process mutation gate and maintenance lifecycle are intentionally single-process. Do not run multiple web replicas until coordination is redesigned.
- Production is restricted to the exact `APP_TRUSTED_ADMIN_USER_ID`. The database is not multi-user scoped.
- Maintenance threads are tracked, cooperative cancellation is requested during shutdown, and stale active records are reconciled as interrupted during startup.

## Safe initial feature state

Keep all of these values false for the initial release:

```dotenv
APP_ENABLE_BROWSER_JOBS=false
APP_ENABLE_COMPANY_REFRESH=false
APP_ENABLE_UTILITIES=false
APP_ENABLE_SCHEDULES=false
APP_ENABLE_DISCOVERY=false
APP_WRITE_FRONTEND_MIRRORS=false
```

`frontend/public` must never receive production snapshots or runtime data.

Build the production frontend with `VITE_BASE_PATH=/` for the dedicated
`https://radar.blueashdigital.tech/` origin. Keep `APP_BASE_PATH` empty. The Vite setting is consumed
at build time and determines asset, client-route, and API URLs.

## Worker limits

```dotenv
APP_MAX_HTTP_WORKERS=4
APP_MAX_BROWSER_WORKERS=1
APP_MAX_ACTIVE_MAINTENANCE=1
```

API models, scheduled actions, collectors, discovery operations, and CLI entry points clamp or reject values outside these configured limits. Browser work remains unavailable while `APP_ENABLE_BROWSER_JOBS=false` regardless of the browser worker setting. This release also rejects `APP_ENABLE_BROWSER_JOBS=true` when `APP_ENV=production`: Playwright validates every URL and redirect, but it cannot pin the browser's socket to the validated DNS answer. Keep browser jobs off until production browser traffic is forced through an independently enforced, allowlisted egress boundary.

## Persistent storage

- Use the exact `/srv/opportunity-radar` host and `/var/lib/opportunity-radar` container mapping in the container contract. Put `APP_DATA_DIR`, the SQLite database, imports, exports, backups, and logs on configured private writable mounts outside the immutable application image.
- SQLite must reside on one host's local SSD-backed persistent volume.
- NFS, SMB, distributed/shared filesystems, and simultaneous mounts from multiple replicas are unsupported.
- Do not expose data, import, export, backup, or log directories through the web server.
- Keep `APP_WRITE_FRONTEND_MIRRORS=false` in production.
- Set `DEPLOYMENT_VERSION` to the immutable release commit or image version.
- Set `REQUIRE_EXISTING_DATABASE=true`. Production startup must fail if the database or required mount is missing, corrupt, or not writable; it must never create an empty replacement.

## Health contract

`GET /api/health` is public for the platform health probe and returns `Cache-Control: no-store`.

- `200 {"status":"healthy"}`: process, expected SQLite schema/read/write rollback probe, and required data storage are ready.
- `200 {"status":"degraded"}`: core service is ready but optional export storage is impaired.
- `503 {"status":"unhealthy"}`: the database is missing/invalid/unreadable/unwritable or required persistent storage is missing/unwritable.

The check never initializes or migrates SQLite, persists its database probe, contacts Blue Ash or job boards, launches a browser, or sends email. Responses contain bounded component states and never contain paths, SQL, credentials, or raw exceptions.

## File and upload limits

- Resume uploads: PDF/DOCX only, at most 10 MiB, with signature, page, archive expansion, and extracted-text limits.
- Imports: JSON/XLSX only, at most 25 MiB, with record, worksheet, archive expansion, and compression-ratio limits. CSV is not supported.
- Temporary uploads use randomized private names and are removed on success, failure, or cancellation.
- Snapshot and spreadsheet writes use serialized unique temporary files and atomic replacement. Spreadsheet formula prefixes and unsafe hyperlink schemes are neutralized.

## Outbound requests

Application-controlled HTTP and browser destinations use the central outbound validator. Only HTTP(S) on approved ports is accepted; credentials and unsafe/malformed hosts are rejected; every DNS answer must be globally routable; and every redirect is revalidated with a bounded redirect count. Requests connections are pinned to a freshly validated address while preserving the original HTTP Host/TLS identity, and cross-origin redirects cannot carry request/session credentials or original query parameters. Browser service workers, WebSockets, and automatic proxy use are blocked by the current policy; browser jobs still remain production-disabled because Playwright cannot provide the same socket-level DNS pinning without an external egress boundary.
