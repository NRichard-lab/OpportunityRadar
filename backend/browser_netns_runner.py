from __future__ import annotations

import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
from pathlib import Path


BROWSER_PROXY_PORT = 17654
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
_RTF_REJECT = 0x0200


class NetworkNamespaceBoundaryError(RuntimeError):
    """The browser network namespace could not be established safely."""


def _inner(browser_args: list[str]) -> int:
    real_executable = _required_environment("OPPORTUNITY_RADAR_CHROMIUM_EXECUTABLE")
    connect_shim = _required_environment("OPPORTUNITY_RADAR_BROWSER_CONNECT_SHIM")
    _required_environment("OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET")
    parent_netns = _required_environment("OPPORTUNITY_RADAR_BROWSER_PARENT_NETNS")
    _bring_loopback_up()
    _require_isolated_namespace(parent_netns)
    _require_loopback_only_routes()
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
        browser_environment = dict(os.environ)
        browser_environment["LD_PRELOAD"] = connect_shim
        child = subprocess.Popen(
            [real_executable, *browser_args],
            close_fds=False,
            env=browser_environment,
            start_new_session=True,
        )
    except BaseException:
        os.close(watchdog_write)
        watchdog.wait(timeout=2.0)
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


def _probe() -> int:
    _required_environment("OPPORTUNITY_RADAR_BROWSER_PROXY_SOCKET")
    connect_shim = _required_environment("OPPORTUNITY_RADAR_BROWSER_CONNECT_SHIM")
    parent_netns = _required_environment("OPPORTUNITY_RADAR_BROWSER_PARENT_NETNS")
    _bring_loopback_up()
    _require_isolated_namespace(parent_netns)
    _require_loopback_only_routes()
    probe_environment = dict(os.environ)
    probe_environment["LD_PRELOAD"] = connect_shim
    probe_script = f"""
import errno
import socket

proxy = socket.create_connection(("127.0.0.1", {BROWSER_PROXY_PORT}), timeout=2.0)
proxy.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\\r\\nHost: 127.0.0.1:443\\r\\n\\r\\n")
response = proxy.recv(256)
proxy.close()

direct = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
direct.settimeout(1.0)
try:
    direct.connect(("1.1.1.1", 443))
except OSError as exc:
    direct_blocked = exc.errno == errno.EACCES
else:
    direct_blocked = False
finally:
    direct.close()

raise SystemExit(0 if response.startswith(b"HTTP/1.1 403 ") and direct_blocked else 1)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe_script],
        env=probe_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5.0,
        check=False,
    )
    if result.returncode != 0:
        raise NetworkNamespaceBoundaryError("Browser connect shim or private-address denial failed closed.")
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
