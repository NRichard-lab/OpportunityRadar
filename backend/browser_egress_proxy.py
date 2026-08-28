from __future__ import annotations

import argparse
import ipaddress
import os
import re
import selectors
import signal
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from backend.outbound_security import (
    DEFAULT_ALLOWED_PORTS,
    Resolver,
    UnsafeOutboundDestination,
    ValidatedOutboundDestination,
    validate_outbound_url,
)


MAX_PROXY_HEADER_BYTES = 64 * 1024
MAX_PROXY_CONNECTIONS = 32
PROXY_HEADER_TIMEOUT_SECONDS = 10.0
PROXY_CONNECT_TIMEOUT_SECONDS = 10.0
PROXY_IDLE_TIMEOUT_SECONDS = 30.0
_HTTP_TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ALLOWED_METHODS = frozenset({
    "CONNECT",
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
})


class BrowserProxyProtocolError(ValueError):
    """A browser proxy client sent a malformed or unsupported request."""


@dataclass(frozen=True)
class BrowserProxyRequest:
    method: str
    version: str
    destination_url: str
    upstream_target: str
    headers: tuple[tuple[bytes, bytes], ...]
    buffered_body: bytes


def parse_browser_proxy_request(header_block: bytes, buffered_body: bytes = b"") -> BrowserProxyRequest:
    """Parse one bounded HTTP proxy request without retaining destination data."""

    if len(header_block) > MAX_PROXY_HEADER_BYTES:
        raise BrowserProxyProtocolError("Proxy request headers exceed the allowed size.")
    if not header_block.endswith(b"\r\n\r\n"):
        raise BrowserProxyProtocolError("Proxy request headers are incomplete.")
    lines = header_block[:-4].split(b"\r\n")
    if not lines or not lines[0]:
        raise BrowserProxyProtocolError("Proxy request line is missing.")
    try:
        method_raw, target_raw, version_raw = lines[0].split(b" ")
        method = method_raw.decode("ascii").upper()
        target = target_raw.decode("ascii")
        version = version_raw.decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise BrowserProxyProtocolError("Proxy request line is malformed.") from exc
    if method not in _ALLOWED_METHODS or method_raw.decode("ascii") != method:
        raise BrowserProxyProtocolError("Proxy request method is not allowed.")
    if version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise BrowserProxyProtocolError("Proxy HTTP version is not supported.")
    if any(ord(character) <= 32 or ord(character) == 127 for character in target):
        raise BrowserProxyProtocolError("Proxy request target contains unsafe characters.")

    headers: list[tuple[bytes, bytes]] = []
    host_headers = 0
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise BrowserProxyProtocolError("Proxy request contains malformed headers.")
        name, value = line.split(b":", 1)
        if not _HTTP_TOKEN.fullmatch(name):
            raise BrowserProxyProtocolError("Proxy request contains an invalid header name.")
        if b"\x00" in value or b"\r" in value or b"\n" in value:
            raise BrowserProxyProtocolError("Proxy request contains an invalid header value.")
        lowered = name.lower()
        if lowered == b"host":
            host_headers += 1
        if lowered not in {b"proxy-authorization", b"proxy-connection", b"connection"}:
            headers.append((name, value.strip(b" \t")))
    if host_headers > 1:
        raise BrowserProxyProtocolError("Proxy request contains duplicate Host headers.")

    if method == "CONNECT":
        if any(separator in target for separator in ("/", "?", "#", "@")):
            raise BrowserProxyProtocolError("CONNECT authority is malformed.")
        try:
            authority = urlsplit(f"//{target}")
            if not authority.hostname or authority.port is None:
                raise ValueError
        except ValueError as exc:
            raise BrowserProxyProtocolError("CONNECT authority must include a valid port.") from exc
        destination_url = f"https://{target}/"
        upstream_target = target
    else:
        try:
            parsed = urlsplit(target)
        except ValueError as exc:
            raise BrowserProxyProtocolError("Absolute proxy request target is malformed.") from exc
        if parsed.scheme.lower() != "http" or not parsed.netloc or parsed.fragment:
            raise BrowserProxyProtocolError("Plain proxy requests require an absolute HTTP target.")
        destination_url = target
        upstream_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    return BrowserProxyRequest(
        method=method,
        version=version,
        destination_url=destination_url,
        upstream_target=upstream_target,
        headers=tuple(headers),
        buffered_body=buffered_body,
    )


def authorize_browser_proxy_request(
    request: BrowserProxyRequest,
    *,
    resolver: Resolver | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
) -> ValidatedOutboundDestination:
    """Apply the DNS-pinned public-address policy independently of Playwright."""

    effective_ports = frozenset(int(value) for value in allowed_ports)
    if request.method == "CONNECT":
        # CONNECT is an opaque byte tunnel, so restrict it to the TLS port. HTTP
        # on port 80 remains available only through parsed absolute-form proxy
        # requests where the destination and Host header stay under our control.
        effective_ports &= {443}
    return validate_outbound_url(
        request.destination_url,
        resolver=resolver,
        allowed_ports=effective_ports,
    )


def connect_pinned_destination(
    destination: ValidatedOutboundDestination,
    *,
    timeout: float = PROXY_CONNECT_TIMEOUT_SECONDS,
) -> socket.socket:
    """Connect to a validated numeric address without performing another lookup."""

    address = ipaddress.ip_address(destination.addresses[0])
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    upstream = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    upstream.settimeout(timeout)
    sockaddr = (str(address), destination.port, 0, 0) if family == socket.AF_INET6 else (str(address), destination.port)
    try:
        upstream.connect(sockaddr)
    except BaseException:
        upstream.close()
        raise
    return upstream


class BrowserEgressProxyServer:
    """Owner-only Unix-socket HTTP proxy with DNS-pinned public egress."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        resolver: Resolver | None = None,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
        max_connections: int = MAX_PROXY_CONNECTIONS,
        parent_pid: int | None = None,
    ) -> None:
        if isinstance(max_connections, bool) or not isinstance(max_connections, int) or max_connections < 1:
            raise ValueError("max_connections must be a positive integer.")
        self.socket_path = Path(socket_path)
        self._resolver = resolver
        self._allowed_ports = frozenset(int(value) for value in allowed_ports)
        self._parent_pid = parent_pid
        self._connections = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: set[threading.Thread] = set()
        self._thread_lock = threading.Lock()

    def serve_forever(self) -> None:
        self._prepare_socket_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)
            listener.listen(MAX_PROXY_CONNECTIONS)
            listener.settimeout(0.5)
            while not self._stop.is_set():
                if self._parent_pid is not None and os.getppid() != self._parent_pid:
                    break
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not self._connections.acquire(blocking=False):
                    self._send_error(client, 503)
                    client.close()
                    continue
                worker = threading.Thread(target=self._handle_client_and_release, args=(client,), daemon=True)
                with self._thread_lock:
                    self._threads.add(worker)
                worker.start()
        finally:
            self.stop()
            with self._thread_lock:
                workers = tuple(self._threads)
            for worker in workers:
                worker.join(timeout=1.0)
            self._remove_socket()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _prepare_socket_path(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            metadata = self.socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise PermissionError("Refusing to replace an untrusted browser proxy path.")
            self.socket_path.unlink()

    def _remove_socket(self) -> None:
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
            self.socket_path.unlink()

    def _handle_client_and_release(self, client: socket.socket) -> None:
        thread = threading.current_thread()
        try:
            self._handle_client(client)
        finally:
            client.close()
            self._connections.release()
            with self._thread_lock:
                self._threads.discard(thread)

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            header, buffered = _receive_proxy_header(client)
            request = parse_browser_proxy_request(header, buffered)
            destination = authorize_browser_proxy_request(
                request,
                resolver=self._resolver,
                allowed_ports=self._allowed_ports,
            )
            upstream = connect_pinned_destination(destination)
            if request.method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if request.buffered_body:
                    upstream.sendall(request.buffered_body)
            else:
                upstream.sendall(_origin_form_request(request, destination))
                if request.buffered_body:
                    upstream.sendall(request.buffered_body)
            _pump_bidirectionally(client, upstream)
        except (BrowserProxyProtocolError, UnsafeOutboundDestination):
            self._send_error(client, 403)
        except (OSError, TimeoutError):
            self._send_error(client, 502)
        finally:
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _send_error(client: socket.socket, status: int) -> None:
        phrase = {403: b"Forbidden", 502: b"Bad Gateway", 503: b"Service Unavailable"}.get(status, b"Error")
        try:
            client.sendall(
                b"HTTP/1.1 " + str(status).encode("ascii") + b" " + phrase
                + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
        except OSError:
            pass


def _receive_proxy_header(client: socket.socket) -> tuple[bytes, bytes]:
    client.settimeout(PROXY_HEADER_TIMEOUT_SECONDS)
    received = bytearray()
    while True:
        marker = received.find(b"\r\n\r\n")
        if marker >= 0:
            boundary = marker + 4
            return bytes(received[:boundary]), bytes(received[boundary:])
        if len(received) >= MAX_PROXY_HEADER_BYTES:
            raise BrowserProxyProtocolError("Proxy request headers exceed the allowed size.")
        chunk = client.recv(min(8192, MAX_PROXY_HEADER_BYTES + 1 - len(received)))
        if not chunk:
            raise BrowserProxyProtocolError("Proxy client disconnected before sending headers.")
        received.extend(chunk)


def _origin_form_request(
    request: BrowserProxyRequest,
    destination: ValidatedOutboundDestination,
) -> bytes:
    hostname = f"[{destination.hostname}]" if ":" in destination.hostname else destination.hostname
    default_port = 443 if destination.scheme == "https" else 80
    host = hostname if destination.port == default_port else f"{hostname}:{destination.port}"
    filtered = [(name, value) for name, value in request.headers if name.lower() != b"host"]
    lines = [f"{request.method} {request.upstream_target} {request.version}".encode("ascii")]
    lines.append(b"Host: " + host.encode("ascii"))
    lines.extend(name + b": " + value for name, value in filtered)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


def _pump_bidirectionally(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    sockets = (left, right)
    for stream in sockets:
        stream.setblocking(False)
        selector.register(stream, selectors.EVENT_READ)
    last_activity = time.monotonic()
    open_readers = set(sockets)
    try:
        while open_readers:
            remaining = PROXY_IDLE_TIMEOUT_SECONDS - (time.monotonic() - last_activity)
            if remaining <= 0:
                break
            events = selector.select(timeout=remaining)
            if not events:
                break
            for key, _ in events:
                source = key.fileobj
                target = right if source is left else left
                try:
                    payload = source.recv(64 * 1024)
                except (BlockingIOError, InterruptedError):
                    continue
                if not payload:
                    selector.unregister(source)
                    open_readers.discard(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                target.setblocking(True)
                target.settimeout(PROXY_IDLE_TIMEOUT_SECONDS)
                target.sendall(payload)
                target.setblocking(False)
                last_activity = time.monotonic()
    finally:
        selector.close()


def _main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    options = parser.parse_args()
    server = BrowserEgressProxyServer(options.socket, parent_pid=options.parent_pid)

    def stop_server(_signum: int, _frame: object) -> None:
        server.stop()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
