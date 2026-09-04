"""Route settlement must be total, idempotent, fail-closed, and silent.

Regression cover for the 2026-09-04 production incident. A route handler runs
inside Playwright's dispatcher: anything it raises leaves the intercepted request
unsettled (the page load then hangs to its timeout) and resurfaces later on an
unrelated call. Route commands issued against a crashed renderer or a closed
page/context are also expected to fail, and must be absorbed rather than
propagated.
"""

from __future__ import annotations

import socket
import unittest

from backend.outbound_security import _settle_route, install_playwright_url_guard


def resolving_to(*addresses: str):
    """Same resolver shape the outbound-security suite uses."""

    def resolver(host: str, port: int, family: int, socktype: int, protocol: int):
        del host, family, socktype, protocol
        answers = []
        for address in addresses:
            answer_family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if answer_family == socket.AF_INET6 else (address, port)
            answers.append((answer_family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return answers

    return resolver


class FakeRoute:
    """Minimal stand-in for playwright.sync_api.Route."""

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._fail_with = fail_with

    def continue_(self, *args, **kwargs) -> None:
        self.calls.append(("continue", args, kwargs))
        if self._fail_with is not None:
            raise self._fail_with

    def abort(self, *args, **kwargs) -> None:
        self.calls.append(("abort", args, kwargs))
        if self._fail_with is not None:
            raise self._fail_with


class FakeRequest:
    def __init__(self, url: str, redirected_from=None) -> None:
        self.url = url
        self.redirected_from = redirected_from


class FakeContext:
    def __init__(self) -> None:
        self.handler = None
        self.ws_handler = None

    def route(self, _pattern, handler) -> None:
        self.handler = handler

    def route_web_socket(self, _pattern, handler) -> None:
        self.ws_handler = handler


class TargetClosed(Exception):
    """Stands in for Playwright's target-closed family."""


class SettleRouteTests(unittest.TestCase):
    def test_allow_continues_once(self) -> None:
        route = FakeRoute()
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "continued")
        self.assertEqual([call[0] for call in route.calls], ["continue"])

    def test_deny_aborts_as_blocked_by_client(self) -> None:
        route = FakeRoute()
        self.assertEqual(_settle_route(route, allow=False, request_url="https://e.test/"), "aborted")
        self.assertEqual(route.calls, [("abort", ("blockedbyclient",), {})])

    def test_second_settlement_is_a_no_op(self) -> None:
        """A duplicate action on the same route must never reach Playwright."""
        route = FakeRoute()
        _settle_route(route, allow=True, request_url="https://e.test/")
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "duplicate")
        self.assertEqual(_settle_route(route, allow=False, request_url="https://e.test/"), "duplicate")
        self.assertEqual(len(route.calls), 1)

    def test_stale_route_after_page_crash_is_absorbed(self) -> None:
        route = FakeRoute(fail_with=TargetClosed("Target page, context or browser has been closed"))
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "unsettled")

    def test_stale_route_after_page_close_is_absorbed(self) -> None:
        route = FakeRoute(fail_with=TargetClosed("Page closed"))
        self.assertEqual(_settle_route(route, allow=False, request_url="https://e.test/"), "unsettled")

    def test_stale_route_after_context_close_is_absorbed(self) -> None:
        route = FakeRoute(fail_with=TargetClosed("BrowserContext closed"))
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "unsettled")

    def test_invalid_interception_id_is_absorbed(self) -> None:
        """The exact driver-side error from the production incident."""
        route = FakeRoute(fail_with=Exception("Invalid InterceptionId."))
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "unsettled")

    def test_base_exception_from_route_is_absorbed(self) -> None:
        """Even a non-Exception failure must not escape into the dispatcher."""

        class Abandoned(BaseException):
            pass

        route = FakeRoute(fail_with=Abandoned("teardown"))
        self.assertEqual(_settle_route(route, allow=True, request_url="https://e.test/"), "unsettled")


class GuardTests(unittest.TestCase):
    def _guard(self, context: FakeContext, **kwargs):
        install_playwright_url_guard(context, **kwargs)
        return context.handler

    def test_safe_url_is_continued(self) -> None:
        context = FakeContext()
        guard = self._guard(context, resolver=resolving_to("93.184.216.34"))
        route = FakeRoute()
        guard(route, FakeRequest("https://example.test/jobs"))
        self.assertEqual([call[0] for call in route.calls], ["continue"])

    def test_blocked_url_is_aborted_and_reported(self) -> None:
        context = FakeContext()
        seen: list[str] = []
        guard = self._guard(
            context,
            resolver=resolving_to("127.0.0.1"),
            on_blocked=lambda url, exc: seen.append(url),
        )
        route = FakeRoute()
        guard(route, FakeRequest("https://internal.test/"))
        self.assertEqual([call[0] for call in route.calls], ["abort"])
        self.assertEqual(seen, ["https://internal.test/"])

    def test_handler_never_raises_when_the_route_is_dead(self) -> None:
        """The whole point: nothing escapes into Playwright's dispatcher."""
        context = FakeContext()
        guard = self._guard(context, resolver=resolving_to("93.184.216.34"))
        route = FakeRoute(fail_with=TargetClosed("Page crashed"))
        guard(route, FakeRequest("https://example.test/"))  # must not raise

    def test_unexpected_validator_failure_fails_closed(self) -> None:
        """An unverified request is never allowed, whatever went wrong."""
        context = FakeContext()

        def exploding_resolver(host, port, family, socktype, protocol):
            raise MemoryError("resolver blew up")

        guard = self._guard(context, resolver=exploding_resolver)
        route = FakeRoute()
        guard(route, FakeRequest("https://example.test/"))
        self.assertEqual([call[0] for call in route.calls], ["abort"])

    def test_reporting_callback_failure_still_settles_the_route(self) -> None:
        context = FakeContext()

        def bad_callback(url, exc):
            raise RuntimeError("reporting is broken")

        guard = self._guard(
            context,
            resolver=resolving_to("127.0.0.1"),
            on_blocked=bad_callback,
        )
        route = FakeRoute()
        guard(route, FakeRequest("https://internal.test/"))
        self.assertEqual([call[0] for call in route.calls], ["abort"])

    def test_websocket_block_absorbs_a_dead_target(self) -> None:
        context = FakeContext()
        install_playwright_url_guard(context, resolver=resolving_to("93.184.216.34"))

        class DeadWebSocketRoute:
            url = "wss://example.test/live"

            def close(self, **_kwargs):
                raise TargetClosed("Target page, context or browser has been closed")

        context.ws_handler(DeadWebSocketRoute())  # must not raise


if __name__ == "__main__":
    unittest.main()
