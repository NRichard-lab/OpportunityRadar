from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import Mock, patch

from backend.browser_egress_proxy import (
    BrowserProxyProtocolError,
    BrowserEgressProxyServer,
    authorize_browser_proxy_request,
    connect_pinned_destination,
    parse_browser_proxy_request,
)
from backend.outbound_security import (
    BROWSER_EGRESS_MODE,
    BROWSER_PROXY_PORT,
    BrowserEgressConfigurationError,
    BrowserRuntimeBoundary,
    UnsafeOutboundDestination,
    _chromium_binary_version,
    _sanitized_process_environment,
    launch_playwright_chromium,
)


def resolving_to(*addresses: str):
    def resolver(host: str, port: int, family: int, socktype: int, protocol: int):
        del host, family, socktype, protocol
        answers = []
        for address in addresses:
            answer_family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if answer_family == socket.AF_INET6 else (address, port)
            answers.append((answer_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return answers

    return resolver


class BrowserProxyPolicyTests(unittest.TestCase):
    def test_connect_private_and_metadata_destinations_are_blocked(self) -> None:
        for authority in ("127.0.0.1:443", "169.254.169.254:80", "10.20.30.40:443"):
            with self.subTest(authority=authority):
                request = parse_browser_proxy_request(
                    f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
                )
                with self.assertRaises(UnsafeOutboundDestination):
                    authorize_browser_proxy_request(request)

    def test_proxy_rejects_hostname_when_any_dns_answer_is_private(self) -> None:
        request = parse_browser_proxy_request(
            b"CONNECT jobs.example:443 HTTP/1.1\r\nHost: jobs.example:443\r\n\r\n"
        )

        with self.assertRaisesRegex(UnsafeOutboundDestination, "not globally routable"):
            authorize_browser_proxy_request(
                request,
                resolver=resolving_to("93.184.216.34", "172.17.0.1"),
            )

    def test_proxy_connects_to_the_validated_numeric_address(self) -> None:
        request = parse_browser_proxy_request(
            b"CONNECT jobs.example:443 HTTP/1.1\r\nHost: jobs.example:443\r\n\r\n"
        )
        destination = authorize_browser_proxy_request(
            request,
            resolver=resolving_to("93.184.216.34"),
        )
        stream = Mock()

        with patch("backend.browser_egress_proxy.socket.socket", return_value=stream) as constructor:
            returned = connect_pinned_destination(destination)

        self.assertIs(returned, stream)
        constructor.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        stream.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_connect_is_restricted_to_tls_while_parsed_http_port_80_remains_allowed(self) -> None:
        connect = parse_browser_proxy_request(
            b"CONNECT jobs.example:80 HTTP/1.1\r\nHost: jobs.example:80\r\n\r\n"
        )
        with self.assertRaises(UnsafeOutboundDestination):
            authorize_browser_proxy_request(connect, resolver=resolving_to("93.184.216.34"))

        plain_http = parse_browser_proxy_request(
            b"GET http://jobs.example/openings HTTP/1.1\r\nHost: jobs.example\r\n\r\n"
        )
        destination = authorize_browser_proxy_request(
            plain_http,
            resolver=resolving_to("93.184.216.34"),
        )
        self.assertEqual(destination.port, 80)

    def test_connect_forwards_bytes_coalesced_after_proxy_headers(self) -> None:
        client = Mock()
        upstream = Mock()
        request = parse_browser_proxy_request(
            b"CONNECT jobs.example:443 HTTP/1.1\r\nHost: jobs.example:443\r\n\r\n",
            buffered_body=b"tls-client-hello",
        )
        server = BrowserEgressProxyServer("/unused", resolver=resolving_to("93.184.216.34"))

        with (
            patch("backend.browser_egress_proxy._receive_proxy_header", return_value=(
                b"CONNECT jobs.example:443 HTTP/1.1\r\nHost: jobs.example:443\r\n\r\n",
                request.buffered_body,
            )),
            patch("backend.browser_egress_proxy.connect_pinned_destination", return_value=upstream),
            patch("backend.browser_egress_proxy._pump_bidirectionally") as pump,
        ):
            server._handle_client(client)

        client.sendall.assert_called_once_with(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        upstream.sendall.assert_called_once_with(b"tls-client-hello")
        pump.assert_called_once_with(client, upstream)
        upstream.close.assert_called_once_with()

    def test_plain_http_absolute_form_is_supported_and_credentials_are_rejected(self) -> None:
        request = parse_browser_proxy_request(
            b"GET http://jobs.example/openings?q=one HTTP/1.1\r\nHost: jobs.example\r\n\r\n"
        )
        self.assertEqual(request.destination_url, "http://jobs.example/openings?q=one")
        self.assertEqual(request.upstream_target, "/openings?q=one")

        unsafe = parse_browser_proxy_request(
            b"GET http://user:secret@jobs.example/ HTTP/1.1\r\nHost: jobs.example\r\n\r\n"
        )
        with self.assertRaises(UnsafeOutboundDestination):
            authorize_browser_proxy_request(unsafe, resolver=resolving_to("93.184.216.34"))

    def test_proxy_parser_rejects_smuggling_and_unbounded_headers(self) -> None:
        malformed = (
            b"GET http://jobs.example/ HTTP/1.1\r\n"
            b"Host: jobs.example\r\nHost: attacker.example\r\n\r\n"
        )
        with self.assertRaises(BrowserProxyProtocolError):
            parse_browser_proxy_request(malformed)
        with self.assertRaises(BrowserProxyProtocolError):
            parse_browser_proxy_request(b"GET http://jobs.example/ HTTP/1.1\r\nX: " + b"a" * 65536 + b"\r\n\r\n")


class ProtectedBrowserLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = BrowserRuntimeBoundary(
            playwright_version="1.62.0",
            chromium_revision="1234",
            chromium_version="151.0.7922.34",
            chromium_executable="/ms-playwright/chromium-1234/chrome-linux64/chrome",
            wrapper_executable="/usr/local/bin/opportunity-radar-chromium-netns",
            unshare_executable="/usr/bin/unshare",
            unix_socket="/tmp/radar-browser-test/egress.sock",
            proxy_port=BROWSER_PROXY_PORT,
        )

    def test_protected_launch_forces_namespace_proxy_and_scrubs_secrets(self) -> None:
        playwright = Mock()
        underlying = Mock()
        playwright.chromium.launch.return_value = underlying
        environment = {
            "APP_ENV": "production",
            "APP_BROWSER_EGRESS_MODE": BROWSER_EGRESS_MODE,
            "BLUEASH_AUTH_CLIENT_SECRET": "must-not-pass",
            "OPPORTUNITY_RADAR_SECRET_KEY": "must-not-pass",
            "DATABASE_URL": "must-not-pass",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("backend.outbound_security.validate_browser_runtime_boundary", return_value=self.runtime),
            patch("backend.outbound_security._require_browser_proxy_ready"),
            patch("backend.outbound_security._browser_parent_netns_inode", return_value="12345"),
        ):
            browser = launch_playwright_chromium(playwright, headless=True)

        options = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(options["executable_path"], self.runtime.wrapper_executable)
        self.assertTrue(options["chromium_sandbox"])
        self.assertIn(f"--proxy-server=http://127.0.0.1:{BROWSER_PROXY_PORT}", options["args"])
        self.assertIn("--proxy-bypass-list=<-loopback>", options["args"])
        self.assertIn(
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            options["args"],
        )
        self.assertNotIn("proxy", options)
        for secret_name in ("BLUEASH_AUTH_CLIENT_SECRET", "OPPORTUNITY_RADAR_SECRET_KEY", "DATABASE_URL"):
            self.assertNotIn(secret_name, options["env"])
        browser.close()
        underlying.close.assert_called_once_with()

    def test_second_browser_is_rejected_until_first_closes(self) -> None:
        first_playwright = Mock()
        first_playwright.chromium.launch.return_value = Mock()
        second_playwright = Mock()
        environment = {
            "APP_ENV": "production",
            "APP_BROWSER_EGRESS_MODE": BROWSER_EGRESS_MODE,
            # Fail fast instead of waiting out the bounded contention window; the
            # first browser is a mock with no real process tree, so the leaked-
            # lease reclaim path is deliberately not eligible here.
            "OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS": "0",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("backend.outbound_security.validate_browser_runtime_boundary", return_value=self.runtime),
            patch("backend.outbound_security._require_browser_proxy_ready"),
            patch("backend.outbound_security._browser_parent_netns_inode", return_value="12345"),
        ):
            first = launch_playwright_chromium(first_playwright, headless=True)
            with self.assertRaisesRegex(BrowserEgressConfigurationError, "Only one Chromium"):
                launch_playwright_chromium(second_playwright, headless=True)
            first.close()
            second = launch_playwright_chromium(second_playwright, headless=True)
            second.close()

    def test_disconnected_browser_releases_process_lease(self) -> None:
        playwright = Mock()
        underlying = Mock()
        playwright.chromium.launch.return_value = underlying
        environment = {
            "APP_ENV": "production",
            "APP_BROWSER_EGRESS_MODE": BROWSER_EGRESS_MODE,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("backend.outbound_security.validate_browser_runtime_boundary", return_value=self.runtime),
            patch("backend.outbound_security._require_browser_proxy_ready"),
            patch("backend.outbound_security._browser_parent_netns_inode", return_value="12345"),
        ):
            launch_playwright_chromium(playwright, headless=True)
            disconnected = underlying.on.call_args.args[1]
            disconnected(underlying)
            replacement = launch_playwright_chromium(playwright, headless=True)
            replacement.close()

    def test_conflicting_launch_controls_are_rejected(self) -> None:
        environment = {
            "APP_ENV": "production",
            "APP_BROWSER_EGRESS_MODE": BROWSER_EGRESS_MODE,
        }
        with patch.dict(os.environ, environment, clear=True):
            for options in (
                {"proxy": {"server": "http://attacker"}},
                {"executable_path": "/tmp/browser"},
                {"chromium_sandbox": False},
                {"env": {"SECRET": "value"}},
                {"args": ["--no-proxy-server"]},
                {"args": ["--proxy-server=direct://"]},
                {"args": ["--no-sandbox"]},
            ):
                with self.subTest(options=options), self.assertRaises(BrowserEgressConfigurationError):
                    launch_playwright_chromium(Mock(), **options)

    def test_production_without_exact_boundary_mode_fails_closed(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaises(BrowserEgressConfigurationError):
                launch_playwright_chromium(Mock(), headless=True)

    def test_generic_sanitized_environment_contains_no_unrelated_app_values(self) -> None:
        with patch.dict(os.environ, {"PATH": "untrusted", "HOME": "untrusted", "APP_SECRET": "value"}, clear=True):
            environment = _sanitized_process_environment()
        self.assertEqual(environment["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(environment["HOME"], "/tmp")
        self.assertNotIn("APP_SECRET", environment)

    def test_approved_chrome_for_testing_version_string_is_recognized(self) -> None:
        completed = Mock(stdout="Google Chrome for Testing 151.0.7922.34\n")
        with patch("backend.outbound_security.subprocess.run", return_value=completed):
            self.assertEqual(_chromium_binary_version("/approved/chrome"), "151.0.7922.34")


if __name__ == "__main__":
    unittest.main()
