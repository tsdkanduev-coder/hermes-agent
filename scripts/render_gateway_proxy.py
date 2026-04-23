#!/usr/bin/env python3
"""Render public-port wrapper for Telegram + voice-call deployments."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from gateway.voice_call_runtime import VoiceCallRuntime


PUBLIC_PORT = int(os.environ.get("PORT", "10000"))
INTERNAL_PORT = int(os.environ.get("HERMES_INTERNAL_TELEGRAM_PORT", "8443"))
HEALTH_HOST = os.environ.get("HERMES_INTERNAL_HOST", "127.0.0.1")
RENDER_PROXY_HOST = os.environ.get("RENDER_PROXY_HOST", "0.0.0.0")
VOICE_CONTROL_PORT = int(os.environ.get("VOICE_CALL_CONTROL_PORT", "3335"))
VOICE_CONTROL_HOST = os.environ.get("VOICE_CALL_CONTROL_HOST", "127.0.0.1")


def _python_executable() -> str:
    candidates = []
    virtual_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        candidates.append(os.path.join(virtual_env, "bin", "python"))

    candidates.extend(["/opt/hermes/.venv/bin/python", sys.executable])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


def _gateway_command() -> list[str]:
    explicit = os.environ.get("RENDER_GATEWAY_COMMAND", "").strip()
    if explicit:
        return shlex.split(explicit)
    return [_python_executable(), "-m", "hermes_cli.main", "gateway", "run"]


def _webhook_path() -> str:
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise SystemExit("TELEGRAM_WEBHOOK_URL is required for Render webhook deployments.")
    parsed = urlparse(webhook_url)
    return parsed.path or "/telegram"


def _build_gateway_env() -> dict[str, str]:
    env = dict(os.environ)
    env["TELEGRAM_WEBHOOK_PORT"] = str(INTERNAL_PORT)
    env["VOICE_CALL_CONTROL_URL"] = f"http://{VOICE_CONTROL_HOST}:{VOICE_CONTROL_PORT}/voice"
    return env


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class RenderProxy:
    def __init__(self, gateway_proc: subprocess.Popen[bytes], webhook_path: str):
        self.gateway_proc = gateway_proc
        self.webhook_path = webhook_path
        self.voice = VoiceCallRuntime()
        self.client = aiohttp.ClientSession()

    def gateway_ready(self) -> bool:
        return self.gateway_proc.poll() is None and _port_is_open(HEALTH_HOST, INTERNAL_PORT)

    def health_payload(self) -> dict[str, object]:
        gateway_returncode = self.gateway_proc.poll()
        gateway_ok = self.gateway_ready()
        voice_health = self.voice.health()
        return {
            "status": "ok" if gateway_ok else "starting",
            "gateway_running": gateway_returncode is None,
            "gateway_returncode": gateway_returncode,
            "public_port": PUBLIC_PORT,
            "internal_port": INTERNAL_PORT,
            "webhook_path": self.webhook_path,
            "voice": voice_health,
        }

    async def close(self) -> None:
        await self.client.close()

    async def health(self, _request: web.Request) -> web.Response:
        payload = self.health_payload()
        status = 200 if payload["status"] == "ok" else 503
        return web.json_response(payload, status=status)

    async def proxy_telegram(self, request: web.Request) -> web.StreamResponse:
        if request.path != self.webhook_path:
            return web.json_response({"detail": "Not found"}, status=404)
        if not self.gateway_ready():
            return web.json_response(
                {"status": "starting", "detail": "Gateway is not ready yet."}, status=503
            )

        body = await request.read()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        target = f"http://{HEALTH_HOST}:{INTERNAL_PORT}{request.rel_url}"
        async with self.client.request(
            request.method,
            target,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as response:
            payload = await response.read()
            outgoing = web.Response(status=response.status, body=payload)
            content_type = response.headers.get("Content-Type")
            if content_type:
                outgoing.headers["Content-Type"] = content_type
            return outgoing


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


async def _start_site(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


async def _main_async() -> int:
    webhook_path = _webhook_path()
    command = _gateway_command()

    print(f"[render-proxy] Starting gateway command: {' '.join(command)}", flush=True)
    gateway_proc = subprocess.Popen(command, env=_build_gateway_env())
    proxy = RenderProxy(gateway_proc, webhook_path)

    public_app = web.Application(client_max_size=2 * 1024 * 1024)
    public_app.router.add_get("/", proxy.health)
    public_app.router.add_get("/health", proxy.health)
    public_app.router.add_route("*", webhook_path, proxy.proxy_telegram)
    public_app.router.add_post(proxy.voice.webhook_path, proxy.voice.handle_webhook)
    public_app.router.add_get(proxy.voice.stream_path, proxy.voice.handle_stream)

    control_app = web.Application(client_max_size=2 * 1024 * 1024)
    control_app.router.add_post("/voice/calls", proxy.voice.handle_control_initiate)
    control_app.router.add_get("/voice/calls", proxy.voice.handle_control_history)
    control_app.router.add_get("/voice/calls/{call_id}", proxy.voice.handle_control_status)
    control_app.router.add_post("/voice/calls/{call_id}/end", proxy.voice.handle_control_end)

    public_runner = await _start_site(public_app, RENDER_PROXY_HOST, PUBLIC_PORT)
    control_runner = await _start_site(control_app, VOICE_CONTROL_HOST, VOICE_CONTROL_PORT)

    print(
        f"[render-proxy] Listening on {RENDER_PROXY_HOST}:{PUBLIC_PORT}; "
        f"Telegram {webhook_path} -> {HEALTH_HOST}:{INTERNAL_PORT}{webhook_path}; "
        f"voice webhook {proxy.voice.webhook_path}; voice stream {proxy.voice.stream_path}; "
        f"voice control {VOICE_CONTROL_HOST}:{VOICE_CONTROL_PORT}",
        flush=True,
    )

    stop_event = asyncio.Event()

    def _request_stop(sig_num: int, _frame: Optional[object]) -> None:
        sig_name = signal.Signals(sig_num).name
        print(f"[render-proxy] Received {sig_name}", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    returncode = 0
    try:
        while not stop_event.is_set():
            current = gateway_proc.poll()
            if current is not None:
                returncode = current
                print(f"[render-proxy] Gateway exited with code {returncode}", flush=True)
                stop_event.set()
                break
            await asyncio.sleep(1)
    finally:
        if gateway_proc.poll() is None:
            returncode = _terminate_gateway(gateway_proc, "signal")
        await proxy.close()
        await control_runner.cleanup()
        await public_runner.cleanup()

    return returncode


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
