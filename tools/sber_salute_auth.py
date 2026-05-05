"""
Sber SaluteSpeech authorization helper.

The SaluteSpeech REST API uses a two-step credential flow:

  1. The user generates an *Authorization Key* in the Sber Studio cabinet —
     a base64 string that already encodes ``client_id:client_secret``.
  2. That key is exchanged for a 30-minute *access_token* via
     ``POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth``.
  3. Subsequent calls to ``/rest/v1/speech:recognize`` and
     ``/rest/v1/text:synthesize`` send the access_token as
     ``Authorization: Bearer <token>``.

This module hides that exchange behind ``get_access_token()``.  TTS and STT
modules never touch the OAuth endpoint directly — they request a token, use
it, and on a 401 call ``invalidate_cached_token()`` and retry once.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_SCOPE = "SALUTE_SPEECH_CORP"
# Sber's WAF in front of speech.giga.chat blocks requests without a recognised
# User-Agent. Send the same UA the Sber SDKs use so we don't get rate-limited
# or 403'd. Harmless on the legacy ngw.devices.sberbank.ru endpoint.
USER_AGENT = "GigaVoice"


def get_oauth_url() -> str:
    """OAuth token endpoint (override with ``SBER_SALUTE_OAUTH_URL``)."""
    return (os.getenv("SBER_SALUTE_OAUTH_URL") or DEFAULT_OAUTH_URL).strip() or DEFAULT_OAUTH_URL

# Refresh the cached token when fewer than this many seconds remain on it.
_REFRESH_GRACE_SECONDS = 60


def get_verify() -> "str | bool":
    """Return the value to pass to ``requests``' ``verify=`` argument.

    Sber endpoints are signed by the Russian Trusted Root CA, which is not
    in the default certifi/macOS/Linux trust stores.  Users who hit
    ``SSL: CERTIFICATE_VERIFY_FAILED`` should download the root cert from
    https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt and
    point ``SBER_SALUTE_CA_BUNDLE`` at it.  As a last resort, setting
    ``SBER_SALUTE_INSECURE=1`` disables verification entirely (not
    recommended for production).
    """
    bundle = (os.getenv("SBER_SALUTE_CA_BUNDLE") or "").strip()
    if bundle:
        return bundle
    if (os.getenv("SBER_SALUTE_INSECURE") or "").strip() in ("1", "true", "True", "YES", "yes"):
        return False
    return True


class SmartSpeechError(Exception):
    """Raised when the SaluteSpeech REST API returns an unrecoverable error."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SaluteCredentials:
    """Resolved Sber SaluteSpeech credentials."""

    auth_key: str   # Base64-encoded "client_id:client_secret"
    scope: str = DEFAULT_SCOPE


def get_salute_credentials() -> Optional[SaluteCredentials]:
    """Build credentials from environment variables.

    Accepts either:

      * ``SBER_SALUTE_AUTH_KEY`` — the ready base64 *Authorization Key*
        copied from the Sber Studio cabinet (preferred), or
      * ``SBER_SALUTE_CLIENT_ID`` + ``SBER_SALUTE_CLIENT_SECRET`` — encoded
        on the fly.

    ``SBER_SALUTE_SCOPE`` overrides the default scope
    (``SALUTE_SPEECH_CORP``).  Returns ``None`` when no credentials are
    configured so callers can degrade gracefully.
    """
    scope = (os.getenv("SBER_SALUTE_SCOPE") or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE

    auth_key = (os.getenv("SBER_SALUTE_AUTH_KEY") or "").strip()
    if auth_key:
        return SaluteCredentials(auth_key=auth_key, scope=scope)

    client_id = (os.getenv("SBER_SALUTE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("SBER_SALUTE_CLIENT_SECRET") or "").strip()
    if client_id and client_secret:
        encoded = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        return SaluteCredentials(auth_key=encoded, scope=scope)

    return None


class _TokenCache:
    """Thread-safe access_token cache with grace-period refresh."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0  # Unix seconds (monotonic-ish, server-provided)
        self._scope: Optional[str] = None
        self._auth_key: Optional[str] = None

    def get(self, creds: SaluteCredentials, *, force_refresh: bool = False) -> str:
        """Return a valid access_token, refreshing if needed."""
        with self._lock:
            cred_changed = (
                self._auth_key != creds.auth_key or self._scope != creds.scope
            )
            now = time.time()
            needs_refresh = (
                force_refresh
                or cred_changed
                or self._token is None
                or now >= self._expires_at - _REFRESH_GRACE_SECONDS
            )
            if needs_refresh:
                token, expires_at = _request_token(creds)
                self._token = token
                self._expires_at = expires_at
                self._auth_key = creds.auth_key
                self._scope = creds.scope
            return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0


_cache = _TokenCache()


def get_access_token(*, force_refresh: bool = False) -> str:
    """Return a valid access_token, fetching one if needed.

    Raises ``SmartSpeechError`` if no credentials are configured or the
    OAuth endpoint rejects them.
    """
    creds = get_salute_credentials()
    if creds is None:
        raise SmartSpeechError(
            "Sber SaluteSpeech credentials not configured. Set "
            "SBER_SALUTE_AUTH_KEY (or SBER_SALUTE_CLIENT_ID + "
            "SBER_SALUTE_CLIENT_SECRET) in the environment."
        )
    return _cache.get(creds, force_refresh=force_refresh)


def invalidate_cached_token() -> None:
    """Drop the cached access_token so the next call fetches a fresh one."""
    _cache.invalidate()


def _request_token(creds: SaluteCredentials) -> tuple[str, float]:
    """Exchange the Authorization Key for a fresh access_token."""
    import requests

    rq_uid = str(uuid.uuid4())
    try:
        response = requests.post(
            get_oauth_url(),
            headers={
                "Authorization": f"Basic {creds.auth_key}",
                "RqUID": rq_uid,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            data={"scope": creds.scope},
            timeout=30,
            verify=get_verify(),
        )
    except requests.exceptions.SSLError as exc:
        raise SmartSpeechError(
            "SaluteSpeech TLS handshake failed — Sber uses the Russian Trusted "
            "Root CA, which is not in the default trust store. Download "
            "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt "
            "and point SBER_SALUTE_CA_BUNDLE at it (or set SBER_SALUTE_INSECURE=1 "
            f"to skip verification). Underlying error: {exc}"
        ) from exc

    if response.status_code != 200:
        body = response.text[:300] if response.text else ""
        raise SmartSpeechError(
            f"SaluteSpeech OAuth failed (HTTP {response.status_code}): {body}",
            status_code=response.status_code,
        )

    payload = response.json()
    # Two response shapes in the wild:
    #   * ngw.devices.sberbank.ru → {"access_token": "...", "expires_at": <ms>}
    #   * speech.giga.chat        → {"tok": "...", "exp": <seconds>}
    token = payload.get("access_token") or payload.get("tok")
    if not token:
        raise SmartSpeechError(
            f"SaluteSpeech OAuth response missing access_token: {payload!r}"
        )

    expires_at_raw = payload.get("expires_at")
    if expires_at_raw is None:
        expires_at_raw = payload.get("exp")
    if isinstance(expires_at_raw, (int, float)) and expires_at_raw > 0:
        expires_at = float(expires_at_raw)
        if expires_at > 1e12:  # milliseconds
            expires_at /= 1000.0
    else:
        # No expiry field — assume documented 30-minute TTL.
        expires_at = time.time() + 30 * 60

    logger.debug(
        "SaluteSpeech access_token acquired (scope=%s, expires_in=%ds)",
        creds.scope,
        int(expires_at - time.time()),
    )
    return token, expires_at
