"""Google Workspace tool for the Telegram concierge runtime."""

from __future__ import annotations

import json
import os
from urllib import error, parse, request

from gateway.session_context import get_session_env
from tools.registry import registry


GOOGLE_WORKSPACE_SCHEMA = {
    "name": "google_workspace",
    "description": (
        "Connect and read the user's Google Workspace via per-user OAuth. "
        "Use for Google Mail/Gmail and Google Docs access. This version is read-only: "
        "search/read Gmail, read supported Gmail attachments, search/read Google Docs, "
        "and check connection status. "
        "Do not claim to send email, modify labels, create documents, or edit documents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "connect",
                    "status",
                    "disconnect",
                    "gmail_search",
                    "gmail_get",
                    "gmail_attachment_get",
                    "docs_search",
                    "docs_get",
                ],
                "description": "Google Workspace action to perform.",
            },
            "query": {
                "type": "string",
                "description": (
                    "Gmail search query for gmail_search, or text query for docs_search. "
                    "For Gmail, use Gmail operators like from:, subject:, newer_than:, has:attachment."
                ),
            },
            "message_id": {
                "type": "string",
                "description": "Gmail message id returned by gmail_search. Required for gmail_get and gmail_attachment_get.",
            },
            "attachment_id": {
                "type": "string",
                "description": "Gmail attachment id returned by gmail_get. Use with gmail_attachment_get.",
            },
            "filename": {
                "type": "string",
                "description": "Attachment filename returned by gmail_get. Alternative to attachment_id for gmail_attachment_get.",
            },
            "doc_id": {
                "type": "string",
                "description": "Google Docs document id or full Google Docs URL.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum search results to return, max 25.",
            },
        },
        "required": ["action"],
    },
}


def _control_url(path: str = "") -> str:
    base = os.environ.get("GOOGLE_WORKSPACE_CONTROL_URL", "http://127.0.0.1:3335/workspace")
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
        with request.urlopen(req, timeout=45) as resp:
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
    for key in ("query", "message_id", "attachment_id", "filename", "doc_id", "max_results"):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    return payload


def _check_workspace_requirements() -> bool:
    return bool(os.environ.get("GOOGLE_WORKSPACE_CONTROL_URL") or os.environ.get("RENDER"))


def google_workspace_tool(args: dict) -> str:
    action = str(args.get("action") or "status").strip()

    if action == "connect":
        result = _http_json("POST", _control_url("/connect"), _session_payload())
        if result.get("success"):
            result.pop("auth_url", None)
            result["assistant_instruction"] = (
                "Send public_message verbatim as the whole user-facing reply. It contains a "
                "Markdown link; do not expand it into a raw URL. Do not add OAuth "
                "implementation details, storage paths, tool names, or diagnostics."
            )
        elif result.get("missing"):
            result["assistant_instruction"] = (
                "Tell the user briefly that Google Workspace connection is not configured yet. "
                "Do not expose raw env var names unless the user asks as the owner."
            )
        return json.dumps(result, ensure_ascii=False)

    if action == "status":
        query = parse.urlencode({"user_id": get_session_env("HERMES_SESSION_USER_ID")})
        return json.dumps(_http_json("GET", _control_url(f"/status?{query}"), None), ensure_ascii=False)

    if action == "disconnect":
        return json.dumps(
            _http_json("POST", _control_url("/disconnect"), _session_payload()),
            ensure_ascii=False,
        )

    if action == "gmail_search":
        return json.dumps(
            _http_json("POST", _control_url("/gmail/search"), _request_payload(args)),
            ensure_ascii=False,
        )

    if action == "gmail_get":
        return json.dumps(
            _http_json("POST", _control_url("/gmail/get"), _request_payload(args)),
            ensure_ascii=False,
        )

    if action == "gmail_attachment_get":
        return json.dumps(
            _http_json("POST", _control_url("/gmail/attachment"), _request_payload(args)),
            ensure_ascii=False,
        )

    if action == "docs_search":
        return json.dumps(
            _http_json("POST", _control_url("/docs/search"), _request_payload(args)),
            ensure_ascii=False,
        )

    if action == "docs_get":
        return json.dumps(
            _http_json("POST", _control_url("/docs/get"), _request_payload(args)),
            ensure_ascii=False,
        )

    return json.dumps({"success": False, "error": f"Unsupported action: {action}"})


registry.register(
    name="google_workspace",
    toolset="google_workspace",
    schema=GOOGLE_WORKSPACE_SCHEMA,
    handler=lambda args, **kw: google_workspace_tool(args),
    check_fn=_check_workspace_requirements,
    requires_env=[
        "GOOGLE_WORKSPACE_CONTROL_URL",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
    ],
    emoji="",
)
