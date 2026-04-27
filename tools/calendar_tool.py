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
        "Connect, read, and create events in the user's Google Calendar via per-user OAuth. "
        "Use when the user asks to connect calendar, check schedule, list events, "
        "find free time, or put a meeting/event into the calendar. Event creation "
        "requires a clear title/topic, date+time, and guests/participants. Do not "
        "claim to edit or delete calendar events."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "connect",
                    "status",
                    "list_events",
                    "find_free_slots",
                    "create_event",
                    "disconnect",
                ],
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
                "description": (
                    "ISO datetime range start for reads, or event start for create_event. "
                    "For create_event it must include both date and time."
                ),
            },
            "end": {
                "type": "string",
                "description": "ISO datetime range end for reads, or event end for create_event.",
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
            "title": {
                "type": "string",
                "description": (
                    "Event title/topic for create_event. Infer from the user's message "
                    "or forwarded thread context."
                ),
            },
            "guests": {
                "type": "string",
                "description": (
                    "Human-readable meeting guests/participants for create_event. Required even "
                    "if attendee emails are not known, e.g. 'Иван, Мария и я'."
                ),
            },
            "attendees": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "displayName": {"type": "string"},
                    },
                    "required": ["email"],
                },
                "description": (
                    "Optional attendee email objects. Email invitations are not sent "
                    "unless send_updates is true."
                ),
            },
            "location": {
                "type": "string",
                "description": "Optional event location.",
            },
            "description": {
                "type": "string",
                "description": "Optional event description or context from the user's message/thread.",
            },
            "send_updates": {
                "type": "boolean",
                "description": "Whether Google should email attendee updates. Default false.",
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
        "title",
        "guests",
        "attendees",
        "location",
        "description",
        "send_updates",
    ):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    return payload


def _format_created_event_message(result: dict) -> str:
    event = result.get("event") or {}
    title = str(event.get("summary") or "(без названия)")
    start = str(event.get("start_local") or event.get("start") or "")
    end = str(event.get("end_local") or event.get("end") or "")
    guests = str(result.get("guests") or "")
    location = str(event.get("location") or "")
    link = str(event.get("htmlLink") or "")
    lines = ["Добавил встречу в календарь.", "", f"— Тема: {title}"]
    if start and end:
        lines.append(f"— Когда: {start} - {end}")
    elif start:
        lines.append(f"— Когда: {start}")
    if guests:
        lines.append(f"— Участники: {guests}")
    if location:
        lines.append(f"— Место: {location}")
    if link:
        lines.extend(["", f"Ссылка: {link}"])
    return "\n".join(lines)


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

    if action == "create_event":
        result = _http_json("POST", _control_url("/events/create"), _request_payload(args))
        if result.get("success"):
            result["public_message"] = _format_created_event_message(result)
            result["assistant_instruction"] = (
                "Send public_message verbatim as the whole user-facing reply. Do not add "
                "raw event ids, calendar ids, OAuth scopes, API payloads, or diagnostics."
            )
        elif result.get("needs_reconnect"):
            result["assistant_instruction"] = (
                "Tell the user briefly that calendar write access needs a fresh Google "
                "authorization. Offer to reconnect the calendar. Do not expose raw scope names."
            )
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
