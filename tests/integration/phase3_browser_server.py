from __future__ import annotations

import os
import select
import signal
import socket
import socketserver
import threading

from playwright.sync_api import sync_playwright


if os.environ.get("PHASE3_SYNTHETIC_TEST_MODE") != "1":
    raise RuntimeError("The Phase 3 browser service is test-only")

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())


class _CdpProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        with socket.create_connection(("127.0.0.1", 9223), timeout=5) as upstream:
            peers = (self.request, upstream)
            while not stop.is_set():
                readable, _, _ = select.select(peers, (), (), 0.5)
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(data)


class _CdpProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-proxy-server",
            "--remote-debugging-port=9223",
        ],
    )
    proxy = _CdpProxy(("0.0.0.0", 9222), _CdpProxyHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        stop.wait()
    finally:
        proxy.shutdown()
        proxy.server_close()
        browser.close()
