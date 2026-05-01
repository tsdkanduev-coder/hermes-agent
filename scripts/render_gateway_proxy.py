#!/usr/bin/env python3
"""Render public-port wrapper for Telegram + voice-call deployments."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from gateway.google_calendar_runtime import GoogleCalendarRuntime, delayed_cleanup_pending
from gateway.voice_call_runtime import VoiceCallRuntime


PUBLIC_PORT = int(os.environ.get("PORT", "10000"))
INTERNAL_PORT = int(os.environ.get("HERMES_INTERNAL_TELEGRAM_PORT", "8443"))
HEALTH_HOST = os.environ.get("HERMES_INTERNAL_HOST", "127.0.0.1")
RENDER_PROXY_HOST = os.environ.get("RENDER_PROXY_HOST", "0.0.0.0")
VOICE_CONTROL_PORT = int(os.environ.get("VOICE_CALL_CONTROL_PORT", "3335"))
VOICE_CONTROL_HOST = os.environ.get("VOICE_CALL_CONTROL_HOST", "127.0.0.1")
TRACE_ADMIN_TOKEN_ENV = "TRACE_ADMIN_TOKEN"
TRACE_SEARCH_PATH = "/admin/trace-search"
MAX_TRACE_TEXT_CHARS = 4000


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
    env["GOOGLE_CALENDAR_CONTROL_URL"] = (
        f"http://{VOICE_CONTROL_HOST}:{VOICE_CONTROL_PORT}/calendar"
    )
    env["GOOGLE_WORKSPACE_CONTROL_URL"] = (
        f"http://{VOICE_CONTROL_HOST}:{VOICE_CONTROL_PORT}/workspace"
    )
    return env


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _truncate(value: object, limit: int = MAX_TRACE_TEXT_CHARS) -> object:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... <truncated {len(text) - limit} chars>"


def _redact(value: object) -> object:
    if value is None:
        return None
    text = str(value)
    patterns = [
        (r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]"),
        (r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", "[TELEGRAM_TOKEN_REDACTED]"),
        (r"\brnd_[A-Za-z0-9_-]{16,}\b", "rnd_[REDACTED]"),
        (r"\bfc-[A-Za-z0-9_-]{16,}\b", "fc-[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{16,}\b", "sk-[REDACTED]"),
        (r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,}]+", r"\1=[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return _truncate(text)


def _parse_log_timestamp(line: str) -> Optional[float]:
    # Hermes file logs start with "YYYY-mm-dd HH:MM:SS,mmm".
    raw = line[:23]
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except Exception:
        return None


def _iter_log_files() -> list[Path]:
    log_dir = _hermes_home() / "logs"
    names = [
        "agent.log",
        "agent.log.1",
        "agent.log.2",
        "agent.log.3",
        "gateway.log",
        "gateway.log.1",
        "gateway.log.2",
        "gateway.log.3",
        "errors.log",
        "errors.log.1",
        "errors.log.2",
    ]
    return [log_dir / name for name in names if (log_dir / name).exists()]


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
        self.calendar = GoogleCalendarRuntime()
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
            "calendar": self.calendar.health(),
            "workspace": self.calendar.workspace_health(),
        }

    async def close(self) -> None:
        await self.client.close()

    async def health(self, _request: web.Request) -> web.Response:
        payload = self.health_payload()
        status = 200 if payload["status"] == "ok" else 503
        return web.json_response(payload, status=status)

    def _admin_authorized(self, request: web.Request) -> bool:
        expected = os.environ.get(TRACE_ADMIN_TOKEN_ENV, "").strip()
        if not expected:
            return False
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() == expected
        return request.headers.get("X-Trace-Token", "").strip() == expected

    async def trace_search(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return web.json_response({"detail": "Not found"}, status=404)

        query = request.query.get("q", "").strip()
        if not query:
            return web.json_response({"detail": "q is required"}, status=400)

        try:
            limit = min(max(int(request.query.get("limit", "5")), 1), 20)
        except ValueError:
            limit = 5
        try:
            window_seconds = min(max(int(request.query.get("window_seconds", "900")), 60), 7200)
        except ValueError:
            window_seconds = 900

        db_path = _hermes_home() / "state.db"
        if not db_path.exists():
            return web.json_response(
                {"detail": "state.db not found", "path": str(db_path)}, status=404
            )

        payload: dict[str, object] = {
            "query": query,
            "db_path": str(db_path),
            "matches": [],
        }

        like = f"%{query.lower()}%"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT m.id, m.session_id, m.role, m.content, m.tool_call_id,
                       m.tool_calls, m.tool_name, m.timestamp, s.source, s.user_id,
                       s.model, s.started_at
                FROM messages m
                LEFT JOIN sessions s ON s.id = m.session_id
                WHERE lower(coalesce(m.content, '')) LIKE ?
                   OR lower(coalesce(m.tool_calls, '')) LIKE ?
                ORDER BY m.timestamp DESC, m.id DESC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()

            matches = []
            for row in rows:
                start_ts = float(row["timestamp"] or 0) - window_seconds
                end_ts = float(row["timestamp"] or 0) + window_seconds
                context_rows = conn.execute(
                    """
                    SELECT id, role, content, tool_call_id, tool_calls, tool_name,
                           timestamp, finish_reason
                    FROM messages
                    WHERE session_id = ?
                      AND timestamp BETWEEN ? AND ?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT 80
                    """,
                    (row["session_id"], start_ts, end_ts),
                ).fetchall()

                messages = []
                tool_name_by_call_id: dict[str, str] = {}
                for msg in context_rows:
                    tool_calls = None
                    if msg["tool_calls"]:
                        try:
                            tool_calls = json.loads(msg["tool_calls"])
                            for tc in tool_calls if isinstance(tool_calls, list) else []:
                                call_id = tc.get("id") or tc.get("tool_call_id")
                                name = (tc.get("function") or {}).get("name") or tc.get("name")
                                if call_id and name:
                                    tool_name_by_call_id[str(call_id)] = str(name)
                        except Exception:
                            tool_calls = _redact(msg["tool_calls"])
                    inferred_tool_name = msg["tool_name"]
                    if not inferred_tool_name and msg["tool_call_id"]:
                        inferred_tool_name = tool_name_by_call_id.get(str(msg["tool_call_id"]))
                    messages.append(
                        {
                            "id": msg["id"],
                            "role": msg["role"],
                            "timestamp": msg["timestamp"],
                            "tool_name": inferred_tool_name,
                            "tool_call_id": msg["tool_call_id"],
                            "tool_calls": tool_calls,
                            "finish_reason": msg["finish_reason"],
                            "content": _redact(msg["content"]),
                        }
                    )

                log_lines = self._collect_trace_logs(
                    session_id=str(row["session_id"]),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    query=query,
                    max_lines=300,
                )

                matches.append(
                    {
                        "message_id": row["id"],
                        "session_id": row["session_id"],
                        "source": row["source"],
                        "user_id": row["user_id"],
                        "model": row["model"],
                        "matched_role": row["role"],
                        "matched_timestamp": row["timestamp"],
                        "matched_content": _redact(row["content"]),
                        "messages": messages,
                        "logs": log_lines,
                    }
                )

            payload["matches"] = matches
            return web.json_response(payload)
        finally:
            conn.close()

    async def admin_voice_calls(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return web.json_response({"detail": "Not found"}, status=404)
        return await self.voice.handle_control_history(request)

    async def admin_voice_call_status(self, request: web.Request) -> web.Response:
        if not self._admin_authorized(request):
            return web.json_response({"detail": "Not found"}, status=404)
        return await self.voice.handle_control_status(request)

    def _collect_trace_logs(
        self,
        *,
        session_id: str,
        start_ts: float,
        end_ts: float,
        query: str,
        max_lines: int,
    ) -> list[dict[str, object]]:
        query_lower = query.lower()
        keep_markers = (
            "conversation turn:",
            "api call #",
            "tool ",
            "turn ended:",
            "response ready:",
            "web_search",
            "web_extract",
            "todo",
            "voice_call",
            "realtime",
            "/voice/stream",
            "/voice/webhook",
        )
        collected: list[dict[str, object]] = []
        for path in _iter_log_files():
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.rstrip("\n")
                        lower = line.lower()
                        ts = _parse_log_timestamp(line)
                        in_window = ts is None or (start_ts <= ts <= end_ts)
                        relevant = (
                            session_id in line
                            or query_lower in lower
                            or any(marker in lower for marker in keep_markers)
                        )
                        if in_window and relevant:
                            collected.append(
                                {
                                    "file": path.name,
                                    "timestamp": ts,
                                    "line": _redact(line),
                                }
                            )
            except Exception as exc:
                collected.append(
                    {
                        "file": path.name,
                        "timestamp": None,
                        "line": f"[trace_search] failed to read log file: {exc}",
                    }
                )
        return collected[-max_lines:]

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
    public_app.router.add_get(TRACE_SEARCH_PATH, proxy.trace_search)
    public_app.router.add_get("/admin/voice-calls", proxy.admin_voice_calls)
    public_app.router.add_get("/admin/voice-calls/{call_id}", proxy.admin_voice_call_status)
    public_app.router.add_route("*", webhook_path, proxy.proxy_telegram)
    public_app.router.add_post(proxy.voice.webhook_path, proxy.voice.handle_webhook)
    public_app.router.add_get(proxy.voice.stream_path, proxy.voice.handle_stream)
    public_app.router.add_get(proxy.calendar.callback_path, proxy.calendar.handle_public_callback)

    control_app = web.Application(client_max_size=2 * 1024 * 1024)
    control_app.router.add_post("/voice/calls", proxy.voice.handle_control_initiate)
    control_app.router.add_get("/voice/calls", proxy.voice.handle_control_history)
    control_app.router.add_get("/voice/calls/{call_id}", proxy.voice.handle_control_status)
    control_app.router.add_post("/voice/calls/{call_id}/end", proxy.voice.handle_control_end)
    control_app.router.add_post("/calendar/connect", proxy.calendar.handle_control_connect)
    control_app.router.add_get("/calendar/status", proxy.calendar.handle_control_status)
    control_app.router.add_post("/calendar/disconnect", proxy.calendar.handle_control_disconnect)
    control_app.router.add_post("/calendar/events", proxy.calendar.handle_control_list)
    control_app.router.add_post("/calendar/events/create", proxy.calendar.handle_control_create_event)
    control_app.router.add_post("/calendar/free-slots", proxy.calendar.handle_control_find_slots)
    control_app.router.add_post("/workspace/connect", proxy.calendar.handle_workspace_control_connect)
    control_app.router.add_get("/workspace/status", proxy.calendar.handle_workspace_control_status)
    control_app.router.add_post("/workspace/disconnect", proxy.calendar.handle_control_disconnect)
    control_app.router.add_post("/workspace/gmail/search", proxy.calendar.handle_workspace_gmail_search)
    control_app.router.add_post("/workspace/gmail/get", proxy.calendar.handle_workspace_gmail_get)
    control_app.router.add_post(
        "/workspace/gmail/attachment",
        proxy.calendar.handle_workspace_gmail_attachment_get,
    )
    control_app.router.add_post("/workspace/docs/search", proxy.calendar.handle_workspace_docs_search)
    control_app.router.add_post("/workspace/docs/get", proxy.calendar.handle_workspace_docs_get)

    public_runner = await _start_site(public_app, RENDER_PROXY_HOST, PUBLIC_PORT)
    control_runner = await _start_site(control_app, VOICE_CONTROL_HOST, VOICE_CONTROL_PORT)
    calendar_cleanup_task = asyncio.create_task(delayed_cleanup_pending(proxy.calendar))

    print(
        f"[render-proxy] Listening on {RENDER_PROXY_HOST}:{PUBLIC_PORT}; "
        f"Telegram {webhook_path} -> {HEALTH_HOST}:{INTERNAL_PORT}{webhook_path}; "
        f"voice webhook {proxy.voice.webhook_path}; voice stream {proxy.voice.stream_path}; "
        f"voice/calendar control {VOICE_CONTROL_HOST}:{VOICE_CONTROL_PORT}; "
        f"calendar callback {proxy.calendar.callback_path}",
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
        calendar_cleanup_task.cancel()
        try:
            await calendar_cleanup_task
        except asyncio.CancelledError:
            pass
        await proxy.close()
        await control_runner.cleanup()
        await public_runner.cleanup()

    return returncode


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
