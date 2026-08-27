# Blue Ash Authentication Integration

> Phase 1A status: this documents the retained transitional shared-cookie integration and its
> fail-closed guardrails. No Blue Ash production or portal changes are part of this checkpoint. The
> one-time authentication handoff and final deployment architecture remain deferred.

Source reviewed: `NRichard-lab/blueashdigital.tech` at commit
`953ebd626d6f071788811841d4fa539e7c9a47b5` on 2026-08-25.

## Existing Blue Ash Authentication

- Authentication occurs in the Blue Ash FastAPI portal at `POST /api/auth/login`.
- Passwords belong to the portal and use Argon2id hashes. Opportunity Radar stores no passwords.
- Administrators always require email MFA. Other portal users use email MFA when their account requires it. The MFA endpoints are `/api/auth/mfa/verify`, `/resend`, and `/cancel`.
- Successful authentication creates an opaque random token. Only its HMAC-SHA256 hash is stored in the portal PostgreSQL `sessions` table.
- The browser token is the HttpOnly `blueash_session` cookie. Production configuration scopes it to domain `.blueashdigital.tech`, path `/`, `Secure`, and `SameSite=Lax`.
- Sessions have a configurable idle timeout and absolute timeout. Current migration defaults are 30 minutes idle and 480 minutes absolute. Valid requests extend only the idle expiry.
- `GET /api/profile/me` validates the session and returns stable user ID, username, email, display name, portal role, MFA state, and permission keys.
- `GET /api/apps` returns only enabled applications assigned to the user. Administrators receive all enabled applications. Standard users require a `user_applications` assignment.
- `POST /api/auth/logout` revokes the server-side session and clears the shared cookie.
- Portal roles are `ADMINISTRATOR` and `USER`; granular portal permissions and per-application assignments are stored in PostgreSQL.

## Opportunity Radar Integration

Production uses `AUTH_MODE=blueash`. Every protected `/api` request forwards the existing
`blueash_session` cookie server-to-server to the portal's protected APIs. Opportunity Radar:

1. Resolves identity through `GET /api/profile/me`.
2. Resolves application access through `GET /api/apps`.
3. Requires an enabled application with slug `opportunity-radar`.
4. Requires the `ADMINISTRATOR` role and an exact stable-ID match with
   `APP_TRUSTED_ADMIN_USER_ID` for the initial production release.
5. Returns `401` for a missing/expired portal session, `403` for a missing assignment or trusted
   administrator match, and `503` when identity/configuration services are unavailable.

No Blue Ash session token is written to logs or stored in Opportunity Radar. Opportunity Radar
does not create a second cookie, session, password, administrator, or MFA challenge. The validated
identity is available on `request.state.identity` and its serialized form on `request.state.user`.
Portal app assignment alone does not grant Opportunity Radar access in the initial production mode.
All global writes and operational surfaces also carry an explicit administrator dependency.

Local development uses `AUTH_MODE=local`, which supplies an explicit development-only identity
without credentials or network calls. It is accepted only with `APP_ENV=development` and a loopback
`APP_PUBLIC_URL`. Missing/unknown modes fail startup; production never falls back to local auth.

## Transitional Browser Flow

1. An authenticated portal user launches Opportunity Radar. The shared cookie is sent to the app,
   the backend validates identity and the `opportunity-radar` assignment, and the requested page opens.
2. An unauthenticated direct visitor is redirected to the Blue Ash portal with a validated
   `returnTo=https://blueashdigital.tech/OpportunityRadar/...` query value.
3. If a session expires while the app is open, the next API check returns `401`; the frontend checks
   session state on focus and every 60 seconds and redirects through the same portal login flow.
4. Opportunity Radar sign-out calls the Blue Ash logout endpoint, clears the shared cookie for
   `.blueashdigital.tech`, and returns the browser to the portal.

## Deferred Portal/Deployment Dependencies

Do not change the Blue Ash portal as part of Phase 1A. Before a later production validation, the
Blue Ash administrator must verify an enabled application with:

- Slug: `opportunity-radar`
- Launch URL: `https://blueashdigital.tech/OpportunityRadar`
- User assignments as required

The current portal frontend does not consume a `returnTo` query after login. Opportunity Radar emits
the safe return value already, but exact deep-link restoration requires a small portal-side enhancement
to validate same-site `/OpportunityRadar` destinations and navigate there after password/MFA success.
That handoff change is intentionally deferred. This checkpoint must not be deployed as though the
transitional flow were the final authentication architecture.
