"""Minimal production voice-call runtime for Render Telegram deployments.

The runtime mirrors the Labota/OpenClaw voice-call contract without importing
the whole plugin system:

- Hermes tool calls an internal localhost control API.
- Voximplant is started through Management API ``StartScenarios``.
- VoxEngine sends lifecycle callbacks to ``/voice/webhook``.
- VoxEngine bridges call audio to ``/voice/stream`` over WebSocket.
- OpenAI Realtime handles the live speech-to-speech conversation.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web, WSMsgType

try:
    import jwt
except Exception:  # pragma: no cover - dependency exists in Render all extra
    jwt = None


logger = logging.getLogger(__name__)


CALL_ENDED_EVENTS = {
    "call.ended",
    "call.completed",
    "call.failed",
    "call.error",
    "call.hangup",
    "ended",
    "completed",
    "failed",
    "error",
    "hangup",
    "disconnected",
    "timeout",
    "busy",
    "no_answer",
    "no-answer",
    "cancelled",
    "canceled",
}


@dataclass
class CallRecord:
    call_id: str
    to: str
    task: str
    user_id: str = ""
    chat_id: str = ""
    session_key: str = ""
    language: str = "ru"
    from_number: str = ""
    status: str = "created"
    provider_call_id: str = ""
    stream_token: str = ""
    control_url: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    summary_sent: bool = False

    def touch(self) -> None:
        self.updated_at = time.time()


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/opt/data")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _mask_phone(value: str) -> str:
    clean = str(value or "").strip()
    if len(clean) <= 4:
        return "***"
    return f"***{clean[-4:]}"


def _public_origin() -> str:
    explicit = os.environ.get("VOICE_CALL_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        parsed = urlparse(explicit)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    telegram_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if telegram_url:
        parsed = urlparse(telegram_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return "https://giga-hermes-staging.onrender.com"


def _ws_origin(origin: str) -> str:
    return origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)


def _read_private_key() -> str:
    raw = os.environ.get("VOXIMPLANT_MANAGEMENT_PRIVATE_KEY", "").strip()
    if raw:
        return raw
    b64 = os.environ.get("VOXIMPLANT_MANAGEMENT_PRIVATE_KEY_B64", "").strip()
    if not b64:
        return ""
    try:
        return base64.b64decode(b64).decode("utf-8")
    except Exception:
        return ""


class VoiceCallRuntime:
    def __init__(self) -> None:
        self.calls: dict[str, CallRecord] = {}
        self.calls_by_provider: dict[str, str] = {}
        self.calls_by_token: dict[str, str] = {}
        self.storage_dir = _get_hermes_home() / "voice-calls"
        self.public_origin = _public_origin()
        self.webhook_path = os.environ.get("VOICE_CALL_WEBHOOK_PATH", "/voice/webhook")
        self.stream_path = os.environ.get("VOICE_CALL_STREAM_PATH", "/voice/stream")
        self._management_jwt_cache: tuple[str, int] | None = None

    def health(self) -> dict[str, Any]:
        required = self.missing_requirements()
        return {
            "enabled": not required,
            "missing": required,
            "public_origin": self.public_origin,
            "webhook_path": self.webhook_path,
            "stream_path": self.stream_path,
            "active_calls": len([c for c in self.calls.values() if not c.ended_at]),
            "warnings": self.config_warnings(),
        }

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            missing.append("OPENAI_API_KEY")
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not os.environ.get("VOXIMPLANT_RULE_ID", "").strip():
            missing.append("VOXIMPLANT_RULE_ID")
        if not os.environ.get("VOXIMPLANT_WEBHOOK_SECRET", "").strip() and not _truthy(
            os.environ.get("VOICE_CALL_SKIP_WEBHOOK_VERIFICATION")
        ):
            missing.append("VOXIMPLANT_WEBHOOK_SECRET")
        has_static_jwt = bool(os.environ.get("VOXIMPLANT_MANAGEMENT_JWT", "").strip())
        has_service_account = all(
            [
                os.environ.get("VOXIMPLANT_MANAGEMENT_ACCOUNT_ID", "").strip(),
                os.environ.get("VOXIMPLANT_MANAGEMENT_KEY_ID", "").strip(),
                _read_private_key(),
            ]
        )
        if not has_static_jwt and not has_service_account:
            missing.append("VOXIMPLANT_MANAGEMENT_JWT or service account fields")
        return missing

    def config_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not os.environ.get("VOICE_CALL_FROM_NUMBER", "").strip():
            warnings.append(
                "VOICE_CALL_FROM_NUMBER is not set; Voximplant scenario must provide a caller ID fallback."
            )
        return warnings

    # ------------------------------------------------------------------
    # Internal control API used by the Hermes tool
    # ------------------------------------------------------------------

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
        from_number = str(data.get("from") or os.environ.get("VOICE_CALL_FROM_NUMBER", "")).strip()
        if not from_number:
            return web.json_response(
                {
                    "success": False,
                    "error": "voice_call not configured",
                    "missing": ["VOICE_CALL_FROM_NUMBER"],
                    "detail": "Outbound Voximplant calls require a configured caller ID.",
                },
                status=503,
            )

        call = CallRecord(
            call_id=f"call_{uuid.uuid4().hex[:16]}",
            to=to,
            task=task,
            user_id=str(data.get("user_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            session_key=str(data.get("session_key") or ""),
            language=str(data.get("language") or "ru"),
            from_number=from_number,
            stream_token=uuid.uuid4().hex,
        )
        self.calls[call.call_id] = call
        self.calls_by_token[call.stream_token] = call.call_id
        self._persist(call)
        logger.info(
            "voice_call initiate call_id=%s to=%s from_set=%s user_id=%s",
            call.call_id,
            _mask_phone(call.to),
            bool(call.from_number),
            call.user_id,
        )

        try:
            await self._start_voximplant_call(call)
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            call.touch()
            self._persist(call)
            logger.exception("voice_call start failed call_id=%s", call.call_id)
            return web.json_response({"success": False, "error": str(exc)}, status=502)

        return web.json_response(
            {
                "success": True,
                "callId": call.call_id,
                "providerCallId": call.provider_call_id,
                "status": call.status,
            }
        )

    async def handle_control_status(self, request: web.Request) -> web.Response:
        call = self._resolve_call(request.match_info.get("call_id", ""))
        if not call:
            return web.json_response({"found": False}, status=404)
        return web.json_response({"found": True, "call": self._safe_call_dict(call)})

    async def handle_control_history(self, request: web.Request) -> web.Response:
        session_key = request.query.get("session_key", "")
        user_id = request.query.get("user_id", "")
        calls = list(self.calls.values())
        if session_key:
            calls = [call for call in calls if call.session_key == session_key]
        elif user_id:
            calls = [call for call in calls if call.user_id == user_id]
        calls.sort(key=lambda call: call.created_at, reverse=True)
        return web.json_response({"calls": [self._safe_call_dict(call) for call in calls[:20]]})

    async def handle_control_end(self, request: web.Request) -> web.Response:
        call = self._resolve_call(request.match_info.get("call_id", ""))
        if not call:
            return web.json_response({"success": False, "error": "call not found"}, status=404)
        if call.control_url:
            await self._post_control(call, {"action": "hangup", "reason": "user_requested"})
        await self._finalize_call(call, "ended")
        return web.json_response({"success": True})

    # ------------------------------------------------------------------
    # Public Voximplant routes
    # ------------------------------------------------------------------

    async def handle_webhook(self, request: web.Request) -> web.Response:
        raw = await request.text()
        payload: dict[str, Any]
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}

        if not self._verify_webhook(request, payload):
            return web.Response(status=401, text="Unauthorized")

        call = self._call_from_payload(payload, request)
        if not call:
            logger.info(
                "voice_call webhook ignored event=%s payload_keys=%s",
                self._event_type(payload),
                sorted(payload.keys()),
            )
            return web.json_response({"ok": True, "ignored": True})

        event_type = self._event_type(payload)
        provider_call_id = self._pick(payload, "providerCallId", "call_session_history_id", "session_id")
        if provider_call_id:
            call.provider_call_id = provider_call_id
            self.calls_by_provider[provider_call_id] = call.call_id

        control_url = self._pick(
            payload,
            "media_session_access_secure_url",
            "mediaSessionAccessSecureUrl",
            "control_url",
            "controlUrl",
        )
        if control_url:
            call.control_url = control_url

        transcript = self._pick(payload, "transcript", "speech", "text")
        if transcript and event_type in {"call.speech", "speech", "transcript"}:
            call.transcript.append({"role": "callee", "text": transcript})

        call.status = event_type or call.status
        call.touch()
        self._persist(call)
        logger.info(
            "voice_call webhook call_id=%s provider_call_id=%s event=%s control_url_set=%s transcript_items=%s",
            call.call_id,
            call.provider_call_id,
            event_type or "unknown",
            bool(call.control_url),
            len(call.transcript),
        )

        if event_type in CALL_ENDED_EVENTS or "end" in event_type or "hangup" in event_type:
            asyncio.create_task(self._finalize_call(call, event_type or "ended"))

        return web.json_response({"ok": True})

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        token = request.query.get("token", "")
        call_id = self.calls_by_token.get(token)
        call = self.calls.get(call_id or "")
        if not call:
            logger.warning("voice_call stream rejected unknown token")
            return web.Response(status=401, text="Unknown stream token")

        vox_ws = web.WebSocketResponse(heartbeat=25)
        await vox_ws.prepare(request)

        realtime = OpenAIRealtimeBridge(
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("VOICE_CALL_REALTIME_MODEL", "gpt-realtime"),
            voice=os.environ.get("VOICE_CALL_ASSISTANT_VOICE", "alloy"),
            instructions=self._build_voice_instructions(call),
            language=call.language or "ru",
            call=call,
            on_assistant_audio=lambda audio: self._send_audio_to_vox(vox_ws, call, audio),
            on_transcript=lambda role, text: self._add_transcript(call, role, text),
        )
        await realtime.connect()
        call.status = "streaming"
        call.touch()
        self._persist(call)
        logger.info("voice_call stream connected call_id=%s", call.call_id)

        stream_sid = f"vox-{call.call_id}"
        seq = 0
        try:
            async for msg in vox_ws:
                if msg.type == WSMsgType.BINARY:
                    await realtime.send_audio(msg.data)
                    continue
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                event_name = str(event.get("event") or "").lower()
                if event_name == "start":
                    stream_sid = (
                        event.get("streamSid")
                        or (event.get("start") or {}).get("streamSid")
                        or stream_sid
                    )
                    setattr(call, "_stream_sid", stream_sid)
                    call.status = "stream_connected"
                    call.touch()
                    await vox_ws.send_json(
                        {
                            "event": "start",
                            "sequenceNumber": 0,
                            "start": {
                                "mediaFormat": {
                                    "encoding": "ULAW",
                                    "sampleRate": 8000,
                                    "channels": 1,
                                },
                                "customParameters": {},
                            },
                            "streamSid": stream_sid,
                        }
                    )
                    continue
                if event_name == "media":
                    payload = ((event.get("media") or {}).get("payload") or "").strip()
                    if payload:
                        try:
                            await realtime.send_audio(base64.b64decode(payload))
                        except Exception:
                            pass
                    continue
                if event_name == "stop":
                    break
                seq += 1
        finally:
            await realtime.close()
            call.touch()
            self._persist(call)
            logger.info("voice_call stream closed call_id=%s", call.call_id)
        return vox_ws

    async def _send_audio_to_vox(
        self, vox_ws: web.WebSocketResponse, call: CallRecord, audio: bytes
    ) -> None:
        if vox_ws.closed:
            return
        stream_sid = getattr(call, "_stream_sid", f"vox-{call.call_id}")
        for offset in range(0, len(audio), 160):
            chunk = audio[offset : offset + 160]
            if not chunk:
                continue
            count = getattr(call, "_audio_seq", 0) + 1
            setattr(call, "_audio_seq", count)
            timestamp_ms = (count - 1) * 20
            await vox_ws.send_json(
                {
                    "event": "media",
                    "sequenceNumber": count,
                    "streamSid": stream_sid,
                    "media": {
                        "chunk": count,
                        "timestamp": timestamp_ms,
                        "payload": base64.b64encode(chunk).decode("ascii"),
                    },
                }
            )

    # ------------------------------------------------------------------
    # Voximplant management
    # ------------------------------------------------------------------

    async def _start_voximplant_call(self, call: CallRecord) -> None:
        webhook_url = f"{self.public_origin}{self.webhook_path}?callId={call.call_id}"
        stream_url = f"{_ws_origin(self.public_origin)}{self.stream_path}?token={call.stream_token}"
        script_custom_data = {
            "provider": "voximplant",
            "callId": call.call_id,
            "from": call.from_number,
            "to": call.to,
            "webhookUrl": webhook_url,
            "webhookSecret": os.environ.get("VOXIMPLANT_WEBHOOK_SECRET", ""),
            "streamUrl": stream_url,
            "clientState": {
                "telegramChatId": call.chat_id,
                "telegramUserId": call.user_id,
                "sessionKey": call.session_key,
            },
        }
        logger.info(
            "voice_call StartScenarios call_id=%s rule_id=%s to=%s from_set=%s",
            call.call_id,
            os.environ["VOXIMPLANT_RULE_ID"],
            _mask_phone(call.to),
            bool(call.from_number),
        )
        response = await self._voximplant_api(
            "StartScenarios",
            {
                "rule_id": os.environ["VOXIMPLANT_RULE_ID"],
                "script_custom_data": json.dumps(script_custom_data, ensure_ascii=False),
            },
        )
        call.provider_call_id = self._pick(
            response,
            "call_session_history_id",
            "callSessionHistoryId",
            "session_id",
            "sessionId",
            "request_id",
            "requestId",
            "call_id",
            "callId",
        ) or call.call_id
        call.control_url = self._pick(
            response,
            "media_session_access_secure_url",
            "mediaSessionAccessSecureUrl",
            "control_url",
            "controlUrl",
        )
        call.status = "initiated"
        self.calls_by_provider[call.provider_call_id] = call.call_id
        call.touch()
        self._persist(call)
        logger.info(
            "voice_call initiated call_id=%s provider_call_id=%s control_url_set=%s",
            call.call_id,
            call.provider_call_id,
            bool(call.control_url),
        )

    async def _voximplant_api(self, action: str, params: dict[str, str]) -> dict[str, Any]:
        token = await self._management_jwt()
        base_url = os.environ.get("VOXIMPLANT_API_BASE_URL", "https://api.voximplant.com/platform_api")
        url = f"{base_url.rstrip('/')}/{action}/"
        async with aiohttp.ClientSession() as session:
            response, text, payload = await self._post_voximplant(session, url, params, token)
            if response == 401 and self._can_generate_management_jwt():
                token = await self._management_jwt(force_service_account=True)
                response, text, payload = await self._post_voximplant(session, url, params, token)
            if response >= 400:
                raise RuntimeError(f"Voximplant API error {response}: {text[:300]}")
            if isinstance(payload, dict):
                error = self._pick(payload, "error", "error_msg", "errorMsg")
                if error:
                    raise RuntimeError(f"Voximplant API error: {error}")
                if payload.get("result") == 0:
                    raise RuntimeError("Voximplant API returned unsuccessful result")
                return payload
            return {}

    async def _post_voximplant(
        self, session: aiohttp.ClientSession, url: str, params: dict[str, str], token: str
    ) -> tuple[int, str, dict[str, Any]]:
        async with session.post(
            url,
            data=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {}
            return response.status, text, payload if isinstance(payload, dict) else {}

    def _can_generate_management_jwt(self) -> bool:
        return bool(
            os.environ.get("VOXIMPLANT_MANAGEMENT_ACCOUNT_ID", "").strip()
            and os.environ.get("VOXIMPLANT_MANAGEMENT_KEY_ID", "").strip()
            and _read_private_key()
        )

    async def _management_jwt(self, *, force_service_account: bool = False) -> str:
        static = os.environ.get("VOXIMPLANT_MANAGEMENT_JWT", "").strip()
        if (
            not force_service_account
            and static
            and static not in {"AUTO", "__AUTO__", "__SERVICE_ACCOUNT__"}
        ):
            return static
        if self._management_jwt_cache and self._management_jwt_cache[1] - 60 > int(time.time()):
            return self._management_jwt_cache[0]
        if jwt is None:
            raise RuntimeError("PyJWT is required for Voximplant service-account JWT generation")
        now = int(time.time())
        exp = now + 3600
        token = jwt.encode(
            {"iat": now, "iss": os.environ["VOXIMPLANT_MANAGEMENT_ACCOUNT_ID"], "exp": exp},
            _read_private_key(),
            algorithm="RS256",
            headers={"kid": os.environ["VOXIMPLANT_MANAGEMENT_KEY_ID"]},
        )
        self._management_jwt_cache = (token, exp)
        return token

    async def _post_control(self, call: CallRecord, payload: dict[str, Any]) -> None:
        async with aiohttp.ClientSession() as session:
            await session.post(
                call.control_url,
                json={"provider": "voximplant", "callId": call.call_id, **payload},
                timeout=aiohttp.ClientTimeout(total=10),
            )

    # ------------------------------------------------------------------
    # Summary and persistence
    # ------------------------------------------------------------------

    async def _finalize_call(self, call: CallRecord, reason: str) -> None:
        if call.summary_sent:
            return
        call.status = reason
        call.ended_at = call.ended_at or time.time()
        call.summary_sent = True
        call.touch()
        self._persist(call)
        if not call.chat_id:
            return
        text = await self._build_summary(call)
        await self._send_telegram(call.chat_id, text)

    async def _build_summary(self, call: CallRecord) -> str:
        transcript = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('text', '')}" for item in call.transcript[-80:]
        ).strip()
        if not transcript:
            if "error" in call.status or "fail" in call.status:
                return (
                    "Звонок не состоялся: соединение завершилось ошибкой на стороне телефонии. "
                    "Бронь не подтверждена."
                )
            return (
                "Звонок завершён, но подробная расшифровка не пришла от голосового канала. "
                "Если нужно, могу попробовать ещё раз."
            )
        prompt = (
            "Составь короткий отчёт для клиента Telegram после телефонного звонка консьержа.\n"
            "Пиши по-русски, уважительно, без первого лица множественного числа. "
            "Не пиши 'мы'. Используй стиль: 'уточнил', 'забронировал вам', 'ресторан сообщил'.\n"
            f"Задача: {call.task}\n\nРасшифровка:\n{transcript}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                    json={
                        "model": os.environ.get("VOICE_CALL_SUMMARY_MODEL", "gpt-5.4"),
                        "messages": [
                            {"role": "system", "content": "Ты профессиональный консьерж."},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as response:
                    payload = await response.json()
                    content = (
                        payload.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
                    if content:
                        return content
        except Exception:
            pass
        return f"Звонок завершён. Краткая расшифровка:\n{transcript[:2500]}"

    async def _send_telegram(self, chat_id: str, text: str) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=20),
            )

    def _persist(self, call: CallRecord) -> None:
        try:
            user_dir = self.storage_dir / (call.user_id or "unknown")
            user_dir.mkdir(parents=True, exist_ok=True)
            path = user_dir / f"{call.call_id}.json"
            data = self._safe_call_dict(call, include_transcript=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _add_transcript(self, call: CallRecord, role: str, text: str) -> None:
        clean = text.strip()
        if not clean:
            return
        call.transcript.append({"role": role, "text": clean})
        call.touch()
        self._persist(call)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_call(self, call_id: str) -> CallRecord | None:
        return self.calls.get(call_id) or self.calls.get(self.calls_by_provider.get(call_id, ""))

    def _call_from_payload(self, payload: dict[str, Any], request: web.Request) -> CallRecord | None:
        call_id = self._pick(payload, "callId") or request.query.get("callId", "")
        provider_id = self._pick(payload, "providerCallId", "call_session_history_id", "session_id")
        return self._resolve_call(call_id) or self._resolve_call(provider_id or "")

    def _verify_webhook(self, request: web.Request, payload: dict[str, Any]) -> bool:
        if _truthy(os.environ.get("VOICE_CALL_SKIP_WEBHOOK_VERIFICATION")):
            return True
        expected = os.environ.get("VOXIMPLANT_WEBHOOK_SECRET", "").strip()
        if not expected:
            return False
        provided = (
            request.headers.get("x-openclaw-voximplant-secret")
            or request.headers.get("x-voximplant-secret")
            or request.query.get("secret")
            or self._pick(payload, "secret", "webhookSecret")
            or ""
        )
        return hmac.compare_digest(expected, provided)

    def _event_type(self, payload: dict[str, Any]) -> str:
        raw = str(
            self._pick(payload, "type", "event", "eventType", "name", "status") or ""
        ).strip()
        normalized = raw.lower().replace("_", ".").replace("-", ".")
        aliases = {
            "connected": "call.answered",
            "answered": "call.answered",
            "ringing": "call.ringing",
            "disconnected": "call.ended",
            "completed": "call.completed",
            "failed": "call.failed",
        }
        return aliases.get(normalized, normalized)

    def _build_voice_instructions(self, call: CallRecord) -> str:
        task = call.task[:500]
        return "\n".join(
            [
                "Роль (внутренний контекст, не произносить): профессиональный персональный консьерж.",
                "Вы сейчас говорите по телефону с сотрудником организации, чтобы выполнить задачу клиента.",
                f"Задача: {task}",
                "Говорите по-русски, коротко, спокойно и по-деловому.",
                "Начните с приветствия и сразу сформулируйте задачу одной фразой.",
                "Не называйте себя ботом, ИИ, Hermes или OpenClaw.",
                "Не используйте первое лицо множественного числа: не говорите 'мы'.",
                "Если собеседник предлагает существенно другой вариант, уточните детали и завершите звонок, чтобы клиент принял решение.",
                "Не соглашайтесь на предоплату, депозит или передачу реквизитов без отдельного подтверждения клиента.",
                "В конце поблагодарите и попрощайтесь.",
            ]
        )

    def _safe_call_dict(self, call: CallRecord, *, include_transcript: bool = False) -> dict[str, Any]:
        data = asdict(call)
        data.pop("stream_token", None)
        if not include_transcript:
            data["transcript"] = data["transcript"][-10:]
        return data

    @staticmethod
    def _pick(data: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and not isinstance(value, (dict, list)):
                return str(value).strip()
        return ""


class OpenAIRealtimeBridge:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        instructions: str,
        language: str,
        call: CallRecord,
        on_assistant_audio,
        on_transcript,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.call = call
        self.on_assistant_audio = on_assistant_audio
        self.on_transcript = on_transcript
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader_task: asyncio.Task | None = None
        self._assistant_text = ""

    async def connect(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(
            f"wss://api.openai.com/v1/realtime?model={self.model}",
            headers={"Authorization": f"Bearer {self.api_key}", "OpenAI-Beta": "realtime=v1"},
            heartbeat=20,
        )
        await self.ws.send_json(
            {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": self.instructions,
                    "voice": self.voice,
                    "temperature": 0.6,
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "input_audio_transcription": {
                        "model": "whisper-1",
                        "language": self.language or "ru",
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "high",
                        "create_response": True,
                    },
                },
            }
        )
        self.reader_task = asyncio.create_task(self._read_events())

    async def send_audio(self, audio: bytes) -> None:
        if self.ws and not self.ws.closed and audio:
            await self.ws.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                }
            )

    async def close(self) -> None:
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()

    async def _read_events(self) -> None:
        assert self.ws is not None
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type") or "")
            if event_type == "response.audio.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    await self.on_assistant_audio(base64.b64decode(delta))
                continue
            if event_type in {"response.audio_transcript.delta", "response.output_text.delta"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    self._assistant_text += delta
                continue
            if event_type in {"response.audio_transcript.done", "response.output_text.done"}:
                text = str(event.get("transcript") or event.get("text") or self._assistant_text).strip()
                self._assistant_text = ""
                if text:
                    self.on_transcript("assistant", text)
                continue
            if event_type == "conversation.item.input_audio_transcription.completed":
                text = str(event.get("transcript") or "").strip()
                if text:
                    self.on_transcript("callee", text)
                continue
