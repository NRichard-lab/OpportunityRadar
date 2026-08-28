from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.phase3.yaml"
PORTAL_ORIGIN = "https://blueashdigital.tech"
PORTAL_API_ORIGIN = "https://api.blueashdigital.tech"
RADAR_ORIGIN = "https://radar.blueashdigital.tech"
RADAR_SESSION_COOKIE = "__Host-opportunity_radar_session"
PORTAL_SESSION_COOKIE = "__Host-blueash_portal_session"
PORTAL_PRE_AUTH_COOKIE = "__Host-blueash_pre_auth"
HOST_RESOLVER_RULES = (
    "MAP blueashdigital.tech 127.0.0.1,"
    "MAP api.blueashdigital.tech 127.0.0.1,"
    "MAP radar.blueashdigital.tech 127.0.0.1,"
    "EXCLUDE localhost"
)


def _required_synthetic_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required by the isolated Phase 3 browser test")
    return value


class Phase3HandoffE2E(unittest.TestCase):
    playwright: Playwright
    browser: Browser

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("PHASE3_SYNTHETIC_TEST_MODE") != "1":
            raise unittest.SkipTest("Phase 3 synthetic integration mode is not enabled")
        cls.synthetic_password = _required_synthetic_value("PHASE3_SYNTHETIC_PASSWORD")
        cls.synthetic_mfa_code = _required_synthetic_value("PHASE3_SYNTHETIC_MFA_CODE")
        cls.client_secret = _required_synthetic_value("BLUEASH_AUTH_CLIENT_SECRET")
        cls.playwright = sync_playwright().start()
        cdp_url = os.environ.get("PHASE3_BROWSER_CDP_URL", "")
        if cdp_url:
            cls.browser = cls.playwright.chromium.connect_over_cdp(cdp_url)
            cls.expect_docker_edge = True
        else:
            cls.browser = cls.playwright.chromium.launch(
                headless=True,
                args=[f"--host-resolver-rules={HOST_RESOLVER_RULES}", "--no-proxy-server"],
            )
            cls.expect_docker_edge = False

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()

    def setUp(self) -> None:
        self.context = self.browser.new_context(ignore_https_errors=True)
        self.context.set_default_timeout(20_000)
        self.context.set_default_navigation_timeout(35_000)
        self.radar_cookie_headers: list[str] = []
        self.callback_codes: list[str] = []
        self.page = self.context.new_page()
        self.page.on("request", self._observe_request)

    def tearDown(self) -> None:
        self.context.close()

    def _observe_request(self, request) -> None:
        parsed = urlsplit(request.url)
        if parsed.hostname != "radar.blueashdigital.tech":
            return
        try:
            cookie_header = request.all_headers().get("cookie", "")
        except Exception:
            cookie_header = ""
        self.radar_cookie_headers.append(cookie_header)
        if parsed.path == "/api/auth/callback":
            code = (parse_qs(parsed.query).get("code") or [""])[0]
            if code:
                self.callback_codes.append(code)

    def _assert_isolated_response(self, response) -> None:
        self.assertIsNotNone(response)
        server_addr = getattr(response, "server_addr", None)
        if callable(server_addr):
            address = server_addr()
            ip_address = ipaddress.ip_address(address.get("ipAddress"))
            if self.expect_docker_edge:
                self.assertTrue(ip_address.is_private)
                self.assertFalse(ip_address.is_loopback)
            else:
                self.assertTrue(ip_address.is_loopback)

    def _login(self, page: Page, username: str, *, mfa: bool) -> None:
        page.get_by_label("Username or Email").wait_for(state="visible")
        self.assertEqual(urlsplit(page.url).hostname, "blueashdigital.tech")
        page.get_by_label("Username or Email").fill(username)
        page.get_by_label("Password").fill(self.synthetic_password)
        page.get_by_role("button", name="Sign In", exact=True).click()
        if mfa:
            page.get_by_label("Verification Code").wait_for(state="visible")
            pre_auth_cookie = next(
                item
                for item in page.context.cookies([PORTAL_API_ORIGIN])
                if item["name"] == PORTAL_PRE_AUTH_COOKIE
            )
            self.assertEqual(pre_auth_cookie["domain"], "api.blueashdigital.tech")
            self.assertFalse(pre_auth_cookie["domain"].startswith("."))
            self.assertEqual(pre_auth_cookie["path"], "/")
            self.assertTrue(pre_auth_cookie["secure"])
            self.assertTrue(pre_auth_cookie["httpOnly"])
            self.assertEqual(pre_auth_cookie["sameSite"], "Lax")
            page.get_by_label("Verification Code").fill(self.synthetic_mfa_code)
            page.get_by_role("button", name="Verify", exact=True).click()
            page.get_by_label("Verification Code").wait_for(state="hidden")
            self.assertFalse(
                any(
                    item["name"] == PORTAL_PRE_AUTH_COOKIE
                    for item in page.context.cookies([PORTAL_API_ORIGIN])
                )
            )

    def _wait_for_radar_session(self, page: Page) -> dict[str, object]:
        page.wait_for_url(re.compile(r"^https://radar\.blueashdigital\.tech/(?!api(?:/|$))"))
        page.wait_for_function(
            "async () => (await fetch('/api/auth/session', {credentials: 'same-origin', cache: 'no-store'})).status === 200"
        )
        response = page.evaluate(
            "async () => { const r = await fetch('/api/auth/session', {credentials: 'same-origin', cache: 'no-store'}); return await r.json(); }"
        )
        self.assertIsInstance(response, dict)
        self.assertTrue(response.get("authenticated"))
        return response

    def test_01_health_is_public_and_does_not_start_portal_auth(self) -> None:
        portal_requests: list[str] = []
        self.page.on(
            "request",
            lambda request: portal_requests.append(request.url)
            if urlsplit(request.url).hostname in {"blueashdigital.tech", "api.blueashdigital.tech"}
            else None,
        )
        response = self.page.goto(f"{RADAR_ORIGIN}/api/health", wait_until="domcontentloaded")
        self._assert_isolated_response(response)
        self.assertEqual(response.status, 200)
        payload = json.loads(self.page.locator("body").inner_text())
        self.assertIn(payload.get("status"), {"healthy", "ok"})
        self.assertEqual(portal_requests, [])
        self.assertEqual(self.context.cookies(), [])

    def test_02_direct_access_login_mfa_cookie_isolation_refresh_and_exit(self) -> None:
        response = self.page.goto(RADAR_ORIGIN, wait_until="domcontentloaded")
        self._assert_isolated_response(response)
        self._login(self.page, "phase3-admin", mfa=True)
        identity = self._wait_for_radar_session(self.page)
        self.assertEqual(identity.get("id"), "11111111-1111-4111-8111-111111111111")

        final_url = urlsplit(self.page.url)
        self.assertEqual(final_url.hostname, "radar.blueashdigital.tech")
        self.assertNotIn("code", parse_qs(final_url.query))
        self.assertNotIn("state", parse_qs(final_url.query))

        portal_cookies = self.context.cookies([PORTAL_API_ORIGIN])
        portal_cookie = next(item for item in portal_cookies if item["name"] == PORTAL_SESSION_COOKIE)
        self.assertEqual(portal_cookie["domain"], "api.blueashdigital.tech")
        self.assertTrue(portal_cookie["secure"])
        self.assertTrue(portal_cookie["httpOnly"])
        self.assertEqual(portal_cookie["sameSite"], "Lax")

        radar_cookies = self.context.cookies([RADAR_ORIGIN])
        radar_cookie = next(item for item in radar_cookies if item["name"] == RADAR_SESSION_COOKIE)
        self.assertEqual(radar_cookie["domain"], "radar.blueashdigital.tech")
        self.assertEqual(radar_cookie["path"], "/")
        self.assertTrue(radar_cookie["secure"])
        self.assertTrue(radar_cookie["httpOnly"])
        self.assertEqual(radar_cookie["sameSite"], "Lax")
        self.assertTrue(self.radar_cookie_headers)
        self.assertTrue(all(f"{PORTAL_SESSION_COOKIE}=" not in value for value in self.radar_cookie_headers))

        browser_storage = self.page.evaluate("() => JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
        self.assertNotIn(radar_cookie["value"], browser_storage)
        self.assertNotIn(portal_cookie["value"], browser_storage)

        self.page.reload(wait_until="domcontentloaded")
        refreshed = self._wait_for_radar_session(self.page)
        self.assertEqual(refreshed.get("id"), identity.get("id"))
        self._assert_secrets_absent_from_logs(
            [
                *self.callback_codes,
                radar_cookie["value"],
                portal_cookie["value"],
                self.client_secret,
                self.synthetic_password,
                self.synthetic_mfa_code,
            ]
        )

        exit_button = self.page.get_by_role(
            "button", name=re.compile(r"^(?:Sign Out|Exit Opportunity Radar|Exit Radar)$")
        )
        exit_button.click()
        self.page.wait_for_url(re.compile(r"^https://blueashdigital\.tech(?:/|$)"))
        self.assertFalse(any(item["name"] == RADAR_SESSION_COOKIE for item in self.context.cookies([RADAR_ORIGIN])))
        self.page.get_by_role("button", name="Dashboard", exact=False).wait_for(state="visible")
        self.assertTrue(any(item["name"] == PORTAL_SESSION_COOKIE for item in self.context.cookies([PORTAL_API_ORIGIN])))

    def test_03_unassigned_user_is_denied_without_a_redirect_loop(self) -> None:
        navigations: list[str] = []
        self.page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == self.page.main_frame else None)
        self.page.goto(RADAR_ORIGIN, wait_until="domcontentloaded")
        self._login(self.page, "phase3-unassigned", mfa=False)
        self.page.wait_for_timeout(2_000)
        body = self.page.locator("body").inner_text().casefold()
        self.assertRegex(body, r"access|denied|do not have access|forbidden")
        self.assertLess(len(navigations), 12, "unassigned access entered a redirect loop")
        self.assertFalse(any(item["name"] == RADAR_SESSION_COOKIE for item in self.context.cookies([RADAR_ORIGIN])))

    def test_04_authenticated_portal_card_launch_completes_the_handoff(self) -> None:
        self.page.goto(PORTAL_ORIGIN, wait_until="domcontentloaded")
        self._login(self.page, "phase3-admin", mfa=True)
        radar_card = self.page.locator("article.app-card").filter(has_text="Opportunity Radar")
        radar_card.get_by_role("button", name="Launch", exact=True).wait_for(state="visible")

        with self.context.expect_page() as popup_info:
            radar_card.get_by_role("button", name="Launch", exact=True).click()
        radar_page = popup_info.value
        radar_page.on("request", self._observe_request)
        radar_page.wait_for_load_state("domcontentloaded")
        identity = self._wait_for_radar_session(radar_page)

        self.assertEqual(identity.get("id"), "11111111-1111-4111-8111-111111111111")
        final_url = urlsplit(radar_page.url)
        self.assertEqual(final_url.hostname, "radar.blueashdigital.tech")
        self.assertNotIn("code", parse_qs(final_url.query))
        self.assertNotIn("state", parse_qs(final_url.query))
        self.assertTrue(
            any(item["name"] == PORTAL_SESSION_COOKIE for item in self.context.cookies([PORTAL_API_ORIGIN]))
        )
        radar_page.close()

    def test_05_portal_logout_immediately_revokes_the_child_session(self) -> None:
        self.page.goto(RADAR_ORIGIN, wait_until="domcontentloaded")
        self._login(self.page, "phase3-assigned", mfa=False)
        identity = self._wait_for_radar_session(self.page)
        self.assertEqual(identity.get("id"), "22222222-2222-4222-8222-222222222222")

        mutation_status = self.page.evaluate(
            """async () => {
                const response = await fetch('/api/companies', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Synthetic authorization probe'})
                });
                return response.status;
            }"""
        )
        self.assertEqual(mutation_status, 403)

        self.page.goto(PORTAL_ORIGIN, wait_until="domcontentloaded")
        self.page.get_by_role("button", name="Logout", exact=False).wait_for(state="visible")
        self.page.get_by_role("button", name="Logout", exact=False).click()
        self.page.get_by_label("Username or Email").wait_for(state="visible")

        session_response = self.page.goto(f"{RADAR_ORIGIN}/api/auth/session", wait_until="domcontentloaded")
        self.assertEqual(session_response.status, 401)
        self.assertFalse(any(item["name"] == RADAR_SESSION_COOKIE for item in self.context.cookies([RADAR_ORIGIN])))

    def _assert_secrets_absent_from_logs(self, values: list[str]) -> None:
        environment = os.environ.copy()
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "logs", "--no-color"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logs = result.stdout + result.stderr
        for value in values:
            self.assertFalse(value and value in logs, "runtime logs contain captured credential material")


if __name__ == "__main__":
    unittest.main(verbosity=2)
