"""Tests for Google OAuth tool user-facing output."""

from tools import calendar_tool, google_workspace_tool


def test_calendar_connect_hides_raw_auth_url_from_model(monkeypatch):
    monkeypatch.setattr(
        calendar_tool,
        "_http_json",
        lambda *_args, **_kwargs: {
            "success": True,
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc",
            "public_message": (
                "Чтобы подключить Google Calendar, нажмите: "
                "[Открыть подключение Google](https://accounts.google.com/o/oauth2/v2/auth?client_id=abc)"
            ),
        },
    )

    result = calendar_tool.calendar_tool({"action": "connect"})

    assert '"auth_url"' not in result
    assert "do not expand it into a raw URL" in result


def test_workspace_connect_hides_raw_auth_url_from_model(monkeypatch):
    monkeypatch.setattr(
        google_workspace_tool,
        "_http_json",
        lambda *_args, **_kwargs: {
            "success": True,
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc",
            "public_message": (
                "Чтобы подключить Google Почту, Документы и календарь, нажмите: "
                "[Открыть подключение Google](https://accounts.google.com/o/oauth2/v2/auth?client_id=abc)"
            ),
        },
    )

    result = google_workspace_tool.google_workspace_tool({"action": "connect"})

    assert '"auth_url"' not in result
    assert "do not expand it into a raw URL" in result
