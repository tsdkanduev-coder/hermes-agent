#!/usr/bin/env python3
"""Render public-port wrapper for Hermes Telegram webhook deployments.

This wrapper avoids optional HTTP dependencies so it can run in minimal
environments. It binds Render's public ``$PORT``, exposes ``/health``, starts
``hermes gateway run`` as a child process, and proxies the Telegram webhook
path to the child gateway's internal webhook listener.
"""

from __future__ import annotations

import http.client
import json
import os
import shlex
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse


PUBLIC_PORT = int(os.environ.get("PORT", "10000"))
INTERNAL_PORT = int(os.environ.get("HERMES_INTERNAL_TELEGRAM_PORT", "8443"))
HEALTH_HOST = os.environ.get("HERMES_INTERNAL_HOST", "127.0.0.1")
RENDER_PROXY_HOST = os.environ.get("RENDER_PROXY_HOST", "0.0.0.0")
GATEWAY_COMMAND = os.environ.get("RENDER_GATEWAY_COMMAND", "hermes gateway run")


def _webhook_path() -> str:
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("TELEGRAM_WEBHOOK_URL is required for Render webhook deployments.")
    parsed = urlparse(webhook_url)
    return parsed.path or "/telegram"


def _build_gateway_env() -> dict[str, str]:
    env = dict(os.environ)
    env["TELEGRAM_WEBHOOK_PORT"] = str(INTERNAL_PORT)
    return env


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class ProxyState:
    def __init__(self, gateway_proc: subprocess.Popen[bytes], webhook_path: str):
        self.gateway_proc = gateway_proc
        self.webhook_path = webhook_path

    def gateway_ready(self) -> bool:
        return self.gateway_proc.poll() is None and _port_is_open(HEALTH_HOST, INTERNAL_PORT)

    def health_payload(self) -> dict[str, object]:
        return {
            "status": "ok" if self.gateway_ready() else "starting",
            "gateway_running": self.gateway_proc.poll() is None,
            "gateway_returncode": self.gateway_proc.poll(),
            "public_port": PUBLIC_PORT,
            "internal_port": INTERNAL_PORT,
            "webhook_path": self.webhook_path,
        }


class RenderProxyHandler(BaseHTTPRequestHandler):
    state: ProxyState

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[render-proxy] {self.address_string()} - {fmt % args}", flush=True)

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path_without_query(self) -> str:
        return self.path.split("?", 1)[0]

    def _handle_health(self) -> None:
        payload = self.state.health_payload()
        self._write_json(200 if payload["status"] == "ok" else 503, payload)

    def _handle_proxy(self) -> None:
        if self._path_without_query() != self.state.webhook_path:
            self._write_json(404, {"detail": "Not found"})
            return

        if not self.state.gateway_ready():
            self._write_json(503, {"status": "starting", "detail": "Gateway is not ready yet."})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length else b""
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }

        connection = http.client.HTTPConnection(HEALTH_HOST, INTERNAL_PORT, timeout=30)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            content_type = response.getheader("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    def do_GET(self) -> None:
        if self._path_without_query() in {"/", "/health"}:
            self._handle_health()
            return
        self._handle_proxy()

    def do_POST(self) -> None:
        self._handle_proxy()

    def do_PUT(self) -> None:
        self._handle_proxy()


def _terminate_gateway(proc: subprocess.Popen[bytes], reason: str) -> int:
    if proc.poll() is not None:
        return proc.returncode or 0

    print(f"[render-proxy] Stopping gateway ({reason})", flush=True)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        print("[render-proxy] Gateway did not exit after SIGTERM, sending SIGKILL", flush=True)
        proc.kill()
        proc.wait(timeout=10)
    return proc.returncode or 0


def main() -> int:
    webhook_path = _webhook_path()
    command = shlex.split(GATEWAY_COMMAND)

    print(f"[render-proxy] Starting gateway command: {' '.join(command)}", flush=True)
    gateway_proc = subprocess.Popen(command, env=_build_gateway_env())
    state = ProxyState(gateway_proc, webhook_path)
    RenderProxyHandler.state = state

    server = ThreadingHTTPServer((RENDER_PROXY_HOST, PUBLIC_PORT), RenderProxyHandler)
    server.daemon_threads = True

    print(
        f"[render-proxy] Listening on {RENDER_PROXY_HOST}:{PUBLIC_PORT} "
        f"and proxying {webhook_path} -> {HEALTH_HOST}:{INTERNAL_PORT}{webhook_path}",
        flush=True,
    )

    stop_event = threading.Event()

    def _request_stop(sig_num: int, _frame: Optional[object]) -> None:
        sig_name = signal.Signals(sig_num).name
        print(f"[render-proxy] Received {sig_name}", flush=True)
        stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    returncode = 0
    try:
        while not stop_event.is_set():
            current = gateway_proc.poll()
            if current is not None:
                returncode = current
                print(f"[render-proxy] Gateway exited with code {returncode}", flush=True)
                stop_event.set()
                server.shutdown()
                break
            time.sleep(1)
    finally:
        if gateway_proc.poll() is None:
            returncode = _terminate_gateway(gateway_proc, "signal")
        server.server_close()
        server_thread.join(timeout=5)

    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
