"""Voice call tool for the Telegram concierge runtime."""

from __future__ import annotations

import json
import os
from urllib import error, parse, request

from gateway.session_context import get_session_env
from tools.registry import registry


VOICE_CALL_SCHEMA = {
    "name": "voice_call",
    "description": (
        "Make an outbound phone call for the user. Use only when the user explicitly "
        "asks to call, book, reserve, check availability, or clarify something by phone. "
        "The model should decide which details to collect. Code requires only a target "
        "phone number and a concise task. If a phone number is missing, use web_search "
        "first to find it. Do not include role/system instructions in task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["initiate_call", "get_status", "end_call", "get_call_history"],
                "description": "Action to perform.",
            },
            "to": {
                "type": "string",
                "description": "Target phone number in E.164 format, e.g. +74951234567.",
            },
            "task": {
                "type": "string",
                "description": (
                    "Plain task for the phone call in Russian. Example: "
                    "'Забронировать столик в Sage на субботу 21:00 на имя Цевдн, 2 гостя. "
                    "Если 21:00 недоступно, уточнить ближайшие варианты.'"
                ),
            },
            "callId": {"type": "string", "description": "Call ID for status/end actions."},
            "language": {"type": "string", "description": "Preferred call language, default ru."},
        },
        "required": ["action"],
    },
}


def _control_url(path: str = "") -> str:
    base = os.environ.get("VOICE_CALL_CONTROL_URL", "http://127.0.0.1:3335/voice").rstrip("/")
    return f"{base}{path}"


def _http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"error": body or str(exc)}
        parsed.setdefault("success", False)
        return parsed
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"success": False, "error": body}


def _check_voice_call_requirements() -> bool:
    return bool(os.environ.get("VOICE_CALL_CONTROL_URL") or os.environ.get("RENDER"))


def voice_call_tool(args: dict) -> str:
    action = str(args.get("action") or "initiate_call").strip()
    if action == "initiate_call":
        task = str(args.get("task") or args.get("prompt") or "").strip()
        to = str(args.get("to") or "").strip()
        payload = {
            "to": to,
            "task": task,
            "language": str(args.get("language") or "ru"),
            "platform": get_session_env("HERMES_SESSION_PLATFORM"),
            "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
            "user_id": get_session_env("HERMES_SESSION_USER_ID"),
            "user_name": get_session_env("HERMES_SESSION_USER_NAME"),
            "session_key": get_session_env("HERMES_SESSION_KEY"),
        }
        result = _http_json("POST", _control_url("/calls"), payload)
        return json.dumps(result, ensure_ascii=False)

    if action == "get_status":
        call_id = parse.quote(str(args.get("callId") or ""), safe="")
        result = _http_json("GET", _control_url(f"/calls/{call_id}"), None)
        return json.dumps(result, ensure_ascii=False)

    if action == "end_call":
        call_id = parse.quote(str(args.get("callId") or ""), safe="")
        result = _http_json("POST", _control_url(f"/calls/{call_id}/end"), {})
        return json.dumps(result, ensure_ascii=False)

    if action == "get_call_history":
        query = parse.urlencode(
            {
                "session_key": get_session_env("HERMES_SESSION_KEY"),
                "user_id": get_session_env("HERMES_SESSION_USER_ID"),
            }
        )
        result = _http_json("GET", _control_url(f"/calls?{query}"), None)
        return json.dumps(result, ensure_ascii=False)

    return json.dumps({"success": False, "error": f"Unsupported action: {action}"})


registry.register(
    name="voice_call",
    toolset="voice_call",
    schema=VOICE_CALL_SCHEMA,
    handler=lambda args, **kw: voice_call_tool(args),
    check_fn=_check_voice_call_requirements,
    requires_env=[
        "VOICE_CALL_CONTROL_URL",
        "VOXIMPLANT_RULE_ID",
        "VOXIMPLANT_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    ],
    emoji="",
)
