from __future__ import annotations

import errno
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path


BROWSER_PROXY_PORT = 17654
MAX_RELAY_CONNECTIONS = 32
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
_RTF_REJECT = 0x0200


class NetworkNamespaceBoundaryError(RuntimeError):
    """The browser network namespace could not be established safely."""


class _UnixProxyRelay:
    def __init__(self, socket_path: str, port: int) -> None:
        self._socket_path = socket_path
        self._port = port
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._connections = threading.BoundedSemaphore(MAX_RELAY_CONNECTIONS)
        self._threads: set[threading.Thread] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self._port))
        listener.listen(MAX_RELAY_CONNECTIONS)
        listener.settimeout(0.5)
        self._listener = listener
        worker = threading.Thread(target=self._accept, daemon=True)
        worker.start()
        with self._lock:
            self._threads.add(worker)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            workers = tuple(self._threads)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=1.0)

    def _accept(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    client, _ = self._listener.accept()  # type: ignore[union-attr]
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not self._connections.acquire(blocking=False):
                    client.close()
                    continue
                worker = threading.Thread(target=self._relay, args=(client,), daemon=True)
                worker.start()
                with self._lock:
                    self._threads.add(worker)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _relay(self, client: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(10.0)
            upstream.connect(self._socket_path)
            _copy_bidirectionally(client, upstream)
        finally:
            client.close()
            upstream.close()
            self._connections.release()
            with self._lock:
                self._threads.discard(threading.current_thread())


def _inner(browser_args: list[str]) -> int:
    real_executable = _required_environment("OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE")
    unix_socket = _required_environment("OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET")
    parent_netns = _required_environment("OPPORTUNITY_RADAR_BROWSER_PARENT_NETNS")
    _bring_loopback_up()
    _require_isolated_namespace(parent_netns)
    _require_loopback_only_routes()
    relay = _UnixProxyRelay(unix_socket, BROWSER_PROXY_PORT)
    relay.start()
    watchdog_read, watchdog_write = os.pipe()
    os.set_inheritable(watchdog_write, False)
    watchdog = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--watchdog", str(watchdog_read)],
        close_fds=True,
        pass_fds=(watchdog_read,),
        env=dict(os.environ),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(watchdog_read)
    try:
        child = subprocess.Popen(
            [real_executable, *browser_args],
            close_fds=False,
            env=dict(os.environ),
            start_new_session=True,
        )
    except BaseException:
        os.close(watchdog_write)
        watchdog.wait(timeout=2.0)
        relay.stop()
        raise
    try:
        os.write(watchdog_write, f"{child.pid}\n".encode("ascii"))
    except OSError as exc:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2.0)
        os.close(watchdog_write)
        watchdog.wait(timeout=2.0)
        relay.stop()
        raise NetworkNamespaceBoundaryError("Chromium process-group watchdog failed closed.") from exc

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        return child.wait()
    finally:
        os.close(watchdog_write)
        try:
            watchdog.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            watchdog.kill()
            watchdog.wait(timeout=2.0)
        relay.stop()


def _probe() -> int:
    unix_socket = _required_environment("OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET")
    parent_netns = _required_environment("OPPORTUNITY_RADAR_BROWSER_PARENT_NETNS")
    _bring_loopback_up()
    _require_isolated_namespace(parent_netns)
    _require_loopback_only_routes()
    relay = _UnixProxyRelay(unix_socket, BROWSER_PROXY_PORT)
    relay.start()
    try:
        with socket.create_connection(("127.0.0.1", BROWSER_PROXY_PORT), timeout=2.0) as proxy:
            proxy.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
            response = proxy.recv(256)
        if not response.startswith(b"HTTP/1.1 403 "):
            raise NetworkNamespaceBoundaryError("Private destination was not denied by the outer proxy.")
        direct = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        direct.settimeout(1.0)
        try:
            direct.connect(("1.1.1.1", 443))
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.ENETUNREACH, errno.EHOSTUNREACH}:
                raise NetworkNamespaceBoundaryError("Direct browser egress failed with an unexpected result.") from exc
        else:
            raise NetworkNamespaceBoundaryError("Browser namespace allowed direct Internet egress.")
        finally:
            direct.close()
    finally:
        relay.stop()
    return 0


def _bring_loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack("16sh", b"lo", 0)
        response = fcntl.ioctl(control.fileno(), _SIOCGIFFLAGS, request)
        _, flags = struct.unpack("16sh", response)
        fcntl.ioctl(control.fileno(), _SIOCSIFFLAGS, struct.pack("16sh", b"lo", flags | _IFF_UP))


def _require_isolated_namespace(parent_netns: str) -> None:
    current = str(Path("/proc/self/ns/net").stat().st_ino)
    if current == parent_netns:
        raise NetworkNamespaceBoundaryError("Browser process did not enter a distinct network namespace.")
    interfaces = {name for _, name in socket.if_nameindex()}
    if interfaces != {"lo"}:
        raise NetworkNamespaceBoundaryError("Browser namespace contains a non-loopback interface.")


def _require_loopback_only_routes() -> None:
    try:
        ipv4 = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
        ipv6 = Path("/proc/net/ipv6_route").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise NetworkNamespaceBoundaryError("Browser namespace routes could not be inspected.") from exc
    for line in ipv4:
        fields = line.split()
        if not fields:
            continue
        if fields[0] != "lo":
            raise NetworkNamespaceBoundaryError("Browser namespace contains a non-loopback IPv4 route.")
        if len(fields) > 1 and fields[1] == "00000000":
            raise NetworkNamespaceBoundaryError("Browser namespace contains an IPv4 default route.")
    for line in ipv6:
        fields = line.split()
        if not fields:
            continue
        if fields[-1] != "lo":
            raise NetworkNamespaceBoundaryError("Browser namespace contains a non-loopback IPv6 route.")
        if len(fields) > 8 and fields[0] == "0" * 32 and fields[1] == "00":
            # Linux installs an unreachable/reject ::/0 sentinel on loopback
            # even in a fresh network namespace. It is not an egress route.
            try:
                flags = int(fields[8], 16)
            except ValueError as exc:
                raise NetworkNamespaceBoundaryError("Browser namespace contains a malformed IPv6 route.") from exc
            if not flags & _RTF_REJECT:
                raise NetworkNamespaceBoundaryError("Browser namespace contains a usable IPv6 default route.")


def _copy_bidirectionally(left: socket.socket, right: socket.socket) -> None:
    stop = threading.Event()

    def copy(source: socket.socket, target: socket.socket) -> None:
        try:
            while not stop.is_set():
                payload = source.recv(64 * 1024)
                if not payload:
                    break
                target.sendall(payload)
        except OSError:
            pass
        finally:
            stop.set()
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    reverse = threading.Thread(target=copy, args=(right, left), daemon=True)
    reverse.start()
    copy(left, right)
    reverse.join(timeout=1.0)


def _watch_process_group(descriptor: int) -> int:
    payload = bytearray()
    try:
        while b"\n" not in payload and len(payload) < 64:
            chunk = os.read(descriptor, 64 - len(payload))
            if not chunk:
                return 1
            payload.extend(chunk)
        try:
            process_group = int(bytes(payload).split(b"\n", 1)[0])
        except ValueError:
            return 1
        while os.read(descriptor, 4096):
            pass
    finally:
        os.close(descriptor)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return 0


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise NetworkNamespaceBoundaryError(f"Required browser boundary setting {name} is missing.")
    return value


def _main() -> int:
    arguments = sys.argv[1:]
    if len(arguments) == 2 and arguments[0] == "--watchdog":
        return _watch_process_group(int(arguments[1]))
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if arguments == ["--probe"]:
        return _probe()
    return _inner(arguments)


if __name__ == "__main__":
    raise SystemExit(_main())
