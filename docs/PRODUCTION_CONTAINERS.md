# Opportunity Radar production containers and persistence

This document defines Opportunity Radar build artifacts, persistent storage, backup/restore, and
local validation. It is not authorization to deploy. The production environment uses the reviewed
`AUTH_MODE=portal_handoff` contract documented in `BLUEASH_AUTH_INTEGRATION.md`.

## Architecture

The first release has two containers on the external `blueash-edge` network:

```text
future Caddy :443
  /api/*  -> opportunity-radar-backend:8000 (one Uvicorn process)
  /*       -> opportunity-radar-frontend:8080 (unprivileged Nginx)
```

The frontend never proxies API traffic. Its Nginx configuration returns 404 for `/api`, `/api/*`,
`/data`, and `/data/*`, while arbitrary UI paths fall back to `index.html`. The production frontend
is built for the root path at `https://radar.blueashdigital.tech/`.

The backend image uses Python 3.12 on Debian Bookworm, runs as UID/GID 10001, and starts exactly:

```text
uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
```

The frontend build stage uses Node 22 and `npm ci`; its runtime uses unprivileged Nginx as UID/GID
10001. Both base images are digest-pinned. Python packages are version-locked in
`requirements-production.txt`, and frontend packages are locked by `frontend/package-lock.json`.

Playwright and Chromium are intentionally absent. Browser imports are lazy, no startup path needs
them, and all browser/discovery features are disabled. A later browser-capable image must not be
created until independent outbound egress enforcement exists.

## Storage contract

Create these directories on the single deployment host's local SSD:

```text
/srv/opportunity-radar/
  database/
  data/
    imports/
  exports/
  backups/
  logs/
  tmp/
```

The service mounts and configuration are:

| Host path | Container path/configuration | Lifecycle |
| --- | --- | --- |
| `/srv/opportunity-radar/database` | `/var/lib/opportunity-radar/database`; `DATABASE_URL=sqlite:////var/lib/opportunity-radar/database/opportunity_radar.db` | Persistent, required |
| `/srv/opportunity-radar/data` | `/var/lib/opportunity-radar/data`; `APP_DATA_DIR` | Persistent, required |
| `/srv/opportunity-radar/data/imports` | `/var/lib/opportunity-radar/data/imports`; `APP_IMPORT_DIR` | Persistent while processing |
| `/srv/opportunity-radar/exports` | `/var/lib/opportunity-radar/exports`; `APP_OUTPUT_DIR` and `APP_EXPORT_DIR` | Persistent |
| `/srv/opportunity-radar/backups` | `/var/lib/opportunity-radar/backups`; `APP_BACKUP_DIR` | Persistent |
| `/srv/opportunity-radar/logs` | `/var/log/opportunity-radar`; `APP_LOG_DIR` | Persistent |
| Container tmpfs | `/tmp` and `/var/lib/opportunity-radar/tmp` | Ephemeral |

`/srv/opportunity-radar/tmp` is an optional operator scratch directory. It is disposable and is not
mounted into the production containers. Browser caches/profiles and extraction work files are also
ephemeral and must not be put on a persistent mount.

SQLite is supported only on one host's local SSD-backed filesystem. NFS, SMB, shared/distributed
filesystems, multiple Uvicorn processes, and multiple backend replicas are unsupported. The initial
permissions are owned by the image identity:

```sh
sudo install -d -o 10001 -g 10001 -m 0750 \
  /srv/opportunity-radar/database \
  /srv/opportunity-radar/data \
  /srv/opportunity-radar/data/imports \
  /srv/opportunity-radar/exports \
  /srv/opportunity-radar/backups \
  /srv/opportunity-radar/logs \
  /srv/opportunity-radar/tmp
```

Do not put `frontend/public/data`, developer runtime paths, source-tree databases, `.env` files,
resumes, exports, logs, credentials, or private snapshots into either image. `.dockerignore` blocks
these categories, and each Dockerfile copies only the source it needs.

## Build immutable images

Set the full release commit and build both images from the repository root:

```sh
OPPORTUNITY_RADAR_RELEASE_SHA="$(git rev-parse HEAD)"
docker build --pull --file docker/backend/Dockerfile \
  --build-arg "DEPLOYMENT_VERSION=${OPPORTUNITY_RADAR_RELEASE_SHA}" \
  --tag "blueash/opportunity-radar-backend:${OPPORTUNITY_RADAR_RELEASE_SHA}" .
docker build --pull --file docker/frontend/Dockerfile \
  --build-arg VITE_BASE_PATH=/ \
  --tag "blueash/opportunity-radar-frontend:${OPPORTUNITY_RADAR_RELEASE_SHA}" .
```

Do not use mutable production tags such as `latest`. The backend image label and default
`DEPLOYMENT_VERSION` contain the same full commit SHA. Runtime Compose also overrides
`DEPLOYMENT_VERSION` with that release SHA.

## Production environment and startup

Copy `deploy/opportunity-radar.env.example` to a root-readable protected file outside the checkout,
replace every placeholder, and set mode 0600. Do not commit or bake that file into an image.
Register the Radar client and apply the matching Portal migration before starting Radar. Keep both
handoff secrets outside Git and validate the exact production origins and callback before rollout.

Before first startup, place an already migrated and validated database at:

```text
/srv/opportunity-radar/database/opportunity_radar.db
```

After placing the database, assign it to the container identity and keep it private:

```sh
sudo chown 10001:10001 /srv/opportunity-radar/database/opportunity_radar.db
sudo chmod 0600 /srv/opportunity-radar/database/opportunity_radar.db
```

The application never creates a production replacement. `REQUIRE_EXISTING_DATABASE=true` is both a
production default and a Compose-enforced value. Missing/corrupt databases, missing required data
storage, or failed database write probes terminate startup. If the database later disappears,
health becomes unhealthy and runtime connections use SQLite `mode=rw`, which cannot create a new
file.

After a reviewed release authorization, the intended operator sequence is:

```sh
export OPPORTUNITY_RADAR_RELEASE_SHA=<full-release-commit-sha>
export OPPORTUNITY_RADAR_ENV_FILE=/etc/opportunity-radar/opportunity-radar.env
export OPPORTUNITY_RADAR_HOST_ROOT=/srv/opportunity-radar
docker network inspect blueash-edge >/dev/null
docker compose -f compose.production.yaml config --quiet
docker compose -f compose.production.yaml up -d
docker compose -f compose.production.yaml ps
```

The production Compose file publishes no host ports. Future Caddy must join `blueash-edge` and use
the reviewed `deploy/Caddyfile.example` only after a separate production change approval.

## Health contract

The backend healthcheck calls `GET /api/health`. Healthy means the expected schema is present, the
database can be read and exercised by a rolled-back write, required data storage is writable, and
the check cleaned up its probe. Missing/corrupt/read-only SQLite or missing/unwritable required data
is `503 unhealthy`; an unavailable optional export directory is `200 degraded`. The response
includes the bounded deployment version and never reveals filesystem paths or raw exceptions.

The frontend healthcheck requests `/`. A deep UI route returns the SPA shell, `/api/*` and `/data/*`
return 404, missing hashed assets return 404, hashed assets are immutable-cached, and `index.html`
uses revalidation/no-cache semantics.

## SQLite settings

Every application connection enables foreign keys and a 5000 ms busy timeout. Writable connections
enable WAL and `synchronous=FULL`. Connections are short-lived per operation, remain on the thread
that created them, and use the single-process mutation gate for shared writes. These settings do not
make shared/network storage or multiple application processes supported.

## Online backup

The backup command uses SQLite's online backup API, so committed WAL content is captured safely. It
writes a unique hidden pending directory, validates integrity, foreign keys, schema, bounded table
counts, and schema version, records the deployment version and SHA-256, fsyncs the completed files,
and only then atomically publishes the backup directory.

Run it while the application is healthy:

```sh
docker compose -f compose.production.yaml exec -T opportunity-radar-backend \
  python -m backend.cli backup-sqlite --retain 7
```

Validate an artifact independently:

```sh
docker compose -f compose.production.yaml exec -T opportunity-radar-backend \
  python -m backend.cli validate-sqlite-backup \
  --backup /var/lib/opportunity-radar/backups/backup_<timestamp>_<id>
```

Retention applies only after a new backup passes validation. It retains seven validated regular
backups by default, never prunes the only known-good backup, ignores incomplete/invalid artifacts,
and does not prune pre-restore preservation artifacts. Published and preservation directories use
mode 0700 and their database, metadata, and auxiliary files use mode 0600. Offsite upload is
intentionally not included.

## Offline restore

Never restore over a live database. Stop the backend and wait for its 60-second graceful shutdown;
verify no other process or container can write the database. Then run a one-off container and pass
the explicit writes-stopped assertion:

```sh
docker compose -f compose.production.yaml stop -t 60 opportunity-radar-backend
docker compose -f compose.production.yaml run --rm --no-deps opportunity-radar-backend \
  python -m backend.cli restore-sqlite \
  --backup /var/lib/opportunity-radar/backups/backup_<timestamp>_<id> \
  --confirm-writes-stopped
docker compose -f compose.production.yaml up -d opportunity-radar-backend
docker compose -f compose.production.yaml ps
```

Restore validates the managed source, copies it into a new staging database through SQLite, checks
integrity, foreign keys, core schema, schema fingerprint/version, and bounded counts, and preserves
the current database before replacement. Only a validated stage is atomically switched into place.
The pre-restore preservation remains in the backup directory. An invalid current database is kept as
a diagnostic artifact instead of discarded. The restore tool sets mode 0600; ensure the restored DB
is owned by 10001:10001 before restarting.

## Local synthetic Compose rehearsal

Use only a new disposable directory. The generator refuses to overwrite an existing database and
creates synthetic companies, jobs, application state, resume-derived content, and settings:

```powershell
$phase2SmokeRoot = Join-Path ([IO.Path]::GetTempPath()) ("opportunity-radar-phase2-" + [guid]::NewGuid().ToString("N"))
foreach ($name in @("database", "data/imports", "exports", "backups", "logs")) {
    New-Item -ItemType Directory -Path (Join-Path $phase2SmokeRoot $name) -Force | Out-Null
}
python scripts/create_synthetic_database.py --database (Join-Path $phase2SmokeRoot "database/opportunity_radar.db")
$env:OPPORTUNITY_RADAR_SMOKE_ROOT = $phase2SmokeRoot
$env:OPPORTUNITY_RADAR_RELEASE_SHA = (git rev-parse HEAD)
docker compose -f compose.smoke.yaml up -d --build
docker compose -f compose.smoke.yaml ps
```

The smoke stack binds only loopback ports 18000 and 18080 by default. The backend uses explicit
development/local authentication solely for synthetic local API checks; storage guards, one-worker
execution, disabled feature flags, read-only roots, mounts, and image contents match the production
model. The frontend intentionally does not proxy the backend.

Useful non-destructive checks are:

```powershell
Invoke-RestMethod http://127.0.0.1:18000/api/health
Invoke-RestMethod http://127.0.0.1:18000/api/companies
Invoke-WebRequest http://127.0.0.1:18080/companies -UseBasicParsing
try { Invoke-WebRequest http://127.0.0.1:18080/api/unknown -UseBasicParsing } catch { $_.Exception.Response.StatusCode.value__ }
docker compose -f compose.smoke.yaml restart opportunity-radar-backend
docker compose -f compose.smoke.yaml up -d --force-recreate opportunity-radar-backend
docker compose -f compose.smoke.yaml up -d --force-recreate opportunity-radar-frontend
docker compose -f compose.smoke.yaml stats --no-stream
```

After checks, stop the synthetic stack with `docker compose -f compose.smoke.yaml down`. Keep or
discard only the generated disposable directory according to the test record; never point the smoke
file at the user's real runtime data.

## Initial-release restrictions

Keep all six feature switches false: browser jobs, company refresh, Utilities, schedules, discovery,
and frontend mirrors. Keep one Uvicorn worker and one backend replica. Do not enable Playwright,
change the storage to PostgreSQL, use network filesystems, expose private directories, or deploy the
Caddy example during Phase 2.
