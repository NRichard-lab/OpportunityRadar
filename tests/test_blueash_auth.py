from __future__ import annotations

import hashlib
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, call, patch

from backend import blueash_auth
from backend.outbound_security import UnsafeOutboundDestination


TRUSTED_ID = "17f975f1-e63a-4daa-985a-82d39ed60684"
USER_ID = "bceda542-92f0-46b6-9bc8-38a67c3ba272"
CLIENT_SECRET = "client-secret-with-at-least-32-random-characters"
RADAR_SECRET = "radar-state-secret-with-at-least-32-random-characters"
ACCESS_TOKEN = "portal-app-token-that-is-long-enough"


class BlueAshAuthTests(unittest.TestCase):
    def test_local_mode_uses_development_identity_without_network(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="development",
            AUTH_MODE="local",
            APP_PUBLIC_URL="http://127.0.0.1:5173",
        ), patch.object(blueash_auth.SSRFProtectedSession, "post") as request:
            identity = blueash_auth.PortalHandoffClient().authenticate("")

        self.assertTrue(identity.development_bypass)
        self.assertEqual(identity.role, "ADMINISTRATOR")
        request.assert_not_called()

    def test_local_mode_cannot_bypass_outside_explicit_development(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="production",
            AUTH_MODE="local",
            APP_PUBLIC_URL="https://radar.blueashdigital.tech",
        ):
            with self.assertRaises(blueash_auth.BlueAshConfigurationError):
                blueash_auth.local_identity()

    def test_unknown_auth_mode_fails_closed(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="development",
            AUTH_MODE="locla",
            APP_PUBLIC_URL="http://127.0.0.1:5173",
        ):
            with self.assertRaises(blueash_auth.BlueAshConfigurationError):
                blueash_auth.validate_auth_configuration()

    def test_production_pins_all_three_public_origins(self) -> None:
        with self.portal_configuration(production=True):
            blueash_auth.validate_auth_configuration()
            with patch.object(blueash_auth, "BLUEASH_PORTAL_API_URL", "https://auth.example.com"):
                with self.assertRaisesRegex(blueash_auth.BlueAshConfigurationError, "exactly"):
                    blueash_auth.validate_auth_configuration()

    def test_production_rejects_placeholder_secret(self) -> None:
        with self.portal_configuration(production=True), patch.object(
            blueash_auth,
            "BLUEASH_AUTH_CLIENT_SECRET",
            "replace-me-with-a-production-secret-value",
        ):
            with self.assertRaisesRegex(blueash_auth.BlueAshConfigurationError, "non-placeholder"):
                blueash_auth.validate_auth_configuration()

    def test_safe_return_path_accepts_ui_path_and_rejects_ambiguous_inputs(self) -> None:
        with patch.object(blueash_auth, "APP_BASE_PATH", ""):
            self.assertEqual(
                blueash_auth.safe_return_path("/jobs?company=Blue+Ash"),
                "/jobs?company=Blue+Ash",
            )
            for unsafe in (
                "https://attacker.example/",
                "//attacker.example/",
                "/api/jobs",
                "/x/../api/jobs",
                "/x/%2e%2e/api/jobs",
                "/x/%252e%252e/api/jobs",
                "/jobs#callback",
                "/%5c%5cattacker.example/",
                "/jobs%0d%0aLocation:%20https://attacker.example/",
            ):
                self.assertEqual(blueash_auth.safe_return_path(unsafe), "/", unsafe)

    def test_signed_handoff_round_trip_binds_state_pkce_and_return_path(self) -> None:
        with self.portal_configuration():
            attempt, cookie = blueash_auth.create_handoff_attempt("/jobs?company=Blue", now=1_000)
            restored = blueash_auth.consume_handoff_cookie(cookie, attempt.state, now=1_100)

        self.assertEqual(restored.return_path, "/jobs?company=Blue")
        self.assertEqual(restored.code_verifier, attempt.code_verifier)
        self.assertEqual(
            restored.code_challenge,
            blueash_auth._base64url(hashlib.sha256(restored.code_verifier.encode("ascii")).digest()),
        )

    def test_handoff_rejects_tampering_state_mismatch_and_expiry(self) -> None:
        with self.portal_configuration():
            attempt, cookie = blueash_auth.create_handoff_attempt("/jobs", now=1_000)
            for supplied_cookie, state, now in (
                (cookie + "x", attempt.state, 1_100),
                (cookie, "different-state-value-that-is-long-enough", 1_100),
                (cookie, attempt.state, 1_301),
            ):
                with self.assertRaises(blueash_auth.BlueAshAuthenticationError):
                    blueash_auth.consume_handoff_cookie(supplied_cookie, state, now=now)

    def test_authorize_url_uses_api_origin_and_exact_snake_case_contract(self) -> None:
        with self.portal_configuration():
            attempt, _ = blueash_auth.create_handoff_attempt("/jobs", now=1_000)
            authorize_url = blueash_auth.build_authorize_url(attempt)

        parsed = urlsplit(authorize_url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://api.blueashdigital.tech/api/app-auth/authorize")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "client_id": ["opportunity-radar"],
                "redirect_uri": ["http://127.0.0.1:5173/api/auth/callback"],
                "response_type": ["code"],
                "state": [attempt.state],
                "code_challenge": [attempt.code_challenge],
                "code_challenge_method": ["S256"],
                "return_path": ["/jobs"],
            },
        )

    def test_start_probe_is_bounded_and_uses_fixed_api_health_path(self) -> None:
        response = Mock(status_code=200)
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "get",
            return_value=response,
        ) as request:
            blueash_auth.PortalHandoffClient(timeout_seconds=3.0).probe()

        request.assert_called_once_with(
            "https://api.blueashdigital.tech/api/health",
            timeout=3.0,
            allow_redirects=False,
        )

    def test_probe_hides_outbound_destination_details(self) -> None:
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "get",
            side_effect=UnsafeOutboundDestination("unsafe internal address 127.0.0.1"),
        ):
            with self.assertRaisesRegex(blueash_auth.BlueAshUnavailableError, "temporarily unavailable") as raised:
                blueash_auth.PortalHandoffClient().probe()
        self.assertNotIn("127.0.0.1", str(raised.exception))

    def test_exchange_uses_http_basic_and_exact_json_contract(self) -> None:
        response = self.response(
            200,
            {
                "access_token": ACCESS_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3_600,
                "idle_expires_at": self.timestamp(minutes=29),
                "absolute_expires_at": self.timestamp(hours=1),
            },
        )
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            return_value=response,
        ) as request:
            exchange = blueash_auth.PortalHandoffClient().exchange(
                "one-time-authorization-code-value",
                "pkce-verifier-value-that-is-long-enough-for-the-contract",
            )

        self.assertEqual(exchange.access_token, ACCESS_TOKEN)
        request.assert_called_once_with(
            "https://api.blueashdigital.tech/api/app-auth/exchange",
            json={
                "code": "one-time-authorization-code-value",
                "code_verifier": "pkce-verifier-value-that-is-long-enough-for-the-contract",
                "redirect_uri": "http://127.0.0.1:5173/api/auth/callback",
            },
            auth=("opportunity-radar", CLIENT_SECRET),
            timeout=5.0,
            allow_redirects=False,
        )

    def test_exchange_and_introspection_fail_closed_on_portal_errors(self) -> None:
        operations = (
            (
                "exchange-forbidden",
                403,
                blueash_auth.BlueAshAuthorizationError,
                lambda client: client.exchange(
                    "one-time-authorization-code-value",
                    "pkce-verifier-value-that-is-long-enough-for-the-contract",
                ),
            ),
            (
                "exchange-outage",
                503,
                blueash_auth.BlueAshUnavailableError,
                lambda client: client.exchange(
                    "one-time-authorization-code-value",
                    "pkce-verifier-value-that-is-long-enough-for-the-contract",
                ),
            ),
            (
                "introspection-forbidden",
                403,
                blueash_auth.BlueAshAuthorizationError,
                lambda client: client.introspect(ACCESS_TOKEN),
            ),
            (
                "introspection-outage",
                503,
                blueash_auth.BlueAshUnavailableError,
                lambda client: client.introspect(ACCESS_TOKEN),
            ),
        )
        for label, status, exception, operation in operations:
            with self.subTest(label=label), self.portal_configuration(), patch.object(
                blueash_auth.SSRFProtectedSession,
                "post",
                return_value=self.response(status, {}),
            ):
                with self.assertRaises(exception):
                    operation(blueash_auth.PortalHandoffClient())

    def test_assigned_ordinary_user_authenticates_without_admin_promotion(self) -> None:
        response = self.response(200, self.introspection_payload(role="USER", user_id=USER_ID))
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            return_value=response,
        ):
            identity = blueash_auth.PortalHandoffClient().introspect(ACCESS_TOKEN)

        self.assertEqual(identity.id, USER_ID)
        self.assertEqual(identity.role, "USER")
        self.assertFalse(blueash_auth.is_administrator(identity))

    def test_introspection_requires_exact_application_binding(self) -> None:
        payload = self.introspection_payload()
        payload["application_slug"] = "another-app"
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            return_value=self.response(200, payload),
        ):
            with self.assertRaises(blueash_auth.BlueAshAuthorizationError):
                blueash_auth.PortalHandoffClient().introspect(ACCESS_TOKEN)

    def test_inactive_introspection_is_never_accepted(self) -> None:
        with self.portal_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            return_value=self.response(200, {"active": False}),
        ):
            with self.assertRaises(blueash_auth.BlueAshAuthenticationError):
                blueash_auth.PortalHandoffClient().introspect(ACCESS_TOKEN)

    def test_default_zero_cache_observes_revocation_on_next_request(self) -> None:
        active = self.response(200, self.introspection_payload())
        inactive = self.response(200, {"active": False})
        with self.portal_configuration(), patch.object(
            blueash_auth,
            "RADAR_INTROSPECTION_CACHE_SECONDS",
            0,
        ), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            side_effect=[active, inactive],
        ) as request:
            client = blueash_auth.PortalHandoffClient()
            client.introspect(ACCESS_TOKEN)
            with self.assertRaises(blueash_auth.BlueAshAuthenticationError):
                client.introspect(ACCESS_TOKEN)

        self.assertEqual(request.call_count, 2)

    def test_optional_success_cache_is_invalidated_before_revoke(self) -> None:
        active = self.response(200, self.introspection_payload())
        revoked = self.response(204, None)
        inactive = self.response(200, {"active": False})
        with self.portal_configuration(), patch.object(
            blueash_auth,
            "RADAR_INTROSPECTION_CACHE_SECONDS",
            30,
        ), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            side_effect=[active, revoked, inactive],
        ) as request:
            client = blueash_auth.PortalHandoffClient()
            client.introspect(ACCESS_TOKEN)
            client.introspect(ACCESS_TOKEN)
            client.revoke(ACCESS_TOKEN)
            with self.assertRaises(blueash_auth.BlueAshAuthenticationError):
                client.introspect(ACCESS_TOKEN)

        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            request.call_args_list[1],
            call(
                "https://api.blueashdigital.tech/api/app-auth/revoke",
                json={"token": ACCESS_TOKEN},
                auth=("opportunity-radar", CLIENT_SECRET),
                timeout=5.0,
                allow_redirects=False,
            ),
        )

    def test_introspection_rejects_noncanonical_user_id_and_oversized_permissions(self) -> None:
        for update in (
            {"user_id": USER_ID.upper()},
            {"permissions": ["permission"] * 257},
            {"permissions": ["x" * 257]},
        ):
            payload = self.introspection_payload()
            payload.update(update)
            with self.portal_configuration(), patch.object(
                blueash_auth.SSRFProtectedSession,
                "post",
                return_value=self.response(200, payload),
            ):
                with self.assertRaises(blueash_auth.BlueAshUnavailableError):
                    blueash_auth.PortalHandoffClient().introspect(ACCESS_TOKEN)

    def test_trusted_administrator_requires_exact_role_and_id(self) -> None:
        administrator = blueash_auth.BlueAshIdentity(
            id=TRUSTED_ID,
            username="admin",
            email="admin@example.com",
            display_name="Admin",
            role="ADMINISTRATOR",
            permissions=(),
        )
        ordinary = blueash_auth.BlueAshIdentity(
            id=TRUSTED_ID,
            username="user",
            email="user@example.com",
            display_name="User",
            role="USER",
            permissions=("*",),
        )
        with patch.multiple(
            blueash_auth,
            APP_ENV="production",
            AUTH_MODE="portal_handoff",
            APP_TRUSTED_ADMIN_USER_ID=TRUSTED_ID,
        ):
            self.assertTrue(blueash_auth.is_trusted_initial_administrator(administrator))
            self.assertFalse(blueash_auth.is_trusted_initial_administrator(ordinary))

    @staticmethod
    def response(status_code: int, payload: object | None) -> Mock:
        response = Mock(status_code=status_code, headers={}, content=b"{}")
        if payload is not None:
            response.json.return_value = payload
        return response

    @staticmethod
    def timestamp(*, minutes: int = 0, hours: int = 0) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=minutes, hours=hours)).isoformat()

    @classmethod
    def introspection_payload(cls, *, role: str = "ADMINISTRATOR", user_id: str = TRUSTED_ID) -> dict[str, object]:
        return {
            "active": True,
            "user_id": user_id,
            "username": "nicholas",
            "email": "nicholas@example.com",
            "display_name": "Nicholas",
            "role": role,
            "permissions": ["applications.view"],
            "application_slug": "opportunity-radar",
            "idle_expires_at": cls.timestamp(minutes=29),
            "absolute_expires_at": cls.timestamp(hours=1),
        }

    @staticmethod
    @contextmanager
    def portal_configuration(*, production: bool = False):
        values = {
            "APP_ENV": "production" if production else "development",
            "AUTH_MODE": "portal_handoff",
            "APP_BASE_PATH": "",
            "APP_PUBLIC_URL": (
                "https://radar.blueashdigital.tech" if production else "http://127.0.0.1:5173"
            ),
            "BLUEASH_PORTAL_PUBLIC_URL": "https://blueashdigital.tech",
            "BLUEASH_PORTAL_API_URL": "https://api.blueashdigital.tech",
            "BLUEASH_AUTH_CLIENT_ID": "opportunity-radar",
            "BLUEASH_AUTH_CLIENT_SECRET": CLIENT_SECRET,
            "OPPORTUNITY_RADAR_SECRET_KEY": RADAR_SECRET,
            "RADAR_SESSION_COOKIE_NAME": (
                "__Host-opportunity_radar_session" if production else "opportunity_radar_session"
            ),
            "RADAR_SESSION_IDLE_SECONDS": 1_800,
            "RADAR_SESSION_ABSOLUTE_MAX_SECONDS": 28_800,
            "RADAR_INTROSPECTION_CACHE_SECONDS": 0,
            "RADAR_HANDOFF_STATE_TTL_SECONDS": 300,
            "APP_TRUSTED_ADMIN_USER_ID": TRUSTED_ID,
            "REQUIRE_EXISTING_DATABASE": production,
            "APP_ENABLE_BROWSER_JOBS": False,
            "APP_WRITE_FRONTEND_MIRRORS": False,
        }
        with patch.multiple(blueash_auth, **values):
            yield


if __name__ == "__main__":
    unittest.main()
