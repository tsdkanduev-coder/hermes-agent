"""Tests for Google Workspace runtime helpers."""

import base64

from gateway.google_calendar_runtime import GoogleCalendarRuntime


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
