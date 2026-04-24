"""Google Calendar tool for the Telegram concierge runtime."""

from __future__ import annotations

import json
import os
from urllib import error, parse, request

from gateway.session_context import get_session_env
from tools.registry import registry


CALENDAR_SCHEMA = {
    "name": "calendar",
    "description": (
        "Connect and read the user's Google Calendar via per-user OAuth. "
        "Use when the user asks to connect calendar, check schedule, list events, "
        "or find free time. This first version is read-only: do not claim to create, "
        "edit, or delete calendar events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["connect", "status", "list_events", "find_free_slots", "disconnect"],
                "description": "Calendar action to perform.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Google calendar id. Use 'primary' unless the user asks otherwise.",
            },
            "date": {
                "type": "string",
                "description": "Natural day shortcut or ISO date: today, tomorrow, сегодня, завтра, YYYY-MM-DD.",
            },
            "days": {
                "type": "integer",
                "description": "Number of days to include, max 14.",
            },
            "start": {
                "type": "string",
                "description": "ISO datetime range start. Optional alternative to date/days.",
            },
            "end": {
                "type": "string",
                "description": "ISO datetime range end. Optional alternative to date/days.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone, default Europe/Moscow.",
            },
            "duration_min": {
                "type": "integer",
                "description": "Desired free slot duration in minutes.",
            },
            "day_start_hour": {
                "type": "integer",
                "description": "Start of working/search window in local hour, default 9.",
            },
            "day_end_hour": {
                "type": "integer",
                "description": "End of working/search window in local hour, default 21.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum events to return, max 50.",
            },
        },
        "required": ["action"],
    },
}


def _control_url(path: str = "") -> str:
    base = os.environ.get("GOOGLE_CALENDAR_CONTROL_URL", "http://127.0.0.1:3335/calendar")
    return f"{base.rstrip('/')}{path}"


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
        parsed.setdefault("status_code", exc.code)
        return parsed
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    try:
        return json.loads(body) if body else {}
    except json.JSONDecodeError:
        return {"success": False, "error": body}


def _session_payload() -> dict[str, str]:
    return {
        "platform": get_session_env("HERMES_SESSION_PLATFORM"),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
        "user_id": get_session_env("HERMES_SESSION_USER_ID"),
        "user_name": get_session_env("HERMES_SESSION_USER_NAME"),
        "session_key": get_session_env("HERMES_SESSION_KEY"),
    }


def _request_payload(args: dict) -> dict:
    payload = _session_payload()
    for key in (
        "calendar_id",
        "date",
        "days",
        "start",
        "end",
        "timezone",
        "duration_min",
        "day_start_hour",
        "day_end_hour",
        "max_results",
    ):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    return payload


def _check_calendar_requirements() -> bool:
    return bool(os.environ.get("GOOGLE_CALENDAR_CONTROL_URL") or os.environ.get("RENDER"))


def calendar_tool(args: dict) -> str:
    action = str(args.get("action") or "status").strip()

    if action == "connect":
        result = _http_json("POST", _control_url("/connect"), _session_payload())
        if result.get("success"):
            result["assistant_instruction"] = (
                "Send public_message verbatim as the whole user-facing reply. Do not add "
                "OAuth implementation details, storage paths, tool names, or diagnostics."
            )
        elif result.get("missing"):
            result["assistant_instruction"] = (
                "Tell the user briefly that Google Calendar connection is not configured yet. "
                "Do not expose raw env var names unless the user asks as the owner."
            )
        return json.dumps(result, ensure_ascii=False)

    if action == "status":
        query = parse.urlencode({"user_id": get_session_env("HERMES_SESSION_USER_ID")})
        result = _http_json("GET", _control_url(f"/status?{query}"), None)
        return json.dumps(result, ensure_ascii=False)

    if action == "list_events":
        result = _http_json("POST", _control_url("/events"), _request_payload(args))
        return json.dumps(result, ensure_ascii=False)

    if action == "find_free_slots":
        result = _http_json("POST", _control_url("/free-slots"), _request_payload(args))
        return json.dumps(result, ensure_ascii=False)

    if action == "disconnect":
        result = _http_json("POST", _control_url("/disconnect"), _session_payload())
        return json.dumps(result, ensure_ascii=False)

    return json.dumps({"success": False, "error": f"Unsupported action: {action}"})


registry.register(
    name="calendar",
    toolset="calendar",
    schema=CALENDAR_SCHEMA,
    handler=lambda args, **kw: calendar_tool(args),
    check_fn=_check_calendar_requirements,
    requires_env=[
        "GOOGLE_CALENDAR_CONTROL_URL",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
    ],
    emoji="",
)
