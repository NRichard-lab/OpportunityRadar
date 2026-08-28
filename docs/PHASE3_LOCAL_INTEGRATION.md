# Phase 3 local authentication integration

This harness validates the Blue Ash Portal to Opportunity Radar one-time authentication handoff
using only disposable local data. It does not read either repository's `.env`, publish images,
push commits, deploy services, change DNS, or contact a production database.

## Safety model

- PostgreSQL uses a `tmpfs` database named `portal_phase3` with synthetic credentials.
- Radar uses a newly generated synthetic SQLite database under the operating-system temp directory.
- Portal, Radar, and Caddy attach only to an internal Docker network. Chromium uses that same
  internal application network plus a separate control bridge that no application service joins.
- Host TCP 443 is never published. Only Chromium's control port is published on an ephemeral
  `127.0.0.1` port; no application HTTP service is reachable from the host.
- The integration Caddy service owns the exact production-shaped hostnames only inside Docker.
- Chromium resolves those three exact hostnames to Caddy through isolated Docker DNS. No hosts-file
  or external DNS change is made.
- Caddy uses an ephemeral internal CA. Chromium ignores only that local certificate error. A
  one-shot container copies only the public root certificate (never the CA private key) into a
  separate read-only Radar volume, and Radar's server-side Portal client validates that certificate.
- The normal Radar SSRF transport remains unchanged. A strictly gated, bind-mounted integration
  wrapper swaps it for a normal Requests session only after confirming the exact synthetic paths,
  hosts, auth mode, disabled feature flags, and local Caddy CA path.
- Portal's deterministic MFA sink is likewise a gated integration-only ASGI wrapper. It stores the
  normal hash-only MFA challenge and never adds a production bypass endpoint.
- Every application/client and signing secret is generated in memory for one run. Fixed values are
  limited to conspicuously synthetic user credentials, the disposable PostgreSQL credential, and a
  synthetic MFA code.

The exact production URLs are intentional: they let the browser validate `Secure`, host-only, and
`__Host-` cookie behavior without weakening application settings or colliding with a host service.

## Prerequisites

- Docker Desktop with Docker Compose v2
- Python 3.12 with this repository's development requirements
- Docker access to pull the pinned Playwright Chromium image
- Clean Portal and Radar implementation worktrees

The default Portal worktree is:

```text
C:\Users\dog10\OneDrive\Documents\ChatGPT\Blue Ash Release
```

Use another clean Portal worktree with `-PortalRepo` when needed.

## Static and Compose validation

This performs Python compilation and `docker compose config --quiet`; it does not start containers:

```powershell
Set-Location 'C:\Users\dog10\Documents\Codex\FinancialJobsRadarOrig'
.\scripts\run_phase3_integration.ps1 -ValidateOnly
```

## Full disposable integration run

```powershell
Set-Location 'C:\Users\dog10\Documents\Codex\FinancialJobsRadarOrig'
.\scripts\run_phase3_integration.ps1
```

The runner performs this migration cycle against only disposable PostgreSQL:

```text
blank -> 20260825_0005 -> 20260827_0006 -> 20260825_0005 -> 20260827_0006
```

At every revision it invokes the Portal schema validator. At `0006`, validation covers the exact
registry URLs, auth tables, columns, hash uniqueness, foreign keys, and cleanup indexes. At `0005`,
it confirms the forward-auth schema is gone and the old registry state is restored. After the
second upgrade it seeds only reserved synthetic identities and assignments.

The browser suite then checks:

- unauthenticated Radar health without a Portal request;
- direct Radar access, Portal login, administrator MFA, and the completed handoff;
- a pre-authenticated Portal dashboard Opportunity Radar card Launch into an authenticated Radar
  popup, covering the production defect's real frontend launch path;
- removal of authorization code and state from the final URL;
- host-only Portal pre-auth, Portal session, and Radar session cookies, including `Secure`,
  `HttpOnly`, `SameSite=Lax`, and `Path=/` attributes;
- absence of the raw Portal cookie on Radar requests;
- refresh with the Radar app session;
- absence of captured codes, tokens, the client secret, the synthetic password, and the MFA value
  from container logs;
- Radar-only logout while the Portal session remains active; and
- a bounded unassigned-user denial without a redirect loop;
- assigned ordinary-user denial for a global Radar mutation; and
- immediate child-session invalidation after Portal logout (the integration cache is disabled).

The runner always calls `docker compose down -v --remove-orphans` and removes its guarded temp
directory in `finally`, including after a partially failed first start. It intentionally has no
keep-stack mode, so ephemeral interpolation values cannot be forgotten while resources remain.

## Manual migration commands

The runner is the preferred entry point because it generates safe environment values. Within that
prepared environment, its exact migration commands are:

```powershell
docker compose -f compose.phase3.yaml up -d --wait portal-postgres
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool alembic upgrade 20260825_0005
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool python tests/integration/validate_phase3_schema.py 0005
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool alembic upgrade 20260827_0006
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool python tests/integration/validate_phase3_schema.py 0006
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool alembic downgrade 20260825_0005
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool python tests/integration/validate_phase3_schema.py 0005
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool alembic upgrade 20260827_0006
docker compose -f compose.phase3.yaml --profile tools run --rm portal-tool python tests/integration/validate_phase3_schema.py 0006
```

Never substitute a production database URL, existing Radar data directory, production secret, or
production Compose/Caddy file. The Portal helpers refuse any database other than the isolated
`portal_phase3` service, and the Radar wrapper refuses any runtime paths or hosts outside this
harness.
