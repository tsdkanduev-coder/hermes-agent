"""Voice call tool for the Telegram concierge runtime."""

from __future__ import annotations

import json
import os
import re
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
                    "'Забронировать столик в Sage на субботу 21:00 на имя пользователя, 2 гостя. "
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


def _extract_reservation_details(task: str) -> list[str]:
    lines: list[str] = []
    clean = " ".join(task.split())
    weekday = (
        r"понедельник|вторник|сред[ау]|четверг|пятниц[ау]|"
        r"суббот[ау]|воскресенье"
    )

    date_match = re.search(
        rf"\b(сегодня|завтра|послезавтра|(?:в|на)\s+(?:{weekday})|\d{{4}}-\d{{2}}-\d{{2}})\b",
        clean,
        re.IGNORECASE,
    )
    if date_match:
        lines.append(f"Дата: {date_match.group(1)}")

    time_match = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", clean)
    if not time_match:
        time_match = re.search(
            r"\b(?:в|на|после)\s+([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\b",
            clean,
            re.IGNORECASE,
        )
    if time_match:
        minutes = time_match.group(2) or "00"
        lines.append(f"Время: {time_match.group(1)}:{minutes}")

    party_match = re.search(
        r"\b(\d+)\s*(?:человек|человека|персон|персоны|гост(?:я|ей)?)\b",
        clean,
        re.IGNORECASE,
    )
    if party_match:
        lines.append(f"Количество персон: {party_match.group(1)}")

    name_match = re.search(
        r"(?:на имя|имя брони|имя)\s+([А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\s-]{0,40})",
        clean,
        re.IGNORECASE,
    )
    if name_match:
        lines.append(f"Имя брони: {name_match.group(1).strip()}")

    condition_match = re.search(r"\b(если .+)$", clean, re.IGNORECASE)
    if condition_match:
        lines.append(f"Условие: {condition_match.group(1).strip()}")

    return lines


def _format_started_message(to: str, task: str) -> str:
    lines = [f"Запустил звонок на номер {to} с задачей:", ""]
    detail_lines = _extract_reservation_details(task)
    if detail_lines:
        if re.search(r"\b(брон|забронировать|столик|стол)\b", task, re.IGNORECASE):
            lines.append("— Забронировать столик")
        else:
            headline = re.split(r"[.;]", task.strip(), maxsplit=1)[0].strip()
            if headline:
                lines.append(f"— {headline}")
        lines.extend(f"— {line}" for line in detail_lines)
    else:
        lines.append(f"— {task.strip()}")
    lines.extend(["", "Как только будет результат, сообщу детали."])
    return "\n".join(lines)


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
        if result.get("success"):
            result["public_message"] = _format_started_message(to, task)
            result["assistant_instruction"] = (
                "Send public_message verbatim as the entire user-facing reply. Do not add "
                "status explanations. Do not expose callId, providerCallId, raw status names "
                "such as call.initiated, or internal diagnostics."
            )
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
        "VOICE_CALL_CONTROL_URL (or RENDER for render-gateway-proxy)",
        "TELEGRAM_BOT_TOKEN",
        "If VOICE_CALL_BACKEND=gigacaller: GIGACALLER_WSS_URL optional (has dev default), "
        "GIGACALLER_GUEST_PHONE recommended; GIGACALLER_INSECURE_SSL=1 only for broken CA.",
        "If VOICE_CALL_BACKEND unset (voximplant): VOXIMPLANT_* + OPENAI_API_KEY or XAI_API_KEY "
        "+ VOICE_CALL_FROM_NUMBER.",
        "OPENAI_API_KEY optional for post-call summary (both backends).",
    ],
    emoji="",
)
