"""GigaCaller-backed voice runtime (WSS) for Hermes voice_call tool.

Set ``VOICE_CALL_BACKEND=gigacaller``. Requires corporate VPN / network access
to ``GIGACALLER_WSS_URL`` when using the default dev gateway.

Control API matches ``VoiceCallRuntime`` (POST /voice/calls, GET status, etc.).
Voximplant webhook/stream routes are unused stubs for the Render proxy layout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from typing import Any

import aiohttp
from aiohttp import web

from gateway.voice_call_runtime import CallRecord, VoiceCallRuntime

logger = logging.getLogger(__name__)

DEFAULT_GIGACALLER_WSS = "wss://gateway-dev-gigacaller.apps.advdad.sberdevices.ru/v1/ws/"
DEFAULT_GIGACALLER_VOICE = "Erm-Freespeech_8000"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _giga_wss_url() -> str:
    return os.environ.get("GIGACALLER_WSS_URL", "").strip() or DEFAULT_GIGACALLER_WSS


def _digits_to_e164_ru(phone: str) -> str:
    d = "".join(c for c in phone if c.isdigit())
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    if len(d) != 11 or d[0] != "7":
        raise ValueError(f"Invalid Russian phone number: {phone!r}")
    return "+" + d


def _build_system_prompt(call: CallRecord) -> str:
    """Map Hermes ``task`` + session fields into a GigaCaller system prompt."""
    guest_name = (call.user_name or "").strip() or "гость"
    guest_phone = os.environ.get("GIGACALLER_GUEST_PHONE", "").strip()
    if not guest_phone:
        guest_phone = "не указан — если спросят, скажите что уточните у гостя в чате"
    task = (call.task or "").strip()
    return "\n".join(
        [
            "**Роль и задача**",
            f"Ты — Мария, ассистент гостя {guest_name}. Исходящий звонок: цель — выполнить задачу ниже. "
            "На линии сотрудник заведения, гостя нет.",
            "",
            "**Правила**",
            "1. Ты звонящая сторона; не выдумывай факты вне задачи.",
            "2. Вежливый разговорный русский, короткие фразы, на «Вы».",
            "3. Если не хватает данных — честно скажи, что уточните у гостя.",
            "",
            "**Контекст Hermes (задача целиком)**",
            task,
            "",
            "**Теги (используй как есть, не меняй номер заведения)**",
            f"Телефон заведения (уже набирается шлюзом): {call.to}",
            f"Имя для брони/записи: {guest_name}",
            f"Телефон гостя (если спросят): {guest_phone}",
        ]
    )


class GigaCallerVoiceRuntime(VoiceCallRuntime):
    """Same HTTP control surface as ``VoiceCallRuntime``; call transport is GigaCaller WSS."""

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not _giga_wss_url():
            missing.append("GIGACALLER_WSS_URL")
        return missing

    def config_warnings(self) -> list[str]:
        warnings: list[str] = []
        if _truthy(os.environ.get("GIGACALLER_INSECURE_SSL")):
            warnings.append("GIGACALLER_INSECURE_SSL is enabled; TLS certificate verification is disabled.")
        if not os.environ.get("GIGACALLER_GUEST_PHONE", "").strip():
            warnings.append(
                "GIGACALLER_GUEST_PHONE is not set; if the venue asks for the guest phone, "
                "the model may not have a real number to give."
            )
        return warnings

    def health(self) -> dict[str, Any]:
        required = self.missing_requirements()
        return {
            "enabled": not required,
            "missing": required,
            "backend": "gigacaller",
            "gigacaller_wss": _giga_wss_url()[:80] + ("…" if len(_giga_wss_url()) > 80 else ""),
            "active_calls": len([c for c in self.calls.values() if not c.ended_at]),
            "warnings": self.config_warnings(),
        }

    async def handle_webhook(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ignored": True, "backend": "gigacaller"})

    async def handle_stream(self, request: web.Request) -> web.Response:
        return web.Response(
            status=410,
            text="Voice stream is not used when VOICE_CALL_BACKEND=gigacaller.",
        )

    async def handle_control_initiate(self, request: web.Request) -> web.Response:
        data = await request.json()
        task = str(data.get("task") or data.get("prompt") or "").strip()
        to = str(data.get("to") or "").strip()
        if not to:
            return web.json_response({"success": False, "error": "to required"}, status=400)
        if not task:
            return web.json_response({"success": False, "error": "task required"}, status=400)
        missing = self.missing_requirements()
        if missing:
            return web.json_response(
                {"success": False, "error": "voice_call not configured", "missing": missing},
                status=503,
            )
        try:
            target = _digits_to_e164_ru(to)
        except ValueError as exc:
            return web.json_response({"success": False, "error": str(exc)}, status=400)

        call = CallRecord(
            call_id=f"call_{uuid.uuid4().hex[:16]}",
            to=target,
            task=task,
            user_id=str(data.get("user_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            session_key=str(data.get("session_key") or ""),
            language=str(data.get("language") or "ru"),
            from_number="",
            stream_token=uuid.uuid4().hex,
        )
        self.calls[call.call_id] = call
        self.calls_by_token[call.stream_token] = call.call_id
        # Synthetic id for API parity; must be unique (unlike a shared "gigacaller" label).
        call.provider_call_id = f"gigacaller:{call.call_id}"
        self.calls_by_provider[call.provider_call_id] = call.call_id
        self._persist(call)
        logger.info(
            "gigacaller voice_call initiate call_id=%s to=%s user_id=%s",
            call.call_id,
            target,
            call.user_id,
        )

        task_runner = asyncio.create_task(
            self._giga_call_runner(call),
            name=f"giga-voice-{call.call_id}",
        )
        setattr(call, "_runner_task", task_runner)

        return web.json_response(
            {
                "success": True,
                "callId": call.call_id,
                "providerCallId": call.provider_call_id,
                "status": "initiated",
            }
        )

    async def handle_control_end(self, request: web.Request) -> web.Response:
        call = self._resolve_call(request.match_info.get("call_id", ""))
        if not call:
            return web.json_response({"success": False, "error": "call not found"}, status=404)
        runner: asyncio.Task | None = getattr(call, "_runner_task", None)
        if runner:
            if not runner.done():
                runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        # ``_giga_call_runner`` always runs ``_finalize_call`` in ``finally``.
        return web.json_response({"success": True})

    def _ssl_for_ws(self) -> bool | ssl.SSLContext:
        if _truthy(os.environ.get("GIGACALLER_INSECURE_SSL")):
            return False
        return True

    async def _giga_call_runner(self, call: CallRecord) -> None:
        url = _giga_wss_url()
        voice = os.environ.get("GIGACALLER_VOICE", DEFAULT_GIGACALLER_VOICE).strip()
        model = os.environ.get("GIGACALLER_MODEL", "").strip()
        prompt = _build_system_prompt(call)
        payload: dict[str, Any] = {
            "phoneNumber": call.to,
            "systemPrompt": prompt,
            "retry": "",
            "voice": voice,
        }
        if model:
            payload["model"] = model
        initial = {"type": "initialRequest", "data": payload}

        timeout = aiohttp.ClientTimeout(
            total=float(os.environ.get("GIGACALLER_CALL_TIMEOUT_SECONDS", "900") or "900")
        )
        call.status = "connecting"
        call.touch()
        self._persist(call)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(url, ssl=self._ssl_for_ws(), heartbeat=25) as ws:
                    greeting = await ws.receive()
                    if greeting.type == aiohttp.WSMsgType.TEXT:
                        g_len = len(greeting.data or "")
                    elif greeting.type == aiohttp.WSMsgType.BINARY:
                        g_len = len(greeting.data or b"")
                    else:
                        raise RuntimeError(f"unexpected greeting from GigaCaller: {greeting.type!r}")
                    logger.debug("gigacaller greeting call_id=%s type=%s len=%s", call.call_id, greeting.type, g_len)

                    await ws.send_str(json.dumps(initial, ensure_ascii=False))
                    call.status = "streaming"
                    call.touch()
                    self._persist(call)
                    logger.info("gigacaller initialRequest sent call_id=%s", call.call_id)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._ingest_giga_message(call, msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(str(ws.exception() or "websocket error"))

                    call.status = "completed"
        except asyncio.CancelledError:
            call.status = "cancelled"
            call.error = call.error or "cancelled"
            raise
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            logger.exception("gigacaller session failed call_id=%s", call.call_id)
        finally:
            call.ended_at = call.ended_at or time.time()
            call.touch()
            self._persist(call)
            await self._finalize_call(call, call.status or "ended")

    def _ingest_giga_message(self, call: CallRecord, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") != "transcription":
            return
        inner = msg.get("data")
        if not isinstance(inner, dict):
            return
        src = str(inner.get("source", "")).lower()
        text = str(inner.get("text", "")).strip()
        if not text:
            return
        role = "model" if src == "model" else "peer"
        self._add_transcript(call, role, text)
