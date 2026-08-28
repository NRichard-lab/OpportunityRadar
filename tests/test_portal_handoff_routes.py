from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend import blueash_auth
from backend.blueash_auth import (
    BlueAshIdentity,
    BlueAshUnavailableError,
    PortalExchange,
)
import server


TRUSTED_ID = "17f975f1-e63a-4daa-985a-82d39ed60684"
USER_ID = "bceda542-92f0-46b6-9bc8-38a67c3ba272"
CLIENT_SECRET = "client-secret-with-at-least-32-random-characters"
RADAR_SECRET = "radar-state-secret-with-at-least-32-random-characters"
ACCESS_TOKEN = "portal-app-token-that-is-long-enough"
ORIGIN = "https://radar.blueashdigital.tech"


class PortalHandoffRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(server.app, base_url=ORIGIN)

    def test_health_is_public_and_never_calls_portal(self) -> None:
        portal_client = Mock()
        readiness = SimpleNamespace(
            payload={"status": "ok", "version": "test"},
            status_code=200,
        )
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client), patch.object(
            server._health_readiness_cache,
            "get",
            return_value=readiness,
        ):
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        portal_client.assert_not_called()
        portal_client.probe.assert_not_called()
        portal_client.authenticate.assert_not_called()
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_start_probes_then_redirects_with_signed_host_only_cookie(self) -> None:
        portal_client = Mock()
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            response = self.client.get(
                "/api/auth/start?returnTo=%2Fjobs%3Fcompany%3DBlue",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith(
            "https://api.blueashdigital.tech/api/app-auth/authorize?"
        ))
        portal_client.probe.assert_called_once_with()
        cookie = response.headers["set-cookie"]
        self.assertIn("__Host-opportunity_radar_handoff=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Domain=", cookie)

    def test_start_portal_outage_stays_local_and_returns_sanitized_503(self) -> None:
        portal_client = Mock()
        portal_client.probe.side_effect = BlueAshUnavailableError(
            "Blue Ash authentication is temporarily unavailable."
        )
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            response = self.client.get("/api/auth/start", follow_redirects=False)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("location", response.headers)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Authentication is temporarily unavailable", response.text)
        self.assertNotIn("api.blueashdigital.tech", response.text)

    def test_callback_exchanges_introspects_sets_raw_token_and_cleans_url(self) -> None:
        portal_client = Mock()
        portal_client.exchange.return_value = PortalExchange(
            access_token=ACCESS_TOKEN,
            expires_in=3_600,
            idle_expires_at=datetime.now(timezone.utc) + timedelta(minutes=29),
            absolute_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        portal_client.introspect.return_value = self.identity(role="USER", user_id=USER_ID)
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            attempt, cookie = blueash_auth.create_handoff_attempt("/jobs")
            self.client.cookies.set(
                "__Host-opportunity_radar_handoff",
                cookie,
                domain="radar.blueashdigital.tech",
                path="/",
            )
            response = self.client.get(
                f"/api/auth/callback?code=one-time-authorization-code-value&state={attempt.state}",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/jobs")
        self.assertNotIn("code=", response.headers["location"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        portal_client.exchange.assert_called_once_with(
            "one-time-authorization-code-value",
            attempt.code_verifier,
        )
        portal_client.introspect.assert_called_once_with(ACCESS_TOKEN)
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(value for value in cookies if value.startswith("__Host-opportunity_radar_session="))
        self.assertIn(f"={ACCESS_TOKEN};", session_cookie)
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("Secure", session_cookie)
        self.assertNotIn("Domain=", session_cookie)
        self.assertTrue(any("__Host-opportunity_radar_handoff=" in value and "Max-Age=0" in value for value in cookies))

    def test_consumed_callback_back_navigation_preserves_session_and_returns_clean_error_state(self) -> None:
        with self.runtime():
            response = self.client.get(
                "/api/auth/callback?code=already-consumed-code-value&state=missing-state-value",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/?auth=failed")
        self.assertNotIn("already-consumed", response.headers["location"])

    def test_callback_rejects_duplicate_or_oversized_credentials_with_clean_redirect(self) -> None:
        cases = (
            "/api/auth/callback?code=first-code-value-long-enough&code=second-code-value-long-enough&state=state-value",
            f"/api/auth/callback?code={'x' * 4097}&state=state-value",
            "/api/auth/callback?code=code-value-that-is-long-enough&state=first&state=second",
        )
        with self.runtime():
            for url in cases:
                with self.subTest(url=url[:100]):
                    response = self.client.get(url, follow_redirects=False)
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.headers["location"], "/?auth=failed")

    def test_portal_error_is_sanitized_after_state_validation(self) -> None:
        with self.runtime():
            attempt, cookie = blueash_auth.create_handoff_attempt("/jobs")
            self.client.cookies.set(
                "__Host-opportunity_radar_handoff",
                cookie,
                domain="radar.blueashdigital.tech",
                path="/",
            )
            response = self.client.get(
                f"/api/auth/callback?error=access_denied&state={attempt.state}",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/jobs?auth=denied")

    def test_exchange_forbidden_and_outage_map_to_sanitized_local_states(self) -> None:
        for exception, expected_state in (
            (blueash_auth.BlueAshAuthorizationError("internal Portal detail"), "denied"),
            (blueash_auth.BlueAshUnavailableError("internal Portal detail"), "unavailable"),
        ):
            with self.subTest(expected_state=expected_state):
                portal_client = Mock()
                portal_client.exchange.side_effect = exception
                with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
                    attempt, cookie = blueash_auth.create_handoff_attempt("/jobs")
                    self.client.cookies.set(
                        "__Host-opportunity_radar_handoff",
                        cookie,
                        domain="radar.blueashdigital.tech",
                        path="/",
                    )
                    response = self.client.get(
                        f"/api/auth/callback?code=one-time-authorization-code-value&state={attempt.state}",
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], f"/jobs?auth={expected_state}")
                self.assertNotIn("internal", response.headers["location"])

    def test_logout_clears_local_cookie_even_when_portal_revoke_fails(self) -> None:
        portal_client = Mock()
        portal_client.revoke.side_effect = BlueAshUnavailableError("Portal unavailable")
        self.client.cookies.set(
            "__Host-opportunity_radar_session",
            ACCESS_TOKEN,
            domain="radar.blueashdigital.tech",
            path="/",
        )
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            response = self.client.post("/api/auth/logout", headers={"Origin": ORIGIN})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirectUrl"], "https://blueashdigital.tech/")
        self.assertTrue(any(
            "__Host-opportunity_radar_session=" in value and "Max-Age=0" in value
            for value in response.headers.get_list("set-cookie")
        ))
        portal_client.revoke.assert_called_once_with(ACCESS_TOKEN)

    def test_unsafe_request_requires_exact_origin(self) -> None:
        portal_client = Mock()
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            missing = self.client.post("/api/auth/logout")
            foreign = self.client.post(
                "/api/auth/logout",
                headers={"Origin": "https://attacker.example"},
            )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(foreign.status_code, 403)
        portal_client.revoke.assert_not_called()

    def test_auth_public_allowlist_is_exact(self) -> None:
        portal_client = Mock()
        portal_client.authenticate.side_effect = blueash_auth.BlueAshAuthenticationError(
            "Blue Ash authentication is required."
        )
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            response = self.client.get("/api/auth/not-a-public-route")

        self.assertEqual(response.status_code, 401)
        portal_client.authenticate.assert_called_once_with("")

    def test_assigned_ordinary_user_reads_but_admin_mutation_is_forbidden(self) -> None:
        portal_client = Mock()
        portal_client.authenticate.return_value = self.identity(role="USER", user_id=USER_ID)
        self.client.cookies.set(
            "__Host-opportunity_radar_session",
            ACCESS_TOKEN,
            domain="radar.blueashdigital.tech",
            path="/",
        )
        with self.runtime(), patch.object(server, "_portal_auth_client", portal_client):
            read_response = self.client.get("/api/status")
            write_response = self.client.post(
                "/api/companies",
                headers={"Origin": ORIGIN},
                json={"name": "Read-only user cannot add this company"},
            )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(write_response.status_code, 403)
        self.assertIn("Administrator", write_response.text)

    @staticmethod
    def identity(*, role: str, user_id: str) -> BlueAshIdentity:
        return BlueAshIdentity(
            id=user_id,
            username="user",
            email="user@example.com",
            display_name="User",
            role=role,
            permissions=("applications.view",),
        )

    @staticmethod
    @contextmanager
    def runtime():
        auth_values = {
            "APP_ENV": "production",
            "AUTH_MODE": "portal_handoff",
            "APP_BASE_PATH": "",
            "APP_PUBLIC_URL": ORIGIN,
            "BLUEASH_PORTAL_PUBLIC_URL": "https://blueashdigital.tech",
            "BLUEASH_PORTAL_API_URL": "https://api.blueashdigital.tech",
            "BLUEASH_AUTH_CLIENT_ID": "opportunity-radar",
            "BLUEASH_AUTH_CLIENT_SECRET": CLIENT_SECRET,
            "OPPORTUNITY_RADAR_SECRET_KEY": RADAR_SECRET,
            "RADAR_SESSION_COOKIE_NAME": "__Host-opportunity_radar_session",
            "RADAR_SESSION_IDLE_SECONDS": 1_800,
            "RADAR_SESSION_ABSOLUTE_MAX_SECONDS": 28_800,
            "RADAR_INTROSPECTION_CACHE_SECONDS": 0,
            "RADAR_HANDOFF_STATE_TTL_SECONDS": 300,
            "APP_TRUSTED_ADMIN_USER_ID": TRUSTED_ID,
            "REQUIRE_EXISTING_DATABASE": True,
            "APP_ENABLE_BROWSER_JOBS": False,
            "APP_WRITE_FRONTEND_MIRRORS": False,
        }
        server_values = {
            "APP_ENV": "production",
            "AUTH_MODE": "portal_handoff",
            "APP_PUBLIC_ORIGIN": ORIGIN,
            "RADAR_SESSION_COOKIE_NAME": "__Host-opportunity_radar_session",
        }
        with patch.multiple(blueash_auth, **auth_values), patch.multiple(server, **server_values):
            yield


if __name__ == "__main__":
    unittest.main()
