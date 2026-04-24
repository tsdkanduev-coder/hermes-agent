"""Google Calendar OAuth/runtime for managed Telegram deployments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
DEFAULT_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


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
    raw = os.environ.get("GOOGLE_CALENDAR_SCOPES", DEFAULT_SCOPE)
    scopes = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    return scopes or [DEFAULT_SCOPE]


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
                "public_message": (
                    "Чтобы подключить Google Calendar, откройте ссылку и разрешите доступ:\n"
                    f"{auth_url}\n\n"
                    "После подтверждения вернитесь в Telegram — сообщу, когда календарь подключится."
                ),
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

    async def handle_control_disconnect(self, request: web.Request) -> web.Response:
        data = await request.json()
        user_id = str(data.get("user_id") or "").strip()
        path = self._user_path(user_id)
        if path.exists():
            path.unlink()
        return web.json_response({"success": True, "connected": False})

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
                "Календарь не подключён: доступ не был подтверждён.",
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
                "Календарь подключён. Теперь могу смотреть расписание и искать свободные окна.",
            )
            return self._html_response("Календарь подключён. Можно вернуться в Telegram.")
        except Exception as exc:
            logger.exception("google calendar oauth callback failed")
            await self._send_telegram(
                pending.chat_id,
                "Календарь не подключился: Google вернул ошибку авторизации. Попробуйте запросить новую ссылку.",
            )
            return self._html_response(f"Не удалось подключить календарь: {html.escape(str(exc))}", status=500)

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
