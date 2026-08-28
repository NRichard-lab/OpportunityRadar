# Blue Ash Portal Handoff

Opportunity Radar uses a one-time authorization-code handoff from the Blue Ash Portal. Radar does
not receive the Portal browser cookie, store passwords, implement MFA, or create its own independent
session record. The only Radar browser credential is the raw, app-scoped token returned by Portal;
Portal persists only that token's hash.

## Production endpoints and origins

- Radar: `https://radar.blueashdigital.tech`
- Portal frontend/exit destination: `https://blueashdigital.tech`
- Portal API and authorization host: `https://api.blueashdigital.tech`
- Radar callback: `https://radar.blueashdigital.tech/api/auth/callback`
- Client/application slug: `opportunity-radar`

Production startup pins these exact origins, an empty `APP_BASE_PATH`, and the exact callback derived
from `APP_PUBLIC_URL`. It rejects HTTP, alternate hosts, URL credentials, query strings, fragments,
placeholder secrets, and an incorrectly named Radar session cookie.

## Browser flow

1. The frontend calls `GET /api/auth/session`. A missing or expired Radar token returns a same-origin
   `loginUrl` for `GET /api/auth/start`; the frontend never trusts or follows an arbitrary login URL.
2. `/api/auth/start` validates a local UI return path and performs a bounded server-to-server probe of
   the fixed Portal API health endpoint. An outage returns a sanitized local `503` and never sends the
   browser to a dead or attacker-controlled host.
3. Radar creates a random `state` and PKCE verifier/challenge. The verifier, state, issue/expiry times,
   and return path are HMAC-signed into a five-minute HttpOnly host-only cookie. The browser is sent to
   `GET https://api.blueashdigital.tech/api/app-auth/authorize` with `client_id`, `redirect_uri`,
   `response_type=code`, `state`, `code_challenge`, `code_challenge_method=S256`, and `return_path`.
4. Portal authenticates its own host-only session, confirms the enabled Radar assignment, consumes a
   one-time code, and redirects to Radar's GET callback. Portal can return only sanitized error codes
   such as `access_denied` or `temporarily_unavailable`.
5. Radar validates the signed state cookie and exchanges the code server-to-server at
   `POST /api/app-auth/exchange`. It immediately introspects the returned token at
   `POST /api/app-auth/introspect` before issuing the Radar cookie.
6. The callback sets `__Host-opportunity_radar_session` to the raw Portal app token and responds `303`
   to the validated local path. The state cookie is always deleted. Callback failures are converted to
   `?auth=denied`, `?auth=unavailable`, or `?auth=failed`; raw codes and Portal text are never rendered.

The production backend disables Uvicorn access logging and sends `Referrer-Policy: no-referrer`, so
the short-lived callback query cannot enter normal access logs or a follow-up Referer header.

## Portal server contract

Exchange, introspection, and revoke authenticate Radar with HTTP Basic using
`BLUEASH_AUTH_CLIENT_ID` and `BLUEASH_AUTH_CLIENT_SECRET`. Credentials are never included in JSON.

- Exchange JSON: `{code, code_verifier, redirect_uri}`
- Exchange result: `{access_token, token_type: "Bearer", expires_in, idle_expires_at, absolute_expires_at}`
- Introspection JSON: `{token}`
- Active result: `{active, user_id, username, email, display_name, role, permissions, application_slug, idle_expires_at, absolute_expires_at}`
- Inactive result: `{active: false}`
- Revoke JSON: `{token}`, successful status `204`

Radar introspects on every protected request by default (`RADAR_INTROSPECTION_CACHE_SECONDS=0`), so
Portal logout, disablement, assignment removal, and token revocation take effect on the next request.
Deployments may opt into a success-only cache of at most 30 seconds. Failures and inactive tokens are
never cached as successes, revoke invalidates a cached success before making its network request, and
cache keys are SHA-256 token digests rather than raw credentials.

## Cookies, expiry, and authorization

Both production cookies are host-only, `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`; neither has
a `Domain` attribute. The handoff cookie is short-lived. The session cookie contains only the raw
Portal-issued app token. Portal enforces the 30-minute idle timeout and the parent-bounded absolute
expiry; Radar rejects expired, overlong, malformed, wrong-application, or unknown-role introspection
responses.

An active token bound to `application_slug=opportunity-radar` proves Portal assignment and permits
authenticated read access for `USER` and `ADMINISTRATOR` identities. Global writes and private
administrator data retain `require_administrator`: production requires both role `ADMINISTRATOR` and
an exact canonical UUID match with `APP_TRUSTED_ADMIN_USER_ID`. The frontend exposes read-only views
to an assigned ordinary user and does not call administrator-only bootstrap endpoints.

Only these routes are public: `/api/health`, `/api/auth/start`, `/api/auth/callback`,
`/api/auth/session`, and `/api/auth/logout`. Similar `/api/auth/*` paths are not broadly bypassed.
Unsafe Portal-handoff requests require an exact `Origin` match. `/api/health` performs storage checks
only and never calls Portal or any external service.

Logout posts only the Radar app token to `/api/app-auth/revoke`, then clears both Radar cookies even if
Portal is unavailable. It returns the browser to `https://blueashdigital.tech/` and deliberately leaves
the parent Portal session active.

## Configuration and rollout

Use `deploy/opportunity-radar.env.example` as the authoritative production template. Keep both secrets
outside Git and set file permissions so only the deployment account can read them. Production Compose
forces `AUTH_MODE=portal_handoff`, the dedicated origins, the host-only cookie name, session bounds,
zero cache, and all Phase 1 feature flags off.

`AUTH_MODE=local` remains available only with `APP_ENV=development` and a loopback
`APP_PUBLIC_URL`. Production never falls back to local authentication when Portal is unavailable.

The required rollout order is:

1. Deploy the Radar infrastructure first.
2. Verify Radar directly, including `/api/health`, without changing the Portal application registry.
3. Prepare the Portal release and back up its PostgreSQL database.
4. Apply Portal migration `0006` only after Radar is available.
5. Verify the Portal Launch flow end to end.

Migration `0006` must not be applied before Radar is available. Radar requires no database migration
for this handoff. After rollout, validate login/MFA return, deep-link return, assigned ordinary-user
read access, trusted-administrator writes, assignment removal, idle expiry, Radar-only logout, Portal
outage behavior, and health independence. A Radar production restart is required to load new
environment values.
