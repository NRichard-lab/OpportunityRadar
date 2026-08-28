# Opportunity Radar production runtime contract

Phase 2 supports a deliberately small first-release runtime. It does not authorize deployment by itself; portal launch/authentication handoff and release operations remain separate work. Container builds, storage, and backup/restore procedures are defined in [PRODUCTION_CONTAINERS.md](PRODUCTION_CONTAINERS.md).

## Process and identity boundary

- Run exactly one Uvicorn worker (`--workers 1`). Do not use `--reload` in production.
- The in-process mutation gate and maintenance lifecycle are intentionally single-process. Do not run multiple web replicas until coordination is redesigned.
- Production is restricted to the exact `APP_TRUSTED_ADMIN_USER_ID`. The database is not multi-user scoped.
- Maintenance threads are tracked, cooperative cancellation is requested during shutdown, and stale active records are reconciled as interrupted during startup.

## Safe feature defaults

The checked-in template keeps all features false. Enable a feature only through
the protected production environment after its operational boundary has been
validated:

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

API models, scheduled actions, collectors, discovery operations, and CLI entry points clamp or reject values outside these configured limits. Browser work remains unavailable while `APP_ENABLE_BROWSER_JOBS=false` regardless of the browser worker setting. Production accepts `APP_ENABLE_BROWSER_JOBS=true` only when `APP_BROWSER_EGRESS_MODE=network_namespace_dns_pinned_proxy_v1` and runtime preflight proves the exact Playwright/Chromium pair, namespace launcher, Chromium wrapper, and no-direct-egress namespace are present. An environment assertion by itself is not sufficient.

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

Application-controlled HTTP and browser destinations use the central outbound validator. Only HTTP(S) on approved ports is accepted; credentials and unsafe/malformed hosts are rejected; every DNS answer must be globally routable; and every redirect is revalidated with a bounded redirect count. Requests connections are pinned to a freshly validated address while preserving the original HTTP Host/TLS identity, and cross-origin redirects cannot carry request/session credentials or original query parameters.

When the production browser boundary is enabled, Chromium runs as UID/GID 10001 in a child user/network namespace with only loopback and no default route. Chromium is forced through a loopback relay that crosses the namespace only through a mode-0600 pathname Unix socket. The parent-side proxy validates every HTTP absolute-form or CONNECT destination and connects to a validated numeric address, closing the DNS-rebinding gap. The empty kernel network namespace independently prevents a browser transport from directly reaching Uvicorn loopback, Docker networks, private networks, link-local/metadata endpoints, or the public Internet. Service workers and WebSockets remain blocked, and QUIC and non-proxied WebRTC are disabled as defense in depth. If the proxy or relay is unavailable, browser traffic has no direct fallback.
