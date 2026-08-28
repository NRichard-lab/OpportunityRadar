from __future__ import annotations

import ipaddress
import re
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest
from requests.utils import select_proxy


DEFAULT_MAX_REDIRECTS = 5
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
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


class OutboundSecurityError(ValueError):
    """Base exception for an outbound request rejected by policy."""


class UnsafeOutboundDestination(OutboundSecurityError):
    """The requested URL or one of its resolved addresses is unsafe."""


class OutboundRedirectLimitExceeded(OutboundSecurityError):
    """An outbound request exceeded the configured redirect limit."""


@dataclass(frozen=True)
class ValidatedOutboundDestination:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


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


def launch_playwright_chromium(playwright: Any, **kwargs: Any) -> Any:
    """Launch Chromium without inheriting or auto-detecting a host proxy."""

    launch_options = dict(kwargs)
    arguments = [str(value) for value in launch_options.pop("args", ())]
    if "--no-proxy-server" not in arguments:
        arguments.append("--no-proxy-server")
    return playwright.chromium.launch(args=arguments, **launch_options)


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
