from __future__ import annotations

import socket
import unittest
from dataclasses import dataclass
from unittest.mock import Mock, patch

import requests

from backend.outbound_security import (
    OutboundRedirectLimitExceeded,
    SSRFProtectedSession,
    UnsafeOutboundDestination,
    install_playwright_url_guard,
    launch_playwright_chromium,
    safe_page_goto,
    safe_requests_request,
    validate_outbound_url,
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


def resolving_hosts(records: dict[str, tuple[str, ...]]):
    def resolver(host: str, port: int, family: int, socktype: int, protocol: int):
        return resolving_to(*records[host])(host, port, family, socktype, protocol)

    return resolver


class FailingResolver:
    def __call__(self, *args, **kwargs):
        del args, kwargs
        raise socket.gaierror("mocked DNS failure")


class FakeResponse:
    def __init__(self, status_code: int, location: str = "") -> None:
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.closed = False
        self.history = []

    def close(self) -> None:
        self.closed = True


class FakeSession(requests.Session):
    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@dataclass
class FakeBrowserRequest:
    url: str
    redirected_from: "FakeBrowserRequest | None" = None


class FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted = ""

    def continue_(self) -> None:
        self.continued = True

    def abort(self, error_code: str) -> None:
        self.aborted = error_code


class FakeContext:
    def __init__(self) -> None:
        self.pattern = ""
        self.handler = None
        self.web_socket_pattern = ""
        self.web_socket_handler = None

    def route(self, pattern: str, handler) -> None:
        self.pattern = pattern
        self.handler = handler

    def route_web_socket(self, pattern: str, handler) -> None:
        self.web_socket_pattern = pattern
        self.web_socket_handler = handler


class FakeWebSocketRoute:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed: tuple[int, str] | None = None

    def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


class FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def goto(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return "response"


class OutboundURLValidationTests(unittest.TestCase):
    def test_accepts_http_and_https_when_every_dns_answer_is_public(self) -> None:
        destination = validate_outbound_url(
            "https://jobs.example.com/openings?q=analyst",
            resolver=resolving_to("93.184.216.34", "2606:4700:4700::1111"),
        )

        self.assertEqual(destination.scheme, "https")
        self.assertEqual(destination.hostname, "jobs.example.com")
        self.assertEqual(destination.port, 443)
        self.assertEqual(destination.addresses, ("93.184.216.34", "2606:4700:4700::1111"))

    def test_rejects_non_http_schemes_credentials_and_unsafe_ports(self) -> None:
        resolver = resolving_to("93.184.216.34")
        rejected = (
            "file:///etc/passwd",
            "ftp://example.com/file",
            "https://user:secret@example.com/",
            "http://example.com:22/",
            "https://example.com:/",
        )

        for url in rejected:
            with self.subTest(url=url), self.assertRaises(UnsafeOutboundDestination):
                validate_outbound_url(url, resolver=resolver)

    def test_rejects_ambiguous_or_malformed_hosts(self) -> None:
        resolver = resolving_to("93.184.216.34")
        rejected = (
            "https://exa mple.com/",
            "https://example.com\\@127.0.0.1/",
            "https://127%2e0%2e0%2e1/",
            "https://bad_host.example/",
            "https:///missing-host",
            "https://[::1",
        )

        for url in rejected:
            with self.subTest(url=url), self.assertRaises(UnsafeOutboundDestination):
                validate_outbound_url(url, resolver=resolver)

    def test_rejects_every_non_global_address_class(self) -> None:
        rejected = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "192.0.2.10",
            "100.64.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "::",
            "::ffff:127.0.0.1",
            "::ffff:8.8.8.8",
            "64:ff9b::7f00:1",
            "2002:7f00:1::",
        )

        for address in rejected:
            bracketed = f"[{address}]" if ":" in address else address
            with self.subTest(address=address), self.assertRaises(UnsafeOutboundDestination):
                validate_outbound_url(f"https://{bracketed}/")

    def test_rejects_hostname_if_any_dns_answer_is_not_public(self) -> None:
        with self.assertRaisesRegex(UnsafeOutboundDestination, "not globally routable"):
            validate_outbound_url(
                "https://mixed.example/",
                resolver=resolving_to("93.184.216.34", "127.0.0.1"),
            )

    def test_dns_errors_and_empty_answers_fail_closed(self) -> None:
        with self.assertRaises(UnsafeOutboundDestination):
            validate_outbound_url("https://unresolved.example/", resolver=FailingResolver())
        with self.assertRaises(UnsafeOutboundDestination):
            validate_outbound_url("https://empty.example/", resolver=resolving_to())


class RedirectValidationTests(unittest.TestCase):
    def test_discovery_and_collector_entrypoints_use_protected_sessions(self) -> None:
        from collectors.base import BaseCollector
        from website_tools import make_session

        discovery_session = make_session()
        collector = BaseCollector(delay_seconds=0)
        try:
            self.assertIsInstance(discovery_session, SSRFProtectedSession)
            self.assertIsInstance(collector.session, SSRFProtectedSession)
        finally:
            discovery_session.close()
            collector.session.close()

    def test_requests_helper_revalidates_relative_redirects(self) -> None:
        redirect = FakeResponse(302, "/next")
        final = FakeResponse(200)
        session = FakeSession([redirect, final])

        response = safe_requests_request(
            session,
            "GET",
            "https://public.example/start",
            resolver=resolving_to("93.184.216.34"),
        )

        self.assertIs(response, final)
        self.assertEqual([call[1] for call in session.calls], [
            "https://public.example/start",
            "https://public.example/next",
        ])
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))
        self.assertTrue(redirect.closed)
        self.assertEqual(final.history, [redirect])

    def test_requests_helper_blocks_redirect_to_private_address_before_sending(self) -> None:
        redirect = FakeResponse(302, "http://127.0.0.1/admin")
        session = FakeSession([redirect])

        with self.assertRaises(UnsafeOutboundDestination):
            safe_requests_request(
                session,
                "GET",
                "https://public.example/start",
                resolver=resolving_to("93.184.216.34"),
            )

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(redirect.closed)

    def test_requests_helper_blocks_redirect_when_dns_includes_private_answer(self) -> None:
        redirect = FakeResponse(302, "https://redirected.example/admin")
        session = FakeSession([redirect])
        resolver = resolving_hosts({
            "public.example": ("93.184.216.34",),
            "redirected.example": ("93.184.216.34", "10.10.10.10"),
        })

        with self.assertRaises(UnsafeOutboundDestination):
            safe_requests_request(
                session,
                "GET",
                "https://public.example/start",
                resolver=resolver,
            )

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(redirect.closed)

    def test_requests_helper_caps_redirects(self) -> None:
        first = FakeResponse(302, "/one")
        second = FakeResponse(302, "/two")
        session = FakeSession([first, second])

        with self.assertRaises(OutboundRedirectLimitExceeded):
            safe_requests_request(
                session,
                "GET",
                "https://public.example/start",
                resolver=resolving_to("93.184.216.34"),
                max_redirects=1,
            )

        self.assertEqual(len(session.calls), 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_cross_origin_redirect_drops_auth_and_entity_headers(self) -> None:
        redirect = FakeResponse(303, "https://other.example/final")
        final = FakeResponse(200)
        session = FakeSession([redirect, final])

        safe_requests_request(
            session,
            "POST",
            "https://public.example/start",
            resolver=resolving_to("93.184.216.34"),
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            json={"value": "secret"},
        )

        second_method, _, second_kwargs = session.calls[1]
        self.assertEqual(second_method, "GET")
        self.assertNotIn("Authorization", second_kwargs["headers"])
        self.assertNotIn("Content-Type", second_kwargs["headers"])
        self.assertNotIn("json", second_kwargs)

    def test_cross_origin_redirect_drops_prepared_session_secrets_and_original_params(self) -> None:
        session = SSRFProtectedSession(resolver=resolving_to("93.184.216.34"))
        session.headers.update({
            "Authorization": "Bearer session-secret",
            "X-API-Key": "session-api-secret",
            "X-Safe-Header": "retained",
        })
        session.auth = ("session-user", "session-password")
        session.params = {"session_token": "session-query-secret"}
        session.cookies.set("cross_origin_cookie", "cookie-secret", domain="other.example", path="/")
        adapter = session.get_adapter("https://public.example/")
        prepared: list[requests.PreparedRequest] = []

        def send(request: requests.PreparedRequest, **kwargs):
            del kwargs
            prepared.append(request)
            response = requests.Response()
            response.status_code = 302 if len(prepared) == 1 else 200
            response.url = request.url
            response.request = request
            if response.status_code == 302:
                response.headers["Location"] = "https://other.example/final?redirect_value=kept"
            return response

        with patch.object(adapter, "send", side_effect=send):
            response = session.get(
                "https://public.example/start",
                params={"request_token": "request-query-secret"},
                headers={"Cookie": "request_cookie=request-cookie-secret"},
                allow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(prepared), 2)
        redirected = prepared[1]
        self.assertEqual(redirected.url, "https://other.example/final?redirect_value=kept")
        lowered_headers = {key.lower(): value for key, value in redirected.headers.items()}
        self.assertNotIn("authorization", lowered_headers)
        self.assertNotIn("cookie", lowered_headers)
        self.assertNotIn("x-api-key", lowered_headers)
        self.assertEqual(lowered_headers["x-safe-header"], "retained")
        self.assertNotIn("session-query-secret", redirected.url)
        self.assertNotIn("request-query-secret", redirected.url)
        self.assertEqual(session.auth, ("session-user", "session-password"))
        self.assertEqual(session.headers["Authorization"], "Bearer session-secret")
        self.assertEqual(session.cookies.get("cross_origin_cookie"), "cookie-secret")

    def test_plain_session_reuses_one_pinned_transport(self) -> None:
        session = FakeSession([FakeResponse(200), FakeResponse(200)])
        resolver = resolving_to("93.184.216.34")

        safe_requests_request(session, "GET", "https://public.example/one", resolver=resolver)
        first_adapter = session.get_adapter("https://public.example/")
        safe_requests_request(session, "GET", "https://public.example/two", resolver=resolver)

        self.assertIs(session.get_adapter("http://public.example/"), first_adapter)
        self.assertIs(session.get_adapter("https://public.example/"), first_adapter)
        session.close()

    def test_protected_session_intercepts_normal_get_calls(self) -> None:
        response = FakeResponse(200)
        sender = FakeSession([response])
        session = SSRFProtectedSession(resolver=resolving_to("93.184.216.34"))

        with patch.object(requests.Session, "request", side_effect=sender.request):
            returned = session.get("https://public.example/")

        self.assertIs(returned, response)
        self.assertEqual(sender.calls[0][1], "https://public.example/")
        self.assertFalse(sender.calls[0][2]["allow_redirects"])

    def test_transport_revalidates_and_blocks_dns_rebinding_before_connect(self) -> None:
        answers = iter(("93.184.216.34", "127.0.0.1"))

        def rebinding_resolver(host: str, port: int, family: int, socktype: int, protocol: int):
            return resolving_to(next(answers))(host, port, family, socktype, protocol)

        session = SSRFProtectedSession(resolver=rebinding_resolver)
        adapter = session.get_adapter("https://public.example/")
        with patch.object(adapter.poolmanager, "connection_from_host") as connect:
            with self.assertRaisesRegex(UnsafeOutboundDestination, "not globally routable"):
                session.get("https://public.example/", timeout=0.1)

        connect.assert_not_called()

    def test_transport_connects_to_validated_ip_with_original_host_and_tls_name(self) -> None:
        session = SSRFProtectedSession(resolver=resolving_to("93.184.216.34"))
        adapter = session.get_adapter("https://jobs.example.com/openings")
        prepared = session.prepare_request(requests.Request("GET", "https://jobs.example.com/openings"))
        pool = Mock(name="pool")

        with patch.object(adapter.poolmanager, "connection_from_host", return_value=pool) as connect:
            returned = adapter.get_connection_with_tls_context(prepared, True, proxies={})

        self.assertIs(returned, pool)
        self.assertEqual(connect.call_args.kwargs["scheme"], "https")
        self.assertEqual(connect.call_args.kwargs["host"], "93.184.216.34")
        self.assertEqual(connect.call_args.kwargs["port"], 443)
        self.assertEqual(connect.call_args.kwargs["pool_kwargs"]["assert_hostname"], "jobs.example.com")
        self.assertEqual(connect.call_args.kwargs["pool_kwargs"]["server_hostname"], "jobs.example.com")
        self.assertEqual(prepared.headers["Host"], "jobs.example.com")

    def test_legacy_requests_transport_hook_fails_closed(self) -> None:
        session = SSRFProtectedSession(resolver=resolving_to("93.184.216.34"))
        adapter = session.get_adapter("https://public.example/")

        with self.assertRaisesRegex(UnsafeOutboundDestination, "cannot enforce DNS-pinned"):
            adapter.get_connection("https://public.example/")


class PlaywrightGuardTests(unittest.TestCase):
    def test_chromium_launch_disables_automatic_proxy_use(self) -> None:
        playwright = Mock()
        browser = Mock()
        playwright.chromium.launch.return_value = browser

        returned = launch_playwright_chromium(
            playwright,
            headless=True,
            args=["--disable-background-networking"],
        )

        self.assertIs(returned, browser)
        playwright.chromium.launch.assert_called_once_with(
            args=["--disable-background-networking", "--no-proxy-server"],
            headless=True,
        )

    def test_context_guard_allows_public_requests_and_blocks_private_requests(self) -> None:
        context = FakeContext()
        blocked: list[str] = []
        install_playwright_url_guard(
            context,
            resolver=resolving_to("93.184.216.34"),
            on_blocked=lambda url, exc: blocked.append(f"{url}: {exc}"),
        )

        public_route = FakeRoute()
        context.handler(public_route, FakeBrowserRequest("https://public.example/app.js"))
        private_route = FakeRoute()
        context.handler(private_route, FakeBrowserRequest("http://127.0.0.1/private"))

        self.assertEqual(context.pattern, "**/*")
        self.assertTrue(public_route.continued)
        self.assertEqual(private_route.aborted, "blockedbyclient")
        self.assertEqual(len(blocked), 1)

        web_socket = FakeWebSocketRoute("ws://127.0.0.1/socket")
        context.web_socket_handler(web_socket)
        self.assertEqual(context.web_socket_pattern, "**/*")
        self.assertEqual(web_socket.closed, (1008, "Outbound WebSocket blocked"))
        self.assertEqual(len(blocked), 2)

    def test_context_guard_caps_browser_redirect_chain(self) -> None:
        context = FakeContext()
        install_playwright_url_guard(
            context,
            max_redirects=1,
            resolver=resolving_to("93.184.216.34"),
        )
        first = FakeBrowserRequest("https://public.example/start")
        second = FakeBrowserRequest("https://public.example/one", redirected_from=first)
        third = FakeBrowserRequest("https://public.example/two", redirected_from=second)
        route = FakeRoute()

        context.handler(route, third)

        self.assertEqual(route.aborted, "blockedbyclient")

    def test_safe_page_goto_validates_before_navigation(self) -> None:
        page = FakePage()
        with self.assertRaises(UnsafeOutboundDestination):
            safe_page_goto(page, "http://169.254.169.254/latest/meta-data")
        self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
