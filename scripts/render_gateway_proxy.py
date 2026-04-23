#!/usr/bin/env python3
"""Render public-port wrapper for Hermes Telegram webhook deployments.

Render expects the public HTTP service to bind to ``$PORT`` and commonly uses
an HTTP health check. Hermes' Telegram webhook mode binds directly to the
Telegram adapter's port and does not expose a generic health route on that same
listener. This wrapper solves both concerns:

- binds the public Render port and serves ``/health``
- starts ``hermes gateway run`` as a child process
- proxies the Telegram webhook path to the child gateway's internal port
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from typing import Optional
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, web


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


async def _port_is_open(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    await writer.wait_closed()
    return True


class RenderGatewayProxy:
    def __init__(self, gateway_proc: asyncio.subprocess.Process, webhook_path: str):
        self.gateway_proc = gateway_proc
        self.webhook_path = webhook_path
        self.session = ClientSession(timeout=ClientTimeout(total=30))
        self.app = web.Application()
        self.app.router.add_get("/", self.handle_root)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_route("*", webhook_path, self.handle_webhook)
        self.runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, RENDER_PROXY_HOST, PUBLIC_PORT)
        await site.start()
        print(
            f"[render-proxy] Listening on {RENDER_PROXY_HOST}:{PUBLIC_PORT} "
            f"and proxying {self.webhook_path} -> {HEALTH_HOST}:{INTERNAL_PORT}{self.webhook_path}",
            flush=True,
        )

    async def close(self) -> None:
        await self.session.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def _gateway_ready(self) -> bool:
        return self.gateway_proc.returncode is None and await _port_is_open(HEALTH_HOST, INTERNAL_PORT)

    async def handle_root(self, _request: web.Request) -> web.Response:
        return await self.handle_health(_request)

    async def handle_health(self, _request: web.Request) -> web.Response:
        ready = await self._gateway_ready()
        body = {
            "status": "ok" if ready else "starting",
            "gateway_running": self.gateway_proc.returncode is None,
            "gateway_returncode": self.gateway_proc.returncode,
            "public_port": PUBLIC_PORT,
            "internal_port": INTERNAL_PORT,
            "webhook_path": self.webhook_path,
        }
        return web.json_response(body, status=200 if ready else 503)

    async def handle_webhook(self, request: web.Request) -> web.Response:
        if not await self._gateway_ready():
            return web.json_response(
                {"status": "starting", "detail": "Gateway is not ready yet."},
                status=503,
            )

        target = f"http://{HEALTH_HOST}:{INTERNAL_PORT}{self.webhook_path}"
        if request.query_string:
            target = f"{target}?{request.query_string}"

        data = await request.read()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        }

        async with self.session.request(
            request.method,
            target,
            data=data,
            headers=headers,
            allow_redirects=False,
        ) as response:
            payload = await response.read()
            proxy_headers = {}
            content_type = response.headers.get("Content-Type")
            if content_type:
                proxy_headers["Content-Type"] = content_type
            return web.Response(body=payload, status=response.status, headers=proxy_headers)


async def _terminate_gateway(proc: asyncio.subprocess.Process, reason: str) -> int:
    if proc.returncode is not None:
        return proc.returncode

    print(f"[render-proxy] Stopping gateway ({reason})", flush=True)
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=20)
    except asyncio.TimeoutError:
        print("[render-proxy] Gateway did not exit after SIGTERM, sending SIGKILL", flush=True)
        proc.kill()
        await proc.wait()
    return proc.returncode or 0


async def main() -> int:
    webhook_path = _webhook_path()
    command = shlex.split(GATEWAY_COMMAND)

    print(f"[render-proxy] Starting gateway command: {' '.join(command)}", flush=True)
    gateway_proc = await asyncio.create_subprocess_exec(*command, env=_build_gateway_env())
    proxy = RenderGatewayProxy(gateway_proc, webhook_path)
    await proxy.start()

    stop_event = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        print(f"[render-proxy] Received {sig_name}", flush=True)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda _s, _f, name=sig.name: _request_stop(name))

    gateway_wait = asyncio.create_task(gateway_proc.wait())
    stop_wait = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        {gateway_wait, stop_wait},
        return_when=asyncio.FIRST_COMPLETED,
    )

    returncode = 0
    if gateway_wait in done:
        returncode = gateway_proc.returncode or 0
        print(f"[render-proxy] Gateway exited with code {returncode}", flush=True)
    else:
        returncode = await _terminate_gateway(gateway_proc, "signal")

    for task in pending:
        task.cancel()

    await proxy.close()
    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
