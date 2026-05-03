"""Tests for Google Workspace runtime helpers."""

import base64

from gateway.google_calendar_runtime import (
    CALENDAR_EVENTS_SCOPE,
    CALENDAR_WRITE_SCOPES,
    GoogleCalendarRuntime,
    _has_any_scope,
    _parse_event_datetime,
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_extract_gmail_plain_body_from_nested_parts():
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>HTML fallback</p>")},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Привет.\nТест письма.")},
                },
            ],
        }
    }

    assert GoogleCalendarRuntime._extract_gmail_body(message) == "Привет.\nТест письма."


def test_extract_gmail_html_body_when_plain_missing():
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64url("<p>Первый</p><br><p>Второй</p>")},
        }
    }

    assert "Первый" in GoogleCalendarRuntime._extract_gmail_body(message)
    assert "Второй" in GoogleCalendarRuntime._extract_gmail_body(message)


def test_extract_google_doc_id_from_url_or_id():
    doc_id = "abcDEF_12345678901234567890"

    assert (
        GoogleCalendarRuntime._extract_doc_id(
            f"https://docs.google.com/document/d/{doc_id}/edit"
        )
        == doc_id
    )
    assert GoogleCalendarRuntime._extract_doc_id(doc_id) == doc_id


def test_extract_google_doc_text_recursively():
    doc = {
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Hello"}}]}},
                {"table": {"tableRows": [{"tableCells": [{"content": [
                    {"paragraph": {"elements": [{"textRun": {"content": " world"}}]}}
                ]}]}]}},
            ]
        }
    }

    assert GoogleCalendarRuntime._extract_doc_text(doc) == "Hello world"


def test_calendar_write_scope_detection_accepts_events_scope():
    assert _has_any_scope(CALENDAR_EVENTS_SCOPE, CALENDAR_WRITE_SCOPES)
    assert not _has_any_scope(
        "https://www.googleapis.com/auth/calendar.readonly",
        CALENDAR_WRITE_SCOPES,
    )


def test_parse_event_datetime_requires_time_and_applies_timezone():
    parsed = _parse_event_datetime("2026-04-27T15:30:00", "Europe/Moscow")

    assert parsed.tzinfo is not None
    assert parsed.isoformat().startswith("2026-04-27T15:30:00")


def test_attendees_from_value_filters_non_emails_and_deduplicates():
    attendees = GoogleCalendarRuntime._attendees_from_value(
        [
            {"email": "a@example.com", "displayName": "A"},
            {"email": "A@example.com"},
            {"email": "not-an-email"},
            "b@example.com",
        ]
    )

    assert attendees == [
        {"email": "a@example.com", "displayName": "A"},
        {"email": "b@example.com"},
    ]


def test_connect_public_message_uses_short_markdown_link():
    runtime = GoogleCalendarRuntime.__new__(GoogleCalendarRuntime)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc&scope=calendar"

    message = runtime._connect_public_message(auth_url, "workspace")

    assert "[Открыть подключение Google](" in message
    assert message.count(auth_url) == 1
    assert f"доступ:\n{auth_url}" not in message
    assert "Google Почту, Документы и календарь" in message
