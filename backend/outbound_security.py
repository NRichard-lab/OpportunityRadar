from __future__ import annotations

import atexit
import functools
import ipaddress
import importlib.metadata
import json
import logging
import os
import platform
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest
from requests.utils import select_proxy


DEFAULT_MAX_REDIRECTS = 5
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
BROWSER_EGRESS_MODE = "network_namespace_dns_pinned_proxy_v1"
BROWSER_PLAYWRIGHT_VERSION = "1.62.0"
BROWSER_CHROMIUM_REVISION = "1234"
BROWSER_CHROMIUM_VERSION = "151.0.7922.34"
BROWSER_PROXY_PORT = 17654
# A headless Chromium tree run --disable-dev-shm-usage (Playwright's default)
# backs every shared-memory region with a file descriptor, on top of the proxied
# sockets and mojo pipes. The 1024 soft RLIMIT_NOFILE a container gets by default
# is exhausted by heavier career pages: Chromium hits EMFILE and self-aborts
# (SIGTRAP, surfaced as "Page crashed"). Refuse to launch below this floor so a
# mis-sized deployment fails loudly here instead of as intermittent page crashes.
MINIMUM_BROWSER_OPEN_FILE_LIMIT = 8192
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CROSS_ORIGIN_SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    "api-key",
}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

Resolver = Callable[..., list[tuple[Any, ...]]]

logger = logging.getLogger(__name__)


class OutboundSecurityError(ValueError):
    """Base exception for an outbound request rejected by policy."""


class UnsafeOutboundDestination(OutboundSecurityError):
    """The requested URL or one of its resolved addresses is unsafe."""


class OutboundRedirectLimitExceeded(OutboundSecurityError):
    """An outbound request exceeded the configured redirect limit."""


class BrowserEgressConfigurationError(OutboundSecurityError):
    """The independently enforced browser egress boundary is unavailable."""


@dataclass(frozen=True)
class ValidatedOutboundDestination:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class BrowserRuntimeBoundary:
    playwright_version: str
    chromium_revision: str
    chromium_version: str
    chromium_executable: str
    wrapper_executable: str
    unshare_executable: str
    unix_socket: str
    proxy_port: int


def validate_outbound_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
) -> ValidatedOutboundDestination:
    """Validate one HTTP(S) destination and every address returned by DNS."""

    if not isinstance(url, str) or not url:
        raise UnsafeOutboundDestination("Outbound URL must be a non-empty string.")
    if url != url.strip() or any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise UnsafeOutboundDestination("Outbound URL contains whitespace or control characters.")
    # Backslashes are interpreted differently by RFC and WHATWG URL parsers.
    if "\\" in url:
        raise UnsafeOutboundDestination("Outbound URL contains an ambiguous backslash.")

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        explicit_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeOutboundDestination("Outbound URL is malformed.") from exc

    if scheme not in {"http", "https"}:
        raise UnsafeOutboundDestination("Only HTTP and HTTPS outbound URLs are allowed.")
    if not parsed.netloc or not hostname:
        raise UnsafeOutboundDestination("Outbound URL must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundDestination("Outbound URLs must not contain credentials.")
    if parsed.netloc.endswith(":"):
        raise UnsafeOutboundDestination("Outbound URL contains an empty port.")
    if "%" in hostname:
        # Reject percent-encoded hosts and IPv6 zone identifiers.
        raise UnsafeOutboundDestination("Outbound hostname contains an unsafe percent or zone identifier.")

    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    ports = frozenset(int(value) for value in allowed_ports)
    if port not in ports:
        raise UnsafeOutboundDestination(f"Outbound port {port} is not allowed.")

    normalized_host, literal = _normalize_hostname(hostname)
    if literal is not None:
        addresses = (literal,)
    else:
        addresses = _resolve_addresses(normalized_host, port, resolver)

    for address in addresses:
        _require_globally_routable(address)

    ordered = tuple(sorted({str(address) for address in addresses}, key=_address_sort_key))
    return ValidatedOutboundDestination(
        url=url,
        scheme=scheme,
        hostname=normalized_host,
        port=port,
        addresses=ordered,
    )


def _normalize_hostname(hostname: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    candidate = hostname.lower()
    try:
        literal = ipaddress.ip_address(candidate)
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal), literal

    if candidate.endswith("."):
        candidate = candidate[:-1]
    try:
        ascii_host = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeOutboundDestination("Outbound hostname is not valid IDNA.") from exc
    if not ascii_host or len(ascii_host) > 253:
        raise UnsafeOutboundDestination("Outbound hostname has an invalid length.")
    labels = ascii_host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise UnsafeOutboundDestination("Outbound hostname is malformed.")
    return ascii_host, None


def _resolve_addresses(hostname: str, port: int, resolver: Resolver | None) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    lookup = resolver or socket.getaddrinfo
    try:
        answers = lookup(
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    except Exception as exc:
        raise UnsafeOutboundDestination("Outbound hostname could not be resolved safely.") from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        try:
            family = answer[0]
            sockaddr = answer[4]
            address_text = sockaddr[0]
        except (IndexError, TypeError) as exc:
            raise UnsafeOutboundDestination("DNS returned a malformed address record.") from exc
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise UnsafeOutboundDestination("DNS returned an unsupported address family.")
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise UnsafeOutboundDestination("DNS returned a malformed IP address.") from exc
        addresses.append(address)
    if not addresses:
        raise UnsafeOutboundDestination("Outbound hostname did not resolve to an IP address.")
    return tuple(addresses)


def _require_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            raise UnsafeOutboundDestination("IPv4-mapped IPv6 destinations are not allowed.")
        if address.sixtofour is not None or address.teredo is not None:
            raise UnsafeOutboundDestination("IPv4-transition IPv6 destinations are not allowed.")
        if any(address in network for network in _NAT64_NETWORKS):
            raise UnsafeOutboundDestination("IPv4-translation IPv6 destinations are not allowed.")
        if address.is_site_local:
            raise UnsafeOutboundDestination("Site-local destinations are not allowed.")

    unsafe = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )
    if unsafe:
        raise UnsafeOutboundDestination(f"Outbound address {address} is not globally routable.")


def _address_sort_key(value: str) -> tuple[int, int]:
    address = ipaddress.ip_address(value)
    return address.version, int(address)


class SSRFProtectedSession(requests.Session):
    """Requests session that validates destinations and follows redirects itself."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        super().__init__()
        self._outbound_resolver = resolver
        self._outbound_allowed_ports = frozenset(allowed_ports)
        self._outbound_max_redirects = _validate_redirect_limit(max_redirects)
        self._outbound_request_lock = threading.RLock()
        self.trust_env = False
        adapter = _PinnedHTTPAdapter(
            resolver=resolver,
            allowed_ports=self._outbound_allowed_ports,
        )
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        follow_redirects = bool(kwargs.pop("allow_redirects", False))
        sender = super().request
        with self._outbound_request_lock:
            return _request_with_validated_redirects(
                sender,
                method,
                url,
                session=self,
                follow_redirects=follow_redirects,
                redirect_keyword="allow_redirects",
                resolver=self._outbound_resolver,
                allowed_ports=self._outbound_allowed_ports,
                max_redirects=self._outbound_max_redirects,
                kwargs=kwargs,
            )


def safe_requests_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    **kwargs: Any,
) -> requests.Response:
    """Make a validated request with a plain requests session."""

    if isinstance(session, SSRFProtectedSession):
        kwargs["allow_redirects"] = kwargs.pop("allow_redirects", True)
        return session.request(method, url, **kwargs)
    if not isinstance(session, requests.Session):
        raise TypeError("session must be an instance of requests.Session")
    _install_pinned_requests_transport(session, resolver=resolver, allowed_ports=allowed_ports)
    follow_redirects = bool(kwargs.pop("allow_redirects", True))
    request_lock = _requests_session_lock(session)
    with request_lock:
        return _request_with_validated_redirects(
            session.request,
            method,
            url,
            session=session,
            follow_redirects=follow_redirects,
            redirect_keyword="allow_redirects",
            resolver=resolver,
            allowed_ports=allowed_ports,
            max_redirects=max_redirects,
            kwargs=kwargs,
        )


class _PinnedHTTPAdapter(HTTPAdapter):
    """Connect to the exact validated address while preserving Host and TLS identity."""

    def __init__(
        self,
        *,
        resolver: Resolver | None,
        allowed_ports: Iterable[int],
    ) -> None:
        self._resolver = resolver
        self._allowed_ports = frozenset(allowed_ports)
        super().__init__(max_retries=0)

    def get_connection(self, url: str, proxies: dict[str, str] | None = None) -> Any:
        """Fail closed if an older Requests release invokes the legacy transport hook."""

        del url, proxies
        raise UnsafeOutboundDestination(
            "The installed Requests version cannot enforce DNS-pinned outbound connections."
        )

    def get_connection_with_tls_context(
        self,
        request: PreparedRequest,
        verify: Any,
        proxies: dict[str, str] | None = None,
        cert: Any = None,
    ) -> Any:
        if select_proxy(request.url, proxies or {}):
            raise UnsafeOutboundDestination("Outbound proxies are not allowed by the pinned transport policy.")
        destination = validate_outbound_url(
            request.url,
            resolver=self._resolver,
            allowed_ports=self._allowed_ports,
        )
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = destination.addresses[0]
        host_params["port"] = destination.port
        request.headers["Host"] = _host_header(destination)
        if destination.scheme == "https":
            pool_kwargs["assert_hostname"] = destination.hostname
            pool_kwargs["server_hostname"] = destination.hostname
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def _host_header(destination: ValidatedOutboundDestination) -> str:
    hostname = f"[{destination.hostname}]" if ":" in destination.hostname else destination.hostname
    default_port = 443 if destination.scheme == "https" else 80
    return hostname if destination.port == default_port else f"{hostname}:{destination.port}"


def _install_pinned_requests_transport(
    session: requests.Session,
    *,
    resolver: Resolver | None,
    allowed_ports: Iterable[int],
) -> None:
    ports = frozenset(allowed_ports)
    installed = getattr(session, "_outbound_transport_policy", None)
    installed_adapter = getattr(session, "_outbound_transport_adapter", None)
    if installed is not None:
        same_policy = installed[0] is resolver and installed[1] == ports
        still_mounted = (
            session.get_adapter("http://public.example/") is installed_adapter
            and session.get_adapter("https://public.example/") is installed_adapter
        )
        if same_policy and still_mounted:
            session.trust_env = False
            return
        raise UnsafeOutboundDestination(
            "A protected session cannot change or replace its outbound transport policy."
        )

    session.trust_env = False
    old_adapters = {
        session.get_adapter("http://public.example/"),
        session.get_adapter("https://public.example/"),
    }
    adapter = _PinnedHTTPAdapter(resolver=resolver, allowed_ports=ports)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session._outbound_transport_policy = (resolver, ports)  # type: ignore[attr-defined]
    session._outbound_transport_adapter = adapter  # type: ignore[attr-defined]
    for previous in old_adapters:
        if previous is not adapter:
            previous.close()


def _requests_session_lock(session: requests.Session) -> threading.RLock:
    lock = getattr(session, "_outbound_request_lock", None)
    if lock is None:
        lock = threading.RLock()
        session._outbound_request_lock = lock  # type: ignore[attr-defined]
    return lock


def _request_with_validated_redirects(
    sender: Callable[..., Any],
    method: str,
    url: str,
    *,
    session: requests.Session,
    follow_redirects: bool,
    redirect_keyword: str,
    resolver: Resolver | None,
    allowed_ports: Iterable[int],
    max_redirects: int,
    kwargs: Mapping[str, Any],
) -> Any:
    redirect_limit = _validate_redirect_limit(max_redirects)
    current_method = str(method).upper()
    current_url = str(url)
    current_kwargs = dict(kwargs)
    history: list[Any] = []
    session_credentials_allowed = True

    while True:
        validate_outbound_url(current_url, resolver=resolver, allowed_ports=allowed_ports)
        send_kwargs = dict(current_kwargs)
        send_kwargs[redirect_keyword] = False
        response = _send_requests_hop(
            session,
            sender,
            current_method,
            current_url,
            send_kwargs,
            suppress_session_params=bool(history),
            suppress_session_credentials=not session_credentials_allowed,
        )
        status_code = int(getattr(response, "status_code", 0))
        location = _response_location(response)
        if not follow_redirects or status_code not in REDIRECT_STATUSES or not location:
            _set_response_history(response, history)
            return response
        if len(history) >= redirect_limit:
            _close_response(response)
            raise OutboundRedirectLimitExceeded(
                f"Outbound request exceeded {redirect_limit} redirects."
            )

        next_url = urljoin(current_url, location)
        try:
            validate_outbound_url(next_url, resolver=resolver, allowed_ports=allowed_ports)
        except OutboundSecurityError:
            _close_response(response)
            raise
        history.append(response)
        _close_response(response)
        cross_origin = _origin(current_url) != _origin(next_url)
        current_method, current_kwargs = _redirect_method_and_kwargs(
            current_method,
            status_code,
            current_kwargs,
            current_url,
            next_url,
        )
        # Requests applies ``params`` only to the initial request. Reusing the
        # high-level Session API for each manually validated hop must not append
        # the caller's original query values to redirect destinations.
        current_kwargs.pop("params", None)
        if cross_origin:
            session_credentials_allowed = False
        current_url = next_url


def _send_requests_hop(
    session: requests.Session,
    sender: Callable[..., Any],
    method: str,
    url: str,
    kwargs: Mapping[str, Any],
    *,
    suppress_session_params: bool,
    suppress_session_credentials: bool,
) -> Any:
    """Send one hop while preventing Session defaults from crossing origins.

    ``Session.request`` merges session-level auth, headers, cookies, and params
    after request-local values are sanitized. Temporarily substituting safe
    defaults under the per-session request lock prevents that implicit merge.
    """

    if not suppress_session_params and not suppress_session_credentials:
        return sender(method, url, **dict(kwargs))

    original_params = session.params
    original_auth = session.auth
    original_headers = session.headers
    original_cookies = session.cookies
    try:
        if suppress_session_params:
            session.params = {}
        if suppress_session_credentials:
            session.auth = None
            safe_headers = original_headers.copy()
            _remove_headers(safe_headers, _CROSS_ORIGIN_SECRET_HEADERS)
            session.headers = safe_headers
            session.cookies = requests.cookies.RequestsCookieJar()
        return sender(method, url, **dict(kwargs))
    finally:
        session.params = original_params
        session.auth = original_auth
        session.headers = original_headers
        session.cookies = original_cookies


def _response_location(response: Any) -> str:
    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping) and not hasattr(headers, "get"):
        return ""
    value = headers.get("location") or headers.get("Location")
    return str(value) if value is not None else ""


def _redirect_method_and_kwargs(
    method: str,
    status_code: int,
    kwargs: Mapping[str, Any],
    source_url: str,
    target_url: str,
) -> tuple[str, dict[str, Any]]:
    redirected = dict(kwargs)
    next_method = method
    if status_code == 303 and method != "HEAD":
        next_method = "GET"
    elif status_code == 302 and method != "HEAD":
        next_method = "GET"
    elif status_code == 301 and method == "POST":
        next_method = "GET"

    headers = dict(redirected.get("headers") or {})
    if _origin(source_url) != _origin(target_url):
        _remove_headers(headers, _CROSS_ORIGIN_SECRET_HEADERS)
        redirected.pop("auth", None)
        redirected.pop("cookies", None)
    if next_method == "GET" and method != "GET":
        for key in ("data", "json", "files", "content"):
            redirected.pop(key, None)
        _remove_headers(headers, {"content-length", "content-type", "transfer-encoding"})
    if headers or "headers" in redirected:
        redirected["headers"] = headers
    return next_method, redirected


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        port = None
    return parsed.scheme.lower(), parsed.hostname, port


def _remove_headers(headers: dict[str, Any], forbidden: set[str]) -> None:
    for key in list(headers):
        if key.lower() in forbidden:
            headers.pop(key, None)


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _set_response_history(response: Any, history: list[Any]) -> None:
    try:
        response.history = history
    except (AttributeError, TypeError):
        pass


def _validate_redirect_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_redirects must be a non-negative integer.")
    return value


def install_playwright_url_guard(
    context: Any,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    on_blocked: Callable[[str, OutboundSecurityError], None] | None = None,
) -> Callable[[Any, Any], None]:
    """Validate every HTTP request in a Playwright context, including redirects."""

    redirect_limit = _validate_redirect_limit(max_redirects)

    def guard(route: Any, request: Any) -> None:
        request_url = str(getattr(request, "url", ""))
        try:
            if _playwright_redirect_depth(request) > redirect_limit:
                raise OutboundRedirectLimitExceeded(
                    f"Browser request exceeded {redirect_limit} redirects."
                )
            validate_outbound_url(
                request_url,
                resolver=resolver,
                allowed_ports=allowed_ports,
            )
        except OutboundSecurityError as exc:
            if on_blocked is not None:
                on_blocked(request_url, exc)
            route.abort("blockedbyclient")
            return
        route.continue_()

    context.route("**/*", guard)
    route_web_socket = getattr(context, "route_web_socket", None)
    if callable(route_web_socket):
        def block_web_socket(web_socket_route: Any) -> None:
            request_url = str(getattr(web_socket_route, "url", ""))
            error = UnsafeOutboundDestination(
                "Browser WebSocket connections are not allowed by the HTTP(S)-only outbound policy."
            )
            if on_blocked is not None:
                on_blocked(request_url, error)
            web_socket_route.close(code=1008, reason="Outbound WebSocket blocked")

        route_web_socket("**/*", block_web_socket)
    return guard


_browser_launch_lease = threading.Lock()
# Tracks the single browser currently permitted by ``_browser_launch_lease`` so a
# later launch can distinguish a genuinely busy owner from a lease that leaked
# because its Chromium was killed (for example by the OOM killer) before the
# owning thread released it.
_browser_lease_state_lock = threading.Lock()
_current_leased_browser: "_LeasedBrowser | None" = None
_browser_boundary_lock = threading.Lock()
_browser_proxy_process: subprocess.Popen[bytes] | None = None
_browser_boundary_directory: Path | None = None
_PROTECTED_BROWSER_ARGUMENTS = (
    f"--proxy-server=http://127.0.0.1:{BROWSER_PROXY_PORT}",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--metrics-recording-only",
)
_FORBIDDEN_BROWSER_ARGUMENT_PREFIXES = (
    "--proxy-server",
    "--proxy-bypass-list",
    "--proxy-auto-detect",
    "--proxy-pac-url",
    "--no-proxy-server",
    "--host-resolver-rules",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--enable-quic",
    "--force-webrtc-ip-handling-policy",
    "--remote-debugging-address",
    "--remote-debugging-port",
)


def launch_playwright_chromium(playwright: Any, **kwargs: Any) -> Any:
    """Launch Chromium with production isolation or the fail-safe local policy."""

    mode = os.environ.get("APP_BROWSER_EGRESS_MODE", "").strip()
    environment = os.environ.get("APP_ENV", "development").strip().lower()
    if mode == BROWSER_EGRESS_MODE:
        return _launch_protected_playwright_chromium(playwright, kwargs)
    if environment == "production":
        raise BrowserEgressConfigurationError(
            "Production browser launch requires the network-namespace DNS-pinned proxy boundary."
        )

    launch_options = dict(kwargs)
    arguments = [str(value) for value in launch_options.pop("args", ())]
    if "--no-proxy-server" not in arguments:
        arguments.append("--no-proxy-server")
    return playwright.chromium.launch(args=arguments, **launch_options)


def _launch_protected_playwright_chromium(playwright: Any, supplied: Mapping[str, Any]) -> Any:
    launch_options = dict(supplied)
    for option in ("proxy", "executable_path", "chromium_sandbox", "env"):
        if option in launch_options:
            raise BrowserEgressConfigurationError(
                f"Browser launch option {option} is controlled by the production egress boundary."
            )
    arguments = [str(value) for value in launch_options.pop("args", ())]
    for argument in arguments:
        lowered = argument.lower()
        if any(lowered == prefix or lowered.startswith(f"{prefix}=") for prefix in _FORBIDDEN_BROWSER_ARGUMENT_PREFIXES):
            raise BrowserEgressConfigurationError(
                "Browser launch arguments cannot override proxy, namespace, sandbox, or debugging controls."
            )

    runtime = validate_browser_runtime_boundary()
    _require_browser_proxy_ready(runtime)
    _acquire_browser_launch_lease(runtime)
    try:
        launch_options.update({
            "args": [*arguments, *_PROTECTED_BROWSER_ARGUMENTS],
            "chromium_sandbox": True,
            "env": _browser_child_environment(runtime),
            "executable_path": runtime.wrapper_executable,
        })
        browser = playwright.chromium.launch(**launch_options)
    except BaseException:
        _release_browser_launch_lease_locked()
        raise
    leased = _LeasedBrowser(browser, _browser_launch_lease, runtime)
    with _browser_lease_state_lock:
        globals()["_current_leased_browser"] = leased
    return leased


def _positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _browser_lease_wait_seconds() -> float:
    """Seconds a new browser launch waits for a busy lease before failing/reclaiming."""
    return _positive_env_float("OPPORTUNITY_RADAR_BROWSER_LEASE_WAIT_SECONDS", 30.0)


def _browser_chromium_grace_seconds() -> float:
    """Seconds ``close()`` may run before the guard force-kills the Chromium tree."""
    return _positive_env_float("OPPORTUNITY_RADAR_BROWSER_CLOSE_GRACE_SECONDS", 12.0)


def _browser_driver_grace_seconds() -> float:
    """Extra seconds after the Chromium tree is gone before the guard kills the
    wedged Playwright node driver so a blocked ``close()`` can return."""
    return _positive_env_float("OPPORTUNITY_RADAR_BROWSER_DRIVER_GRACE_SECONDS", 6.0)


def _browser_kill_grace_seconds() -> float:
    """Seconds to wait after each escalation signal (SIGTERM, then SIGKILL)."""
    return _positive_env_float("OPPORTUNITY_RADAR_BROWSER_KILL_GRACE_SECONDS", 5.0)


def _release_browser_launch_lease_locked() -> None:
    """Release the launch lease held by the current thread and clear the holder."""
    with _browser_lease_state_lock:
        globals()["_current_leased_browser"] = None
        try:
            _browser_launch_lease.release()
        except RuntimeError:
            pass


def _acquire_browser_launch_lease(runtime: "BrowserRuntimeBoundary") -> None:
    """Take the one-Chromium launch lease.

    Order of preference:
      1. Acquire the lease within the configured bounded wait.
      2. Otherwise reclaim it *only* if the previous browser's recorded process
         tree is provably gone (every recorded ``(pid, start_time)`` is dead) and
         no approved Chromium process is running anywhere -- so admitting a
         browser still honours "one Chromium at a time".
      3. Otherwise fail closed with the historical error.

    A bounded wait alone is intentionally NOT the fix: it is paired with the
    positive process-tree-termination check here and with the close-path guard on
    :class:`_LeasedBrowser` (which also kills a wedged Playwright driver) so a
    hung or OOM-killed browser cannot strand the lease for the worker's life.
    """
    wait_seconds = _browser_lease_wait_seconds()
    if wait_seconds <= 0:
        acquired = _browser_launch_lease.acquire(blocking=False)
    else:
        acquired = _browser_launch_lease.acquire(timeout=wait_seconds)
    if acquired:
        return
    if _try_reclaim_leaked_browser_lease(runtime) and _browser_launch_lease.acquire(
        timeout=max(1.0, wait_seconds)
    ):
        return
    raise BrowserEgressConfigurationError("Only one Chromium process may run at a time.")


def _try_reclaim_leaked_browser_lease(runtime: "BrowserRuntimeBoundary") -> bool:
    """Return True (and free the lease) only if the previous browser is provably gone.

    Never force-releases based on lease age, owning-thread liveness, or
    ``browser.is_connected()``. Reclaim requires: every recorded root process of
    the previous browser is dead *by PID + start-time identity*, AND no approved
    Chromium process exists anywhere in this container.
    """
    with _browser_lease_state_lock:
        holder = globals().get("_current_leased_browser")
        recorded = dict(getattr(holder, "_procs", {})) if holder is not None else {}
    if holder is None or not recorded:
        return False
    if _live_recorded_procs(recorded):
        return False
    if _any_approved_chromium_alive(runtime.chromium_executable):
        return False
    logger.warning(
        "Reclaiming a leaked Chromium launch lease: the previous browser process "
        "tree (%s) is fully terminated (PID + start-time verified) and no approved "
        "Chromium process is running. The one-browser boundary is preserved.",
        sorted(recorded),
    )
    holder._release()
    return True


# ``SIGKILL`` is absent on Windows (dev/test hosts); fall back so attribute access
# never raises. The production browser boundary only runs on Linux, where both are
# real.
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", _SIGTERM)


def _is_proc_filesystem_available() -> bool:
    return platform.system() == "Linux" and os.path.isdir("/proc")


def _iter_proc_pids() -> list[int]:
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return []


def _read_proc_stat(pid: int) -> tuple[int, int, str] | None:
    """Return ``(ppid, starttime_ticks, state)`` from ``/proc/<pid>/stat`` or None.

    ``comm`` (field 2) may itself contain spaces and parentheses, so fields are
    parsed after the final ``)``.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    close_paren = data.rfind(b")")
    if close_paren == -1:
        return None
    rest = data[close_paren + 1 :].split()
    # rest[0] = state; rest[1] = ppid; ... rest[19] = starttime
    if len(rest) < 20:
        return None
    try:
        return int(rest[1]), int(rest[19]), rest[0].decode("ascii", "replace")
    except ValueError:
        return None


def _proc_start_time(pid: int) -> int | None:
    stat = _read_proc_stat(pid)
    return None if stat is None else stat[1]


def _proc_alive(pid: int, start_time: int) -> bool:
    """True iff ``pid`` is a live, same-identity, *non-terminated* process.

    A zombie (state ``Z``) or dead (``X``/``x``) task has terminated -- it just
    has not been reaped by its parent yet -- so it must count as gone, otherwise
    "the process tree terminated" could never become true when orphaned tasks are
    reparented to a container without an ``init`` reaper.
    """
    stat = _read_proc_stat(pid)
    if stat is None:
        return False
    _ppid, current_start, state = stat
    if current_start != start_time:
        return False
    return state not in {"Z", "X", "x"}


def _live_recorded_procs(recorded: Mapping[int, int]) -> set[int]:
    return {pid for pid, start_time in recorded.items() if _proc_alive(pid, start_time)}


def _proc_is_approved_chromium(pid: int, executable_real: str, revision_marker: str) -> bool:
    try:
        if os.path.realpath(f"/proc/{pid}/exe") == executable_real:
            return True
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return (bool(executable_real) and executable_real in cmdline) or (revision_marker in cmdline)


def _descendant_pids(root_pids: set[int]) -> set[int]:
    """Live PIDs whose parent chain reaches one of ``root_pids`` (roots included)."""
    parents: dict[int, int] = {}
    for pid in _iter_proc_pids():
        stat = _read_proc_stat(pid)
        if stat is not None:
            parents[pid] = stat[0]
    result = {pid for pid in root_pids if pid in parents}
    result |= set(root_pids)
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if pid not in result and ppid in result:
                result.add(pid)
                changed = True
    return result


def _capture_browser_procs(chromium_executable: str) -> dict[int, int]:
    """``pid -> start_time`` for the approved-Chromium tree launched by THIS process.

    Restricted to approved-Chromium processes that are descendants of the current
    process, so cleanup can never target an unrelated Chromium. Empty off Linux.
    """
    if not _is_proc_filesystem_available():
        return {}
    try:
        executable_real = os.path.realpath(chromium_executable)
    except OSError:
        executable_real = str(chromium_executable)
    revision_marker = f"chromium-{BROWSER_CHROMIUM_REVISION}"
    my_pid = os.getpid()
    procs: dict[int, int] = {}
    for pid in _descendant_pids({my_pid}):
        if pid == my_pid:
            continue
        if _proc_is_approved_chromium(pid, executable_real, revision_marker):
            start_time = _proc_start_time(pid)
            if start_time is not None:
                procs[pid] = start_time
    return procs


def _find_playwright_driver_proc() -> tuple[int, int] | None:
    """``(pid, start_time)`` of the node Playwright driver child of this process."""
    if not _is_proc_filesystem_available():
        return None
    my_pid = os.getpid()
    for pid in _descendant_pids({my_pid}):
        if pid == my_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
        except OSError:
            continue
        if "playwright" in cmdline and ("cli.js" in cmdline or "run-driver" in cmdline or "/driver/node" in cmdline):
            start_time = _proc_start_time(pid)
            if start_time is not None:
                return pid, start_time
    return None


def _any_approved_chromium_alive(chromium_executable: str) -> bool:
    """Read-only check: is any approved Chromium process running in this container."""
    if not _is_proc_filesystem_available():
        return False
    try:
        executable_real = os.path.realpath(chromium_executable)
    except OSError:
        executable_real = str(chromium_executable)
    revision_marker = f"chromium-{BROWSER_CHROMIUM_REVISION}"
    for pid in _iter_proc_pids():
        if _proc_is_approved_chromium(pid, executable_real, revision_marker):
            return True
    return False


def _signal_pids(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            continue


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _live_tree(recorded: Mapping[int, int]) -> set[int]:
    """PIDs to signal: descendants of still-alive recorded roots, plus the roots.

    Only computed while at least one recorded root is still that exact process,
    so this can never expand to an unrelated process tree.
    """
    live_roots = _live_recorded_procs(recorded)
    if not live_roots:
        return set()
    tree = {pid for pid in _descendant_pids(live_roots) if pid != os.getpid()}
    return tree | live_roots


def _reap_browser_process_tree(recorded: Mapping[int, int]) -> bool:
    """Bounded, identity-checked teardown of the recorded browser tree.

    Returns True iff every recorded root process is confirmed gone. Signal-only,
    safe from any thread. On a host without ``/proc`` it returns False so the
    caller fails closed rather than assuming termination.
    """
    if not recorded:
        return True
    if not _is_proc_filesystem_available():
        return False
    recorded = dict(recorded)

    def roots_gone() -> bool:
        return not _live_recorded_procs(recorded)

    if _wait_until(roots_gone, _browser_kill_grace_seconds()):
        return True
    term_targets = _live_tree(recorded)
    if term_targets:
        logger.warning("Browser tree %s alive after close(); sending SIGTERM.", sorted(term_targets))
        _signal_pids(term_targets, _SIGTERM)
    if _wait_until(roots_gone, _browser_kill_grace_seconds()):
        return True
    kill_targets = _live_tree(recorded)
    if kill_targets:
        logger.warning("Browser tree %s survived SIGTERM; sending SIGKILL.", sorted(kill_targets))
        _signal_pids(kill_targets, _SIGKILL)
    return _wait_until(roots_gone, _browser_kill_grace_seconds())


class _CloseGuard:
    """Bounds ``_LeasedBrowser.close()`` without touching Playwright objects.

    A dedicated thread per close: it only sends OS signals, so it never violates
    Playwright's single-thread ownership rule. Escalation:

      1. If ``close()`` has not returned after ``chromium_grace`` -> SIGKILL the
         recorded Chromium process tree.
      2. If ``close()`` is *still* blocked ``driver_grace`` after that (Chromium
         is gone but the node Playwright driver is wedged) -> SIGKILL the driver
         process, which makes the owning thread's blocked pipe read return.

    ``finish()`` is called by ``close()`` and joins this thread, so the guard is
    always fully wound down before the lease can be released / the next browser
    admitted.
    """

    def __init__(
        self,
        recorded_procs: Mapping[int, int],
        driver_proc: tuple[int, int] | None,
        chromium_grace: float,
        driver_grace: float,
    ) -> None:
        self._procs = dict(recorded_procs)
        self._driver_proc = driver_proc
        self._chromium_grace = chromium_grace
        self._driver_grace = driver_grace
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.killed_chromium = False
        self.killed_driver = False

    def start(self) -> None:
        if not self._procs or not _is_proc_filesystem_available():
            return
        self._thread = threading.Thread(target=self._run, name="browser-close-guard", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if self._done.wait(self._chromium_grace):
            return
        targets = _live_tree(self._procs)
        if targets:
            logger.warning("Close guard: close() overdue; SIGKILL Chromium tree %s.", sorted(targets))
            _signal_pids(targets, _SIGKILL)
            self.killed_chromium = True
        if self._done.wait(self._driver_grace):
            return
        if self._driver_proc is not None:
            driver_pid, driver_start = self._driver_proc
            if _proc_alive(driver_pid, driver_start):
                logger.error(
                    "Close guard: close() still blocked after the Chromium tree exited; "
                    "the Playwright driver (pid=%s) is wedged -- sending SIGKILL to unblock it.",
                    driver_pid,
                )
                _signal_pids({driver_pid}, _SIGKILL)
                self.killed_driver = True

    def finish(self, timeout: float) -> bool:
        self._done.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.1, timeout))
        return not thread.is_alive()


class _LeasedBrowser:
    def __init__(
        self, browser: Any, lease: threading.Lock, runtime: "BrowserRuntimeBoundary | None" = None
    ) -> None:
        self._browser = browser
        self._lease = lease
        self._runtime = runtime
        self._release_lock = threading.Lock()
        self._released = False
        self._chromium_grace = _browser_chromium_grace_seconds()
        self._driver_grace = _browser_driver_grace_seconds()
        # pid -> start_time for the Chromium tree this launch created. Only these
        # are ever signalled, only their identity-verified exit permits reclaim.
        self._procs: dict[int, int] = {}
        self._driver_proc: tuple[int, int] | None = None
        self._identify_process_tree()
        on = getattr(browser, "on", None)
        if callable(on):
            on("disconnected", self._on_disconnected)

    # Back-compat / introspection helpers.
    @property
    def _pids(self) -> set[int]:
        return set(self._procs)

    @property
    def _process_confirmed(self) -> bool:
        return bool(self._procs)

    def _identify_process_tree(self) -> None:
        executable = getattr(self._runtime, "chromium_executable", "") if self._runtime else ""
        if not executable:
            return
        try:
            self._procs = _capture_browser_procs(executable)
            self._driver_proc = _find_playwright_driver_proc()
        except Exception:  # pragma: no cover - defensive
            self._procs = {}

    def _on_disconnected(self, *_args: Any) -> None:
        self._release()

    def _release(self) -> None:
        """Release the lease, but only if THIS browser is still the active holder.

        The ``_released`` flag makes this idempotent; the identity check stops a
        late ``disconnected``/``close`` callback from a superseded browser from
        releasing a lease that a newer browser now owns.
        """
        with self._release_lock:
            if self._released:
                return
            self._released = True
        with _browser_lease_state_lock:
            if globals().get("_current_leased_browser") is self:
                globals()["_current_leased_browser"] = None
                try:
                    self._lease.release()
                except RuntimeError:
                    pass

    def close(self, *args: Any, **kwargs: Any) -> Any:
        guard = _CloseGuard(
            self._procs,
            self._driver_proc,
            self._chromium_grace,
            self._driver_grace,
        )
        guard.start()
        result: Any = None
        close_error: BaseException | None = None
        try:
            result = self._browser.close(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a dead/hung browser is expected here
            close_error = exc
            logger.warning(
                "Leased browser close() raised; proceeding to bounded process reap.",
                exc_info=True,
            )
        finally:
            guard_finished = guard.finish(
                self._chromium_grace
                + self._driver_grace
                + 3 * _browser_kill_grace_seconds()
                + 5.0
            )
            terminated = _reap_browser_process_tree(self._procs)
            if terminated and guard_finished:
                self._release()
            else:
                # Termination not proven -> fail closed: keep the lease held. A
                # later launch reclaims it only once these exact processes die.
                logger.critical(
                    "Browser cleanup could not confirm process-tree termination "
                    "(procs=%s guard_finished=%s close_error=%r). Holding the "
                    "one-Chromium launch lease (fail-closed) until the recorded "
                    "processes are gone.",
                    sorted(self._procs),
                    guard_finished,
                    close_error,
                )
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


def _require_browser_open_file_headroom() -> None:
    """Fail closed if the process file-descriptor ceiling is too low for Chromium.

    A headless Chromium tree opens hundreds of descriptors (one per shared memory
    region under ``--disable-dev-shm-usage``, plus proxied sockets and mojo
    pipes). Below ``MINIMUM_BROWSER_OPEN_FILE_LIMIT`` a heavier page exhausts the
    limit mid-load and Chromium self-aborts, which Playwright reports as
    ``Page crashed``. Surfacing it here turns a mis-sized container into a clear
    startup error instead of an intermittent, load-dependent crash.
    """

    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX only; boundary already requires Linux
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:  # pragma: no cover - getrlimit failure is unexpected
        raise BrowserEgressConfigurationError(
            "The browser runtime could not read its open-file limit."
        ) from exc
    if soft != resource.RLIM_INFINITY and soft < MINIMUM_BROWSER_OPEN_FILE_LIMIT:
        # Try to lift the soft limit toward the hard ceiling before giving up so a
        # host that grants a high hard limit but a low default soft limit still
        # starts. Only the soft limit is adjustable without privilege.
        target = MINIMUM_BROWSER_OPEN_FILE_LIMIT
        if hard != resource.RLIM_INFINITY:
            target = min(target, hard)
        if target > soft:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                soft = target
            except (OSError, ValueError):
                pass
        if soft != resource.RLIM_INFINITY and soft < MINIMUM_BROWSER_OPEN_FILE_LIMIT:
            raise BrowserEgressConfigurationError(
                "The browser runtime needs a soft open-file limit (RLIMIT_NOFILE) of at "
                f"least {MINIMUM_BROWSER_OPEN_FILE_LIMIT}; the current limit is {soft}. "
                "Raise it on the container (compose 'ulimits.nofile') before enabling "
                "browser jobs."
            )


@functools.lru_cache(maxsize=1)
def validate_browser_runtime_boundary() -> BrowserRuntimeBoundary:
    """Fail closed unless the exact browser runtime and netns proxy are ready."""

    if os.environ.get("APP_BROWSER_EGRESS_MODE", "").strip() != BROWSER_EGRESS_MODE:
        raise BrowserEgressConfigurationError(
            f"APP_BROWSER_EGRESS_MODE must be exactly {BROWSER_EGRESS_MODE}."
        )
    if platform.system() != "Linux" or not hasattr(os, "geteuid"):
        raise BrowserEgressConfigurationError("The protected browser boundary requires Linux.")
    if os.geteuid() == 0:
        raise BrowserEgressConfigurationError("The protected browser runtime must run as a non-root user.")
    _require_browser_open_file_headroom()
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BrowserEgressConfigurationError("The pinned Playwright runtime is not installed.") from exc
    if playwright_version != BROWSER_PLAYWRIGHT_VERSION:
        raise BrowserEgressConfigurationError(
            f"Playwright must be pinned to {BROWSER_PLAYWRIGHT_VERSION}."
        )
    revision, browser_version = _installed_playwright_browser_metadata()
    if revision != BROWSER_CHROMIUM_REVISION or browser_version != BROWSER_CHROMIUM_VERSION:
        raise BrowserEgressConfigurationError("Playwright Chromium metadata does not match the approved runtime.")

    chromium_executable = _playwright_chromium_executable()
    configured_chromium = os.environ.get("OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE", "").strip()
    if configured_chromium and Path(configured_chromium).resolve() != Path(chromium_executable).resolve():
        raise BrowserEgressConfigurationError("Configured Chromium executable does not match Playwright.")
    try:
        chromium_metadata = Path(chromium_executable).stat()
    except OSError as exc:
        raise BrowserEgressConfigurationError("The approved Chromium executable is unavailable.") from exc
    if (
        not Path(chromium_executable).is_file()
        or not os.access(chromium_executable, os.X_OK)
        or chromium_metadata.st_uid != 0
        or stat.S_IMODE(chromium_metadata.st_mode) & 0o022
    ):
        raise BrowserEgressConfigurationError("The approved Chromium executable is missing or not executable.")
    if f"chromium-{BROWSER_CHROMIUM_REVISION}" not in Path(chromium_executable).as_posix():
        raise BrowserEgressConfigurationError("Chromium executable does not belong to the approved revision.")
    chromium_version = _chromium_binary_version(chromium_executable)
    if chromium_version != BROWSER_CHROMIUM_VERSION:
        raise BrowserEgressConfigurationError("Chromium binary version does not match the approved runtime.")

    unshare = "/usr/bin/unshare"
    try:
        unshare_metadata = Path(unshare).stat()
    except OSError as exc:
        raise BrowserEgressConfigurationError("The unshare executable required for network isolation is unavailable.") from exc
    if (
        not Path(unshare).is_file()
        or not os.access(unshare, os.X_OK)
        or unshare_metadata.st_uid != 0
        or stat.S_IMODE(unshare_metadata.st_mode) & 0o022
    ):
        raise BrowserEgressConfigurationError("The unshare executable required for network isolation is unavailable.")

    wrapper = _validated_browser_wrapper(unshare)
    with _browser_boundary_lock:
        directory, unix_socket = _ensure_browser_boundary_files()
        _ensure_browser_proxy_process(directory, unix_socket)
        _probe_browser_network_namespace(wrapper, unix_socket, chromium_executable)
    return BrowserRuntimeBoundary(
        playwright_version=playwright_version,
        chromium_revision=revision,
        chromium_version=chromium_version,
        chromium_executable=chromium_executable,
        wrapper_executable=str(wrapper),
        unshare_executable=unshare,
        unix_socket=str(unix_socket),
        proxy_port=BROWSER_PROXY_PORT,
    )


def _installed_playwright_browser_metadata() -> tuple[str, str]:
    try:
        import playwright

        manifest = Path(playwright.__file__).resolve().parent / "driver" / "package" / "browsers.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        entry = next(item for item in document["browsers"] if item.get("name") == "chromium")
        return str(entry["revision"]), str(entry["browserVersion"])
    except (ImportError, OSError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise BrowserEgressConfigurationError("Playwright Chromium metadata could not be verified.") from exc


def _playwright_chromium_executable() -> str:
    script = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); "
        "print(p.chromium.executable_path); "
        "p.stop()"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=_sanitized_process_environment(include_playwright_path=True),
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserEgressConfigurationError("Playwright could not locate its Chromium executable.") from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BrowserEgressConfigurationError("Playwright returned an ambiguous Chromium executable path.")
    return lines[0]


def _chromium_binary_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            env=_sanitized_process_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserEgressConfigurationError("Chromium binary version could not be verified.") from exc
    match = re.search(
        r"(?:Chromium|Google Chrome for Testing|Chrome)\s+([0-9]+(?:\.[0-9]+){3})",
        result.stdout,
    )
    if match is None:
        raise BrowserEgressConfigurationError("Chromium returned an unrecognized version string.")
    return match.group(1)


def _validated_browser_wrapper(unshare: str) -> Path:
    configured = os.environ.get("OPPORTUNITY_RADAR_CHROMIUM_WRAPPER", "").strip()
    if not configured:
        raise BrowserEgressConfigurationError("The immutable Chromium namespace wrapper is not configured.")
    wrapper = Path(configured)
    try:
        metadata = wrapper.stat()
    except OSError as exc:
        raise BrowserEgressConfigurationError("The Chromium namespace wrapper is unavailable.") from exc
    if (
        not wrapper.is_file()
        or not os.access(wrapper, os.X_OK)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o222
    ):
        raise BrowserEgressConfigurationError("Chromium namespace wrapper ownership or permissions are unsafe.")
    try:
        wrapper_text = wrapper.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrowserEgressConfigurationError("Chromium namespace wrapper could not be verified.") from exc
    required_fragments = (
        str(Path(unshare)),
        "--user",
        "--map-current-user",
        "--keep-caps",
        "--net",
        "--fork",
        "--kill-child=SIGKILL",
        "backend.browser_netns_runner",
    )
    if any(fragment not in wrapper_text for fragment in required_fragments):
        raise BrowserEgressConfigurationError("Chromium namespace wrapper is missing an isolation control.")
    return wrapper


def _ensure_browser_boundary_files() -> tuple[Path, Path]:
    global _browser_boundary_directory
    if _browser_boundary_directory is None:
        _browser_boundary_directory = Path(tempfile.mkdtemp(prefix="radar-browser-egress-"))
    directory = _browser_boundary_directory
    metadata = directory.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise BrowserEgressConfigurationError("Browser boundary directory ownership or permissions are unsafe.")
    unix_socket = directory / "egress.sock"
    return directory, unix_socket


def _ensure_browser_proxy_process(directory: Path, unix_socket: Path) -> None:
    global _browser_proxy_process
    if unix_socket.parent != directory:
        raise BrowserEgressConfigurationError("Browser proxy socket escaped its protected runtime directory.")
    if _browser_proxy_process is not None and _browser_proxy_process.poll() is None:
        _require_owner_only_unix_socket(unix_socket)
        return
    command = [
        sys.executable,
        "-m",
        "backend.browser_egress_proxy",
        "--socket",
        str(unix_socket),
        "--parent-pid",
        str(os.getpid()),
    ]
    try:
        _browser_proxy_process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=_sanitized_process_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise BrowserEgressConfigurationError("Browser egress proxy process could not start.") from exc
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _browser_proxy_process.poll() is not None:
            break
        try:
            _require_owner_only_unix_socket(unix_socket)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as readiness:
                readiness.settimeout(0.2)
                readiness.connect(str(unix_socket))
            return
        except BrowserEgressConfigurationError:
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    _stop_browser_proxy_process()
    raise BrowserEgressConfigurationError("Browser egress proxy did not create its protected Unix socket.")


def _require_owner_only_unix_socket(unix_socket: Path) -> None:
    try:
        metadata = unix_socket.lstat()
    except FileNotFoundError as exc:
        raise BrowserEgressConfigurationError("Browser egress proxy socket is unavailable.") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BrowserEgressConfigurationError("Browser egress proxy socket ownership or permissions are unsafe.")


def _require_browser_proxy_ready(runtime: BrowserRuntimeBoundary) -> None:
    unix_socket = Path(runtime.unix_socket)
    with _browser_boundary_lock:
        _ensure_browser_proxy_process(unix_socket.parent, unix_socket)
        _require_owner_only_unix_socket(unix_socket)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(2.0)
                probe.connect(str(unix_socket))
                probe.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
                response = probe.recv(256)
        except OSError as exc:
            raise BrowserEgressConfigurationError("Browser egress proxy health check failed.") from exc
        if not response.startswith(b"HTTP/1.1 403 "):
            raise BrowserEgressConfigurationError("Browser egress proxy did not enforce private-address denial.")


def _probe_browser_network_namespace(
    wrapper: Path,
    unix_socket: Path,
    chromium_executable: str,
) -> None:
    environment = _browser_boundary_environment(
        unix_socket=str(unix_socket),
        real_executable=chromium_executable,
    )
    try:
        result = subprocess.run(
            [str(wrapper), "--probe"],
            env=environment,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserEgressConfigurationError("Browser network-namespace probe could not run.") from exc
    if result.returncode != 0:
        raise BrowserEgressConfigurationError("Browser network-namespace isolation probe failed closed.")


def _browser_child_environment(runtime: BrowserRuntimeBoundary) -> dict[str, str]:
    return _browser_boundary_environment(
        unix_socket=runtime.unix_socket,
        real_executable=runtime.chromium_executable,
    )


def _browser_boundary_environment(
    *,
    unix_socket: str,
    real_executable: str,
) -> dict[str, str]:
    environment = _sanitized_process_environment()
    environment.update({
        "OPPORTUNITY_RADAR_BROWSER_PARENT_NETNS": _browser_parent_netns_inode(),
        "OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET": unix_socket,
        "OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE": real_executable,
    })
    return environment


def _browser_parent_netns_inode() -> str:
    try:
        return str(Path("/proc/self/ns/net").stat().st_ino)
    except OSError as exc:
        raise BrowserEgressConfigurationError("Parent network namespace identity is unavailable.") from exc


def _sanitized_process_environment(*, include_playwright_path: bool = False) -> dict[str, str]:
    environment: dict[str, str] = {
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/.cache",
    }
    if include_playwright_path and os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        environment["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    return environment


def _stop_browser_proxy_process() -> None:
    global _browser_proxy_process
    process = _browser_proxy_process
    _browser_proxy_process = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


atexit.register(_stop_browser_proxy_process)


def safe_page_goto(
    page: Any,
    url: str,
    *,
    resolver: Resolver | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    **kwargs: Any,
) -> Any:
    """Validate an initial browser navigation before calling page.goto."""

    validate_outbound_url(url, resolver=resolver, allowed_ports=allowed_ports)
    return page.goto(url, **kwargs)


def _playwright_redirect_depth(request: Any) -> int:
    depth = 0
    current = getattr(request, "redirected_from", None)
    seen: set[int] = set()
    while current is not None:
        identity = id(current)
        if identity in seen:
            raise OutboundRedirectLimitExceeded("Browser redirect chain contains a cycle.")
        seen.add(identity)
        depth += 1
        current = getattr(current, "redirected_from", None)
    return depth
