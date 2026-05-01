"""Google Calendar OAuth/runtime for managed Telegram deployments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web


logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
GOOGLE_GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GOOGLE_GMAIL_ATTACHMENTS_URL = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}"
)
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DOCS_DOCUMENT_URL = "https://docs.googleapis.com/v1/documents/{document_id}"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_FULL_SCOPE = "https://www.googleapis.com/auth/calendar"
DEFAULT_CALENDAR_SCOPE = CALENDAR_EVENTS_SCOPE
CALENDAR_READ_SCOPES = {CALENDAR_READONLY_SCOPE, CALENDAR_EVENTS_SCOPE, CALENDAR_FULL_SCOPE}
CALENDAR_WRITE_SCOPES = {CALENDAR_EVENTS_SCOPE, CALENDAR_FULL_SCOPE}
DEFAULT_WORKSPACE_SCOPES = [
    DEFAULT_CALENDAR_SCOPE,
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/opt/data")


def _public_origin() -> str:
    explicit = os.environ.get("GOOGLE_OAUTH_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        parsed = urlparse(explicit)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    voice_url = os.environ.get("VOICE_CALL_PUBLIC_URL", "").strip().rstrip("/")
    if voice_url:
        parsed = urlparse(voice_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    telegram_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if telegram_url:
        parsed = urlparse(telegram_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
    if render_hostname:
        return f"https://{render_hostname}"
    return "https://giga-hermes-staging.onrender.com"


def _scopes() -> list[str]:
    raw = (
        os.environ.get("GOOGLE_WORKSPACE_SCOPES", "").strip()
        or os.environ.get("GOOGLE_CALENDAR_SCOPES", "").strip()
        or " ".join(DEFAULT_WORKSPACE_SCOPES)
    )
    scopes = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    return scopes or list(DEFAULT_WORKSPACE_SCOPES)


def _has_any_scope(raw: Any, accepted: set[str]) -> bool:
    if not raw:
        return False
    if isinstance(raw, str):
        scopes = {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    elif isinstance(raw, list):
        scopes = {str(part).strip() for part in raw if str(part).strip()}
    else:
        return False
    return bool(scopes & accepted)


def _safe_filename(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_iso(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("datetime value required")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if "T" not in text and len(text) == 10:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_event_datetime(value: str, tz_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("datetime value required")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if "T" not in text:
        raise ValueError("event datetime must include date and time")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def _display_dt(value: str, tz_name: str) -> str:
    try:
        dt = _parse_iso(value)
    except Exception:
        return value
    try:
        local = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local = dt
    return local.strftime("%Y-%m-%d %H:%M")


@dataclass
class PendingOAuth:
    state: str
    code_verifier: str
    user_id: str
    chat_id: str
    session_key: str = ""
    user_name: str = ""
    product: str = "calendar"
    created_at: float = field(default_factory=time.time)


class GoogleCalendarRuntime:
    def __init__(self) -> None:
        self.public_origin = _public_origin()
        self.callback_path = os.environ.get(
            "GOOGLE_CALENDAR_CALLBACK_PATH",
            "/oauth/google/calendar/callback",
        )
        self.storage_dir = _get_hermes_home() / "google-calendar"
        self.pending_dir = self.storage_dir / "pending"
        self.users_dir = self.storage_dir / "users"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.users_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.storage_dir, self.pending_dir, self.users_dir):
            try:
                os.chmod(path, 0o700)
            except OSError:
                logger.debug("could not tighten permissions for %s", path, exc_info=True)

    def health(self) -> dict[str, Any]:
        missing = self.missing_requirements()
        return {
            "enabled": not missing,
            "missing": missing,
            "callback_path": self.callback_path,
            "scopes": _scopes(),
            "connected_users": len(list(self.users_dir.glob("*.json"))),
        }

    def workspace_health(self) -> dict[str, Any]:
        scopes = _scopes()
        return {
            "enabled": not self.missing_requirements(),
            "missing": self.missing_requirements(),
            "callback_path": self.callback_path,
            "scopes": scopes,
            "connected_users": len(list(self.users_dir.glob("*.json"))),
            "capabilities": {
                "calendar_read": _has_any_scope(scopes, CALENDAR_READ_SCOPES),
                "calendar_write": _has_any_scope(scopes, CALENDAR_WRITE_SCOPES),
                "gmail_read": "https://www.googleapis.com/auth/gmail.readonly" in scopes,
                "gmail_attachments_read": "https://www.googleapis.com/auth/gmail.readonly" in scopes,
                "drive_read": "https://www.googleapis.com/auth/drive.readonly" in scopes,
                "docs_read": "https://www.googleapis.com/auth/documents.readonly" in scopes,
            },
        }

    def missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if not os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip():
            missing.append("GOOGLE_OAUTH_CLIENT_ID")
        if not os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip():
            missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            missing.append("TELEGRAM_BOT_TOKEN")
        return missing

    def redirect_uri(self) -> str:
        explicit = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
        return explicit or f"{self.public_origin}{self.callback_path}"

    async def handle_control_connect(self, request: web.Request) -> web.Response:
        data = await request.json()
        return await self._handle_connect_payload(data)

    async def handle_workspace_control_connect(self, request: web.Request) -> web.Response:
        data = await request.json()
        data["product"] = "workspace"
        return await self._handle_connect_payload(data)

    async def _handle_connect_payload(self, data: dict[str, Any]) -> web.Response:
        user_id = str(data.get("user_id") or "").strip()
        chat_id = str(data.get("chat_id") or "").strip()
        if not user_id or not chat_id:
            return web.json_response(
                {"success": False, "error": "Telegram user_id and chat_id are required"},
                status=400,
            )
        missing = self.missing_requirements()
        if missing:
            return web.json_response(
                {
                    "success": False,
                    "error": "calendar oauth is not configured",
                    "missing": missing,
                },
                status=503,
            )

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        pending = PendingOAuth(
            state=state,
            code_verifier=verifier,
            user_id=user_id,
            chat_id=chat_id,
            session_key=str(data.get("session_key") or ""),
            user_name=str(data.get("user_name") or ""),
            product=str(data.get("product") or "calendar"),
        )
        self._write_json(self._pending_path(state), pending.__dict__)

        params = {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "redirect_uri": self.redirect_uri(),
            "response_type": "code",
            "scope": " ".join(_scopes()),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        return web.json_response(
            {
                "success": True,
                "auth_url": auth_url,
                "public_message": self._connect_public_message(auth_url, pending.product),
            }
        )

    async def handle_control_status(self, request: web.Request) -> web.Response:
        user_id = request.query.get("user_id", "").strip()
        token = self._load_user_token(user_id)
        return web.json_response(
            {
                "success": True,
                "connected": bool(token),
                "scopes": (token or {}).get("scope", ""),
                "updated_at": (token or {}).get("updated_at"),
            }
        )

    async def handle_workspace_control_status(self, request: web.Request) -> web.Response:
        user_id = request.query.get("user_id", "").strip()
        token = self._load_user_token(user_id)
        return web.json_response(
            {
                "success": True,
                "connected": bool(token),
                "scopes": (token or {}).get("scope", ""),
                "updated_at": (token or {}).get("updated_at"),
                "capabilities": self.workspace_health()["capabilities"],
            }
        )

    async def handle_control_disconnect(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        path = self._user_path(user_id)
        if path.exists():
            path.unlink()
        return web.json_response({"success": True, "connected": False})

    async def handle_workspace_gmail_search(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        query = str(data.get("query") or "newer_than:7d").strip()
        max_results = min(max(int(data.get("max_results") or 10), 1), 25)
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "google workspace is not connected", "connected": False},
                status=401,
            )
        messages = await self._gmail_search(token, query=query, max_results=max_results)
        return web.json_response(
            {"success": True, "connected": True, "query": query, "messages": messages}
        )

    async def handle_workspace_gmail_get(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        message_id = str(data.get("message_id") or "").strip()
        if not message_id:
            return web.json_response(
                {"success": False, "error": "message_id is required"},
                status=400,
            )
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "google workspace is not connected", "connected": False},
                status=401,
            )
        message = await self._gmail_get(token, message_id)
        return web.json_response({"success": True, "connected": True, "message": message})

    async def handle_workspace_gmail_attachment_get(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        message_id = str(data.get("message_id") or "").strip()
        attachment_id = str(data.get("attachment_id") or "").strip()
        filename = str(data.get("filename") or "").strip()
        if not message_id:
            return web.json_response(
                {"success": False, "error": "message_id is required"},
                status=400,
            )
        if not attachment_id and not filename:
            return web.json_response(
                {"success": False, "error": "attachment_id or filename is required"},
                status=400,
            )
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "google workspace is not connected", "connected": False},
                status=401,
            )
        attachment = await self._gmail_attachment_get(
            token,
            message_id,
            attachment_id=attachment_id,
            filename=filename,
        )
        return web.json_response({"success": True, "connected": True, "attachment": attachment})

    async def handle_workspace_docs_search(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        query = str(data.get("query") or "").strip()
        max_results = min(max(int(data.get("max_results") or 10), 1), 25)
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "google workspace is not connected", "connected": False},
                status=401,
            )
        documents = await self._docs_search(token, query=query, max_results=max_results)
        return web.json_response(
            {"success": True, "connected": True, "query": query, "documents": documents}
        )

    async def handle_workspace_docs_get(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        raw_doc_id = str(data.get("doc_id") or data.get("url") or "").strip()
        doc_id = self._extract_doc_id(raw_doc_id)
        if not doc_id:
            return web.json_response(
                {"success": False, "error": "doc_id or Google Docs URL is required"},
                status=400,
            )
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "google workspace is not connected", "connected": False},
                status=401,
            )
        document = await self._docs_get(token, doc_id)
        return web.json_response({"success": True, "connected": True, "document": document})

    async def handle_control_list(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        calendar_id = str(data.get("calendar_id") or "primary")
        tz_name = str(data.get("timezone") or os.environ.get("TZ") or "Europe/Moscow")
        start, end = self._range_from_request(data, tz_name)
        max_results = min(max(int(data.get("max_results") or 20), 1), 50)
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "calendar is not connected", "connected": False},
                status=401,
            )
        events = await self._list_events(
            token,
            calendar_id=calendar_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            max_results=max_results,
        )
        return web.json_response(
            {
                "success": True,
                "connected": True,
                "calendar_id": calendar_id,
                "time_min": start.isoformat(),
                "time_max": end.isoformat(),
                "events": [self._compact_event(event, tz_name) for event in events],
            }
        )

    async def handle_control_find_slots(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        calendar_id = str(data.get("calendar_id") or "primary")
        tz_name = str(data.get("timezone") or os.environ.get("TZ") or "Europe/Moscow")
        duration_min = min(max(int(data.get("duration_min") or 60), 15), 480)
        start, end = self._range_from_request(data, tz_name)
        day_start_hour = int(data.get("day_start_hour") or 9)
        day_end_hour = int(data.get("day_end_hour") or 21)
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "calendar is not connected", "connected": False},
                status=401,
            )
        events = await self._list_events(
            token,
            calendar_id=calendar_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            max_results=100,
        )
        slots = self._find_slots(
            events,
            start=start,
            end=end,
            tz_name=tz_name,
            duration_min=duration_min,
            day_start_hour=day_start_hour,
            day_end_hour=day_end_hour,
        )
        return web.json_response(
            {
                "success": True,
                "connected": True,
                "calendar_id": calendar_id,
                "duration_min": duration_min,
                "slots": slots[:10],
            }
        )

    async def handle_control_create_event(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        calendar_id = str(data.get("calendar_id") or "primary")
        tz_name = str(data.get("timezone") or os.environ.get("TZ") or "Europe/Moscow")
        title = str(data.get("title") or data.get("summary") or "").strip()
        start_raw = str(data.get("start") or "").strip()
        end_raw = str(data.get("end") or "").strip()
        guests = str(data.get("guests") or "").strip()
        location = str(data.get("location") or "").strip()
        description = str(data.get("description") or "").strip()
        duration_min = min(max(int(data.get("duration_min") or 60), 15), 1440)
        if not title:
            return web.json_response({"success": False, "error": "event title is required"}, status=400)
        if not start_raw:
            return web.json_response(
                {"success": False, "error": "event start datetime with date and time is required"},
                status=400,
            )
        if not guests:
            return web.json_response(
                {"success": False, "error": "event guests or participants are required"},
                status=400,
            )
        try:
            start_dt = _parse_event_datetime(start_raw, tz_name)
            end_dt = (
                _parse_event_datetime(end_raw, tz_name)
                if end_raw
                else start_dt + timedelta(minutes=duration_min)
            )
        except Exception as exc:
            return web.json_response({"success": False, "error": str(exc)}, status=400)
        if end_dt <= start_dt:
            return web.json_response(
                {"success": False, "error": "event end must be after start"},
                status=400,
            )

        token_payload = self._load_user_token(user_id)
        if not token_payload:
            return web.json_response(
                {"success": False, "error": "calendar is not connected", "connected": False},
                status=401,
            )
        if not _has_any_scope(token_payload.get("scope"), CALENDAR_WRITE_SCOPES):
            return web.json_response(
                {
                    "success": False,
                    "error": "calendar write access is not granted",
                    "connected": True,
                    "needs_reconnect": True,
                },
                status=403,
            )
        token = await self._valid_access_token(user_id)
        if not token:
            return web.json_response(
                {"success": False, "error": "calendar is not connected", "connected": False},
                status=401,
            )

        event_body = self._build_event_body(
            title=title,
            start=start_dt,
            end=end_dt,
            tz_name=tz_name,
            guests=guests,
            description=description,
            location=location,
            attendees=data.get("attendees"),
        )
        event = await self._create_event(
            token,
            calendar_id=calendar_id,
            event_body=event_body,
            send_updates=bool(data.get("send_updates")),
        )
        return web.json_response(
            {
                "success": True,
                "connected": True,
                "calendar_id": calendar_id,
                "event": self._compact_event(event, tz_name),
                "guests": guests,
            }
        )

    async def handle_public_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "").strip()
        code = request.query.get("code", "").strip()
        error = request.query.get("error", "").strip()
        pending = self._load_pending(state)
        if not pending:
            return self._html_response("Ссылка устарела. Вернитесь в Telegram и запросите подключение календаря заново.", status=400)
        if error:
            await self._send_telegram(
                pending.chat_id,
                self._oauth_denied_message(pending.product),
            )
            self._pending_path(state).unlink(missing_ok=True)
            return self._html_response("Доступ не подтверждён. Можно закрыть эту вкладку.")
        if not code:
            return self._html_response("Google не вернул код авторизации.", status=400)
        try:
            token_payload = await self._exchange_code(code, pending.code_verifier)
            existing = self._load_user_token(pending.user_id) or {}
            if "refresh_token" not in token_payload and existing.get("refresh_token"):
                token_payload["refresh_token"] = existing["refresh_token"]
            token_payload["created_at"] = existing.get("created_at") or time.time()
            token_payload["updated_at"] = time.time()
            token_payload["expires_at"] = time.time() + int(token_payload.get("expires_in") or 3600)
            self._write_json(self._user_path(pending.user_id), token_payload)
            self._pending_path(state).unlink(missing_ok=True)
            await self._send_telegram(
                pending.chat_id,
                self._oauth_success_message(pending.product),
            )
            return self._html_response(self._oauth_success_html(pending.product))
        except Exception as exc:
            logger.exception("google calendar oauth callback failed")
            await self._send_telegram(
                pending.chat_id,
                self._oauth_error_message(pending.product),
            )
            return self._html_response(f"Не удалось подключить Google: {html.escape(str(exc))}", status=500)

    def _range_from_request(self, data: dict[str, Any], tz_name: str) -> tuple[datetime, datetime]:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        if data.get("start") or data.get("end"):
            start = _parse_iso(str(data.get("start") or now.isoformat()))
            end = _parse_iso(str(data.get("end") or (start + timedelta(days=7)).isoformat()))
            return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        date = str(data.get("date") or "").strip().lower()
        if date in {"today", "сегодня", ""}:
            day = now.date()
        elif date in {"tomorrow", "завтра"}:
            day = (now + timedelta(days=1)).date()
        else:
            day = datetime.fromisoformat(date).date()
        start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        days = max(int(data.get("days") or 1), 1)
        end = start + timedelta(days=min(days, 14))
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    async def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        data = {
            "code": code,
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "redirect_uri": self.redirect_uri(),
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_TOKEN_URL,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(str(payload.get("error_description") or payload.get("error") or payload))
                return payload

    async def _refresh_token(self, user_id: str, token: dict[str, Any]) -> dict[str, Any] | None:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            return None
        data = {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_TOKEN_URL,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    logger.warning("google calendar token refresh failed user=%s error=%s", user_id, payload)
                    return None
        token.update(payload)
        token["refresh_token"] = refresh_token
        token["updated_at"] = time.time()
        token["expires_at"] = time.time() + int(payload.get("expires_in") or 3600)
        self._write_json(self._user_path(user_id), token)
        return token

    async def _valid_access_token(self, user_id: str) -> str:
        token = self._load_user_token(user_id)
        if not token:
            return ""
        if float(token.get("expires_at") or 0) - 60 > time.time():
            return str(token.get("access_token") or "")
        refreshed = await self._refresh_token(user_id, token)
        return str((refreshed or {}).get("access_token") or "")

    async def _list_events(
        self,
        access_token: str,
        *,
        calendar_id: str,
        time_min: str,
        time_max: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": str(max_results),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise web.HTTPBadGateway(text=json.dumps({"success": False, "error": payload}))
                return list(payload.get("items") or [])

    async def _create_event(
        self,
        access_token: str,
        *,
        calendar_id: str,
        event_body: dict[str, Any],
        send_updates: bool,
    ) -> dict[str, Any]:
        url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=quote(calendar_id, safe=""))
        params = {"sendUpdates": "all" if send_updates else "none"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params=params,
                json=event_body,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise web.HTTPBadGateway(text=json.dumps({"success": False, "error": payload}))
                return payload

    def _build_event_body(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        tz_name: str,
        guests: str,
        description: str,
        location: str,
        attendees: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        }
        notes = []
        if guests:
            notes.append(f"Гости/участники: {guests}")
        if description:
            notes.append(description)
        if notes:
            body["description"] = "\n\n".join(notes)
        if location:
            body["location"] = location
        attendee_items = self._attendees_from_value(attendees)
        if attendee_items:
            body["attendees"] = attendee_items
        return body

    @staticmethod
    def _attendees_from_value(value: Any) -> list[dict[str, str]]:
        if not value:
            return []
        raw_items: list[Any]
        if isinstance(value, str):
            raw_items = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
        elif isinstance(value, list):
            raw_items = value
        else:
            return []
        attendees: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_items:
            email = ""
            display_name = ""
            if isinstance(item, dict):
                email = str(item.get("email") or "").strip()
                display_name = str(item.get("displayName") or item.get("display_name") or "").strip()
            else:
                email = str(item or "").strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                continue
            email_key = email.lower()
            if email_key in seen:
                continue
            seen.add(email_key)
            attendee = {"email": email}
            if display_name:
                attendee["displayName"] = display_name
            attendees.append(attendee)
        return attendees

    async def _google_get_json(
        self,
        access_token: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise web.HTTPBadGateway(text=json.dumps({"success": False, "error": payload}))
                return payload

    async def _gmail_search(
        self,
        access_token: str,
        *,
        query: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        payload = await self._google_get_json(
            access_token,
            GOOGLE_GMAIL_MESSAGES_URL,
            params={"q": query, "maxResults": str(max_results)},
        )
        messages: list[dict[str, Any]] = []
        for meta in payload.get("messages") or []:
            message_id = str(meta.get("id") or "")
            if not message_id:
                continue
            message = await self._google_get_json(
                access_token,
                f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                },
            )
            headers = self._gmail_headers(message)
            messages.append(
                {
                    "id": message.get("id", ""),
                    "threadId": message.get("threadId", ""),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": message.get("snippet", ""),
                    "labels": message.get("labelIds", []),
                }
            )
        return messages

    async def _gmail_get(self, access_token: str, message_id: str) -> dict[str, Any]:
        message = await self._google_get_json(
            access_token,
            f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}",
            params={"format": "full"},
        )
        headers = self._gmail_headers(message)
        attachments = await self._gmail_attachment_summaries(access_token, message)
        return {
            "id": message.get("id", ""),
            "threadId": message.get("threadId", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "labels": message.get("labelIds", []),
            "snippet": message.get("snippet", ""),
            "body": self._truncate_text(self._extract_gmail_body(message), 20000),
            "attachments": attachments,
        }

    async def _gmail_attachment_get(
        self,
        access_token: str,
        message_id: str,
        *,
        attachment_id: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        message = await self._google_get_json(
            access_token,
            f"{GOOGLE_GMAIL_MESSAGES_URL}/{quote(message_id, safe='')}",
            params={"format": "full"},
        )
        target: dict[str, Any] | None = None
        normalized_filename = filename.strip().lower()
        for part in self._iter_gmail_parts(message.get("payload") or {}):
            part_filename = str(part.get("filename") or "").strip()
            body = part.get("body") or {}
            part_attachment_id = str(body.get("attachmentId") or "").strip()
            if attachment_id and part_attachment_id == attachment_id:
                target = part
                break
            if normalized_filename and part_filename.lower() == normalized_filename:
                target = part
                break
        if not target:
            raise web.HTTPNotFound(
                text=json.dumps(
                    {"success": False, "error": "attachment not found"},
                    ensure_ascii=False,
                )
            )

        raw = await self._gmail_part_bytes(access_token, message_id, target)
        filename = str(target.get("filename") or filename or "attachment")
        mime_type = str(target.get("mimeType") or "application/octet-stream")
        extraction = self._extract_attachment_text(filename, mime_type, raw)
        return {
            "message_id": message_id,
            "attachment_id": str((target.get("body") or {}).get("attachmentId") or ""),
            "filename": filename,
            "mimeType": mime_type,
            "size": len(raw),
            "text": self._truncate_text(extraction.get("text", ""), 50000),
            "extraction_status": extraction.get("status", "unknown"),
        }

    async def _gmail_attachment_summaries(
        self,
        access_token: str,
        message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        message_id = str(message.get("id") or "")
        max_preview_bytes = int(
            os.environ.get("GOOGLE_WORKSPACE_ATTACHMENT_PREVIEW_MAX_BYTES", "1048576")
        )
        max_previews = int(os.environ.get("GOOGLE_WORKSPACE_ATTACHMENT_PREVIEW_COUNT", "5"))
        attachments: list[dict[str, Any]] = []
        previews_used = 0
        for part in self._iter_gmail_parts(message.get("payload") or {}):
            filename = str(part.get("filename") or "").strip()
            body = part.get("body") or {}
            attachment_id = str(body.get("attachmentId") or "").strip()
            inline_data = body.get("data")
            if not filename and not attachment_id:
                continue
            mime_type = str(part.get("mimeType") or "application/octet-stream")
            size = int(body.get("size") or 0)
            item: dict[str, Any] = {
                "filename": filename or "(inline attachment)",
                "mimeType": mime_type,
                "attachment_id": attachment_id,
                "size": size,
                "has_text_preview": False,
            }
            should_preview = previews_used < max_previews and (
                size <= max_preview_bytes or bool(inline_data)
            )
            if should_preview:
                try:
                    raw = await self._gmail_part_bytes(access_token, message_id, part)
                    extraction = self._extract_attachment_text(filename, mime_type, raw)
                    preview = extraction.get("text", "")
                    if preview:
                        item["text_preview"] = self._truncate_text(preview, 12000)
                        item["has_text_preview"] = True
                    item["extraction_status"] = extraction.get("status", "unknown")
                    previews_used += 1
                except Exception as exc:
                    item["extraction_status"] = f"preview_failed: {type(exc).__name__}"
            elif size > max_preview_bytes:
                item["extraction_status"] = "too_large_for_auto_preview"
            attachments.append(item)
        return attachments

    async def _gmail_part_bytes(
        self,
        access_token: str,
        message_id: str,
        part: dict[str, Any],
    ) -> bytes:
        body = part.get("body") or {}
        data = body.get("data")
        if data:
            return self._decode_b64url_bytes(str(data))
        attachment_id = str(body.get("attachmentId") or "").strip()
        if not attachment_id:
            return b""
        payload = await self._google_get_json(
            access_token,
            GOOGLE_GMAIL_ATTACHMENTS_URL.format(
                message_id=quote(message_id, safe=""),
                attachment_id=quote(attachment_id, safe=""),
            ),
        )
        return self._decode_b64url_bytes(str(payload.get("data") or ""))

    async def _docs_search(
        self,
        access_token: str,
        *,
        query: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        filters = [
            "mimeType='application/vnd.google-apps.document'",
            "trashed=false",
        ]
        if query:
            literal = self._drive_query_literal(query)
            filters.append(f"(name contains '{literal}' or fullText contains '{literal}')")
        payload = await self._google_get_json(
            access_token,
            GOOGLE_DRIVE_FILES_URL,
            params={
                "q": " and ".join(filters),
                "pageSize": str(max_results),
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
                "orderBy": "modifiedTime desc",
            },
        )
        return list(payload.get("files") or [])

    async def _docs_get(self, access_token: str, doc_id: str) -> dict[str, Any]:
        doc = await self._google_get_json(
            access_token,
            GOOGLE_DOCS_DOCUMENT_URL.format(document_id=quote(doc_id, safe="")),
        )
        return {
            "title": doc.get("title", ""),
            "documentId": doc.get("documentId", doc_id),
            "body": self._truncate_text(self._extract_doc_text(doc), 50000),
        }

    def _compact_event(self, event: dict[str, Any], tz_name: str) -> dict[str, Any]:
        start_raw = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date") or ""
        end_raw = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date") or ""
        return {
            "id": event.get("id", ""),
            "summary": event.get("summary") or "(без названия)",
            "start": start_raw,
            "end": end_raw,
            "start_local": _display_dt(start_raw, tz_name),
            "end_local": _display_dt(end_raw, tz_name),
            "location": event.get("location", ""),
            "status": event.get("status", ""),
            "htmlLink": event.get("htmlLink", ""),
        }

    def _find_slots(
        self,
        events: list[dict[str, Any]],
        *,
        start: datetime,
        end: datetime,
        tz_name: str,
        duration_min: int,
        day_start_hour: int,
        day_end_hour: int,
    ) -> list[dict[str, str]]:
        tz = ZoneInfo(tz_name)
        busy: list[tuple[datetime, datetime]] = []
        for event in events:
            start_raw = (event.get("start") or {}).get("dateTime")
            end_raw = (event.get("end") or {}).get("dateTime")
            if not start_raw or not end_raw:
                continue
            try:
                busy.append((_parse_iso(start_raw).astimezone(tz), _parse_iso(end_raw).astimezone(tz)))
            except Exception:
                continue
        busy.sort()

        slots: list[dict[str, str]] = []
        cursor_day = start.astimezone(tz).date()
        end_day = end.astimezone(tz).date()
        while cursor_day <= end_day:
            window_start = datetime.combine(cursor_day, datetime.min.time(), tzinfo=tz).replace(hour=day_start_hour)
            window_end = datetime.combine(cursor_day, datetime.min.time(), tzinfo=tz).replace(hour=day_end_hour)
            cursor = max(window_start, start.astimezone(tz))
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= window_end:
                    continue
                if busy_start > cursor and (busy_start - cursor).total_seconds() >= duration_min * 60:
                    slots.append({"start": cursor.isoformat(), "end": busy_start.isoformat()})
                cursor = max(cursor, busy_end)
            if window_end > cursor and (window_end - cursor).total_seconds() >= duration_min * 60:
                slots.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
            cursor_day += timedelta(days=1)
        return slots

    def _connect_public_message(self, auth_url: str, product: str) -> str:
        if product == "workspace":
            return (
                "Чтобы подключить Google Почту, Документы и календарь, откройте ссылку и разрешите доступ:\n"
                f"{auth_url}\n\n"
                "После подтверждения вернитесь в Telegram — сообщу, когда доступ подключится."
            )
        return (
            "Чтобы подключить Google Calendar, откройте ссылку и разрешите доступ:\n"
            f"{auth_url}\n\n"
            "После подтверждения вернитесь в Telegram — сообщу, когда календарь подключится."
        )

    @staticmethod
    def _oauth_success_message(product: str) -> str:
        if product == "workspace":
            return (
                "Google Workspace подключён. Теперь могу искать и читать письма, "
                "вложения в письмах, Google Docs, календарь и добавлять встречи по вашему запросу."
            )
        return "Календарь подключён. Теперь могу смотреть расписание, искать свободные окна и добавлять встречи."

    @staticmethod
    def _oauth_success_html(product: str) -> str:
        if product == "workspace":
            return "Google Workspace подключён. Можно вернуться в Telegram."
        return "Календарь подключён. Можно вернуться в Telegram."

    @staticmethod
    def _oauth_denied_message(product: str) -> str:
        if product == "workspace":
            return "Google Workspace не подключён: доступ не был подтверждён."
        return "Календарь не подключён: доступ не был подтверждён."

    @staticmethod
    def _oauth_error_message(product: str) -> str:
        if product == "workspace":
            return (
                "Google Workspace не подключился: Google вернул ошибку авторизации. "
                "Попробуйте запросить новую ссылку."
            )
        return (
            "Календарь не подключился: Google вернул ошибку авторизации. "
            "Попробуйте запросить новую ссылку."
        )

    @staticmethod
    def _gmail_headers(message: dict[str, Any]) -> dict[str, str]:
        headers = (message.get("payload") or {}).get("headers") or []
        return {
            str(header.get("name") or ""): str(header.get("value") or "")
            for header in headers
            if header.get("name")
        }

    @classmethod
    def _extract_gmail_body(cls, message: dict[str, Any]) -> str:
        payload = message.get("payload") or {}
        plain = cls._find_gmail_part(payload, "text/plain")
        if plain:
            return plain
        html_body = cls._find_gmail_part(payload, "text/html")
        if html_body:
            return cls._html_to_text(html_body)
        return ""

    @classmethod
    def _find_gmail_part(cls, part: dict[str, Any], mime_type: str) -> str:
        if part.get("mimeType") == mime_type:
            data = (part.get("body") or {}).get("data")
            if data:
                return cls._decode_b64url(data)
        for child in part.get("parts") or []:
            found = cls._find_gmail_part(child, mime_type)
            if found:
                return found
        return ""

    @classmethod
    def _iter_gmail_parts(cls, part: dict[str, Any]):
        yield part
        for child in part.get("parts") or []:
            yield from cls._iter_gmail_parts(child)

    @staticmethod
    def _decode_b64url(value: str) -> str:
        try:
            raw = GoogleCalendarRuntime._decode_b64url_bytes(value)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _decode_b64url_bytes(value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        try:
            return base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception:
            return b""

    @staticmethod
    def _html_to_text(value: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", value)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p\s*>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _extract_attachment_text(cls, filename: str, mime_type: str, raw: bytes) -> dict[str, str]:
        if not raw:
            return {"text": "", "status": "empty"}
        name = filename.lower()
        mime = mime_type.lower()
        if mime == "text/html" or name.endswith((".html", ".htm")):
            text = raw.decode("utf-8", errors="replace")
            return {"text": cls._html_to_text(text), "status": "ok"}
        if mime.startswith("text/") or name.endswith((".txt", ".csv", ".md", ".json", ".xml")):
            text = raw.decode("utf-8", errors="replace")
            return {"text": text.strip(), "status": "ok"}
        if name.endswith(".docx") or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            text = cls._extract_docx_text(raw)
            return {"text": text, "status": "ok" if text else "empty_docx_text"}
        if name.endswith(".pdf") or mime == "application/pdf":
            text = cls._extract_pdf_text(raw)
            return {"text": text, "status": "ok" if text else "pdf_text_unavailable"}
        return {"text": "", "status": "unsupported_type"}

    @staticmethod
    def _extract_docx_text(raw: bytes) -> str:
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                document = archive.read("word/document.xml").decode("utf-8", errors="replace")
        except Exception:
            return ""
        text = re.sub(r"<[^>]+>", " ", document)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _extract_pdf_text(raw: bytes) -> str:
        try:
            from pypdf import PdfReader
        except Exception:
            try:
                from PyPDF2 import PdfReader  # type: ignore
            except Exception:
                return ""
        try:
            reader = PdfReader(BytesIO(raw))
            pages = []
            for page in reader.pages[:25]:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text.strip())
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

    @classmethod
    def _extract_doc_text(cls, node: Any) -> str:
        parts: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                text_run = value.get("textRun")
                if isinstance(text_run, dict) and text_run.get("content"):
                    parts.append(str(text_run["content"]))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(node.get("body", {}) if isinstance(node, dict) else node)
        return "".join(parts)

    @staticmethod
    def _drive_query_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _extract_doc_id(value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        match = re.search(r"/document/d/([A-Za-z0-9_-]+)", text)
        if match:
            return match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
            return text
        return ""

    @staticmethod
    def _truncate_text(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n\n... <truncated {len(value) - limit} chars>"

    def _pending_path(self, state: str) -> Path:
        return self.pending_dir / _safe_filename(state)

    def _user_path(self, user_id: str) -> Path:
        return self.users_dir / _safe_filename(user_id)

    def _load_pending(self, state: str) -> PendingOAuth | None:
        if not state:
            return None
        path = self._pending_path(state)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("created_at") or 0) > 1800:
                path.unlink(missing_ok=True)
                return None
            return PendingOAuth(**data)
        except Exception:
            return None

    def _load_user_token(self, user_id: str) -> dict[str, Any] | None:
        if not user_id:
            return None
        path = self._user_path(user_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    async def _send_telegram(self, chat_id: str, text: str) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or not chat_id:
            return
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=20),
            )

    def _html_response(self, message: str, *, status: int = 200) -> web.Response:
        safe = html.escape(message)
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Гига Помощник</title>"
            "<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
            "max-width:640px;margin:64px auto;line-height:1.5'>"
            f"<h2>{safe}</h2><p>Эту вкладку можно закрыть.</p></body>"
        )
        return web.Response(text=body, status=status, content_type="text/html")


async def delayed_cleanup_pending(runtime: GoogleCalendarRuntime) -> None:
    while True:
        try:
            now = time.time()
            for path in runtime.pending_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if now - float(data.get("created_at") or 0) > 1800:
                        path.unlink(missing_ok=True)
                except Exception:
                    path.unlink(missing_ok=True)
        except Exception:
            logger.debug("google calendar pending cleanup failed", exc_info=True)
        await asyncio.sleep(300)
