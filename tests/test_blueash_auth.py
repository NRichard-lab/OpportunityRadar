from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from backend import blueash_auth
from backend.outbound_security import UnsafeOutboundDestination


class BlueAshAuthTests(unittest.TestCase):
    def test_local_mode_uses_development_identity_without_network(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="development", AUTH_MODE="local", APP_PUBLIC_URL="http://127.0.0.1:5173",
        ), patch.object(blueash_auth.SSRFProtectedSession, "get") as request:
            identity = blueash_auth.BlueAshAuthClient().authenticate("")

        self.assertTrue(identity.development_bypass)
        self.assertEqual(identity.role, "ADMINISTRATOR")
        request.assert_not_called()

    def test_blueash_mode_forwards_session_and_requires_app_assignment(self) -> None:
        profile = Mock(ok=True, status_code=200)
        profile.json.return_value = {
            "id": "user-123", "username": "nicholas", "email": "nicholas@example.com",
            "display_name": "Nicholas", "role": "USER", "permissions": ["applications.view"],
        }
        applications = Mock(ok=True, status_code=200)
        applications.json.return_value = [{"slug": "opportunity-radar"}]

        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession, "get", side_effect=[profile, applications]
        ) as request:
            identity = blueash_auth.BlueAshAuthClient().authenticate("shared-token")

        self.assertEqual(identity.id, "user-123")
        self.assertEqual(identity.display_name, "Nicholas")
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "https://api.blueashdigital.tech/api/profile/me",
                    headers={"Cookie": "blueash_session=shared-token"}, timeout=5.0,
                    allow_redirects=True,
                ),
                call(
                    "https://api.blueashdigital.tech/api/apps",
                    headers={"Cookie": "blueash_session=shared-token"}, timeout=5.0,
                    allow_redirects=True,
                ),
            ],
        )

    def test_missing_app_assignment_is_forbidden(self) -> None:
        profile = Mock(ok=True, status_code=200)
        profile.json.return_value = {"id": "user-123", "username": "user", "email": "user@example.com", "role": "USER"}
        applications = Mock(ok=True, status_code=200)
        applications.json.return_value = [{"slug": "another-app"}]

        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession, "get", side_effect=[profile, applications]
        ):
            with self.assertRaises(blueash_auth.BlueAshAuthorizationError):
                blueash_auth.BlueAshAuthClient().authenticate("shared-token")

    def test_expired_blueash_session_is_unauthenticated(self) -> None:
        response = Mock(ok=False, status_code=401)
        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession, "get", return_value=response
        ):
            with self.assertRaises(blueash_auth.BlueAshAuthenticationError):
                blueash_auth.BlueAshAuthClient().authenticate("expired-token")

    def test_unsafe_blueash_destination_returns_safe_unavailable_error(self) -> None:
        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "get",
            side_effect=UnsafeOutboundDestination("unsafe internal address 127.0.0.1"),
        ):
            with self.assertRaisesRegex(
                blueash_auth.BlueAshUnavailableError,
                "temporarily unavailable",
            ) as raised:
                blueash_auth.BlueAshAuthClient().authenticate("shared-token")

        self.assertNotIn("127.0.0.1", str(raised.exception))

    def test_logout_uses_protected_session(self) -> None:
        response = Mock(status_code=204)
        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession,
            "post",
            return_value=response,
        ) as request:
            blueash_auth.BlueAshAuthClient().logout("shared-token")

        request.assert_called_once_with(
            "https://api.blueashdigital.tech/api/auth/logout",
            headers={"Cookie": "blueash_session=shared-token"},
            timeout=5.0,
            allow_redirects=True,
        )

    def test_login_url_only_accepts_opportunity_radar_paths(self) -> None:
        with (
            patch.object(blueash_auth, "APP_BASE_PATH", "/OpportunityRadar"),
            patch.object(blueash_auth, "APP_PUBLIC_URL", "https://blueashdigital.tech/OpportunityRadar"),
            patch.object(blueash_auth, "BLUEASH_LOGIN_URL", "https://blueashdigital.tech/"),
        ):
            deep_link = blueash_auth.login_url("/OpportunityRadar/utilities?tab=email")
            rejected = blueash_auth.login_url("https://attacker.example/")

        self.assertIn("returnTo=https%3A%2F%2Fblueashdigital.tech%2FOpportunityRadar%2Futilities%3Ftab%3Demail", deep_link)
        self.assertNotIn("attacker", rejected)

    def test_local_mode_cannot_bypass_outside_explicit_development(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="production", AUTH_MODE="local", APP_PUBLIC_URL="https://radar.example.com",
        ):
            with self.assertRaises(blueash_auth.BlueAshConfigurationError):
                blueash_auth.BlueAshAuthClient().authenticate("")

    def test_production_rejects_browser_jobs_without_dns_pinned_egress(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="production",
            AUTH_MODE="blueash",
            APP_ENABLE_BROWSER_JOBS=True,
            APP_PUBLIC_URL="https://blueashdigital.tech/OpportunityRadar",
            BLUEASH_API_URL="https://api.blueashdigital.tech",
            BLUEASH_LOGIN_URL="https://blueashdigital.tech/",
            BLUEASH_SESSION_COOKIE="blueash_session",
            BLUEASH_APP_SLUG="opportunity-radar",
        ):
            with self.assertRaisesRegex(blueash_auth.BlueAshConfigurationError, "DNS-pinned"):
                blueash_auth.validate_auth_configuration()

    def test_unknown_auth_mode_cannot_return_local_identity(self) -> None:
        with patch.multiple(
            blueash_auth,
            APP_ENV="development", AUTH_MODE="locla", APP_PUBLIC_URL="http://127.0.0.1:5173",
        ):
            with self.assertRaises(blueash_auth.BlueAshConfigurationError):
                blueash_auth.BlueAshAuthClient().authenticate("")

    def test_permissions_do_not_promote_a_user_to_administrator(self) -> None:
        identity = blueash_auth.BlueAshIdentity(
            id="17f975f1-e63a-4daa-985a-82d39ed60684", username="user", email="user@example.com",
            display_name="User", role="USER", permissions=("*",),
        )
        with patch.multiple(blueash_auth, APP_ENV="production", AUTH_MODE="blueash"):
            self.assertFalse(blueash_auth.is_administrator(identity))

    def test_trusted_administrator_requires_exact_role_and_id(self) -> None:
        trusted_id = "17f975f1-e63a-4daa-985a-82d39ed60684"
        administrator = blueash_auth.BlueAshIdentity(
            id=trusted_id.upper(), username="admin", email="admin@example.com",
            display_name="Admin", role="ADMINISTRATOR", permissions=(),
        )
        wrong_id = blueash_auth.BlueAshIdentity(
            id="bceda542-92f0-46b6-9bc8-38a67c3ba272", username="admin2", email="admin2@example.com",
            display_name="Other Admin", role="ADMINISTRATOR", permissions=("*",),
        )
        user = blueash_auth.BlueAshIdentity(
            id=trusted_id, username="user", email="user@example.com",
            display_name="User", role="USER", permissions=("*",),
        )
        with patch.multiple(
            blueash_auth,
            APP_ENV="production", AUTH_MODE="blueash", APP_TRUSTED_ADMIN_USER_ID=trusted_id,
        ):
            self.assertTrue(blueash_auth.is_trusted_initial_administrator(administrator))
            self.assertFalse(blueash_auth.is_trusted_initial_administrator(wrong_id))
            self.assertFalse(blueash_auth.is_trusted_initial_administrator(user))

    def test_unknown_identity_role_is_denied(self) -> None:
        profile = Mock(ok=True, status_code=200)
        profile.json.return_value = {
            "id": "17f975f1-e63a-4daa-985a-82d39ed60684", "username": "user",
            "email": "user@example.com", "role": "OWNER",
        }
        applications = Mock(ok=True, status_code=200)
        applications.json.return_value = [{"slug": "opportunity-radar"}]
        with self.blueash_configuration(), patch.object(
            blueash_auth.SSRFProtectedSession, "get", side_effect=[profile, applications]
        ):
            with self.assertRaises(blueash_auth.BlueAshAuthorizationError):
                blueash_auth.BlueAshAuthClient().authenticate("shared-token")

    @staticmethod
    def blueash_configuration():
        return patch.multiple(
            blueash_auth,
            APP_ENV="development", AUTH_MODE="blueash",
            APP_PUBLIC_URL="http://127.0.0.1:5173",
            BLUEASH_API_URL="https://api.blueashdigital.tech",
            BLUEASH_LOGIN_URL="https://blueashdigital.tech/",
            BLUEASH_SESSION_COOKIE="blueash_session",
            BLUEASH_APP_SLUG="opportunity-radar",
        )


if __name__ == "__main__":
    unittest.main()
