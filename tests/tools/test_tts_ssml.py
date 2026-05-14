"""Tests for the SSML helpers and Sber SaluteSpeech SSML emission path."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "SBER_SALUTE_AUTH_KEY",
        "SBER_SALUTE_OAUTH_URL",
        "SBER_SALUTE_SYNTH_URL",
        "SBER_SALUTE_TTS_CONTENT_TYPE",
        "SBER_SALUTE_TTS_VOICE",
        "HERMES_TTS_SSML_ENABLED",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)
    # Reset the Sber token cache between tests so each one fetches its own.
    from tools import tts_tool
    tts_tool._sber_tts_token = ""
    tts_tool._sber_tts_token_scope = ""
    tts_tool._sber_tts_token_expires_at = 0.0


# ===========================================================================
# Helpers: _contains_ssml / _strip_ssml / _ensure_ssml_envelope
# ===========================================================================
class TestContainsSsml:
    def test_speak_tag(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml("<speak>hi</speak>") is True

    def test_self_closing_break(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml('Привет <break time="500ms"/> мир') is True

    def test_prosody_with_attrs(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml('<prosody rate="fast">быстро</prosody>') is True

    def test_emphasis(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml("<emphasis level='strong'>важно</emphasis>") is True

    def test_say_as_hyphenated_tag(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml('<say-as interpret-as="digits">123</say-as>') is True

    def test_plain_text(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml("Просто текст без разметки") is False

    def test_non_ssml_angle_brackets(self):
        from tools.tts_tool import _contains_ssml
        # Generic XML-ish tags that aren't in our SSML vocabulary
        assert _contains_ssml("a < b and c > d") is False
        assert _contains_ssml("<unknown>foo</unknown>") is False

    def test_empty_and_none(self):
        from tools.tts_tool import _contains_ssml
        assert _contains_ssml("") is False
        assert _contains_ssml(None) is False  # type: ignore[arg-type]


class TestStripSsml:
    def test_removes_tags_preserves_text(self):
        from tools.tts_tool import _strip_ssml
        out = _strip_ssml('<speak>Привет, <break time="500ms"/>мир!</speak>')
        assert out == "Привет, мир!"

    def test_decodes_entities(self):
        from tools.tts_tool import _strip_ssml
        out = _strip_ssml("<speak>5 &lt; 10 &amp; 20 &gt; 1</speak>")
        assert out == "5 < 10 & 20 > 1"

    def test_nested_tags(self):
        from tools.tts_tool import _strip_ssml
        out = _strip_ssml(
            '<speak><prosody rate="slow">медленно</prosody> '
            '<emphasis level="strong">сильно</emphasis></speak>'
        )
        assert out == "медленно сильно"

    def test_collapses_whitespace(self):
        from tools.tts_tool import _strip_ssml
        # Tags between words leave the surrounding text untouched, so
        # spaces collapse but the tag itself is just a delete; "<break/>c"
        # produces "c" (no inserted gap).
        out = _strip_ssml("<speak>a <break/>   b   <break/> c</speak>")
        assert out == "a b c"

    def test_empty_returns_empty(self):
        from tools.tts_tool import _strip_ssml
        assert _strip_ssml("") == ""


class TestEnsureSsmlEnvelope:
    def test_wraps_bare_ssml(self):
        from tools.tts_tool import _ensure_ssml_envelope
        out = _ensure_ssml_envelope('Привет <break time="500ms"/>мир')
        assert out.startswith("<speak>") and out.endswith("</speak>")
        assert "<break" in out

    def test_idempotent_on_already_wrapped(self):
        from tools.tts_tool import _ensure_ssml_envelope
        src = '<speak>Привет <break time="500ms"/>мир</speak>'
        assert _ensure_ssml_envelope(src) == src

    def test_passthrough_plain_text(self):
        from tools.tts_tool import _ensure_ssml_envelope
        assert _ensure_ssml_envelope("Просто текст") == "Просто текст"

    def test_passthrough_speak_with_leading_whitespace(self):
        from tools.tts_tool import _ensure_ssml_envelope
        src = "  <speak>x</speak>"
        # Leading whitespace doesn't trigger re-wrapping
        assert _ensure_ssml_envelope(src) == src


# ===========================================================================
# Feature flag: _ssml_feature_enabled
# ===========================================================================
class TestSsmlFeatureEnabled:
    def test_default_off(self, monkeypatch):
        # No env, no config override
        with patch("tools.tts_tool._load_tts_config", return_value={}):
            from tools.tts_tool import _ssml_feature_enabled
            assert _ssml_feature_enabled() is False

    def test_env_on(self, monkeypatch):
        monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", "1")
        from tools.tts_tool import _ssml_feature_enabled
        assert _ssml_feature_enabled() is True

    def test_env_truthy_values(self, monkeypatch):
        from tools.tts_tool import _ssml_feature_enabled
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", value)
            assert _ssml_feature_enabled() is True, value

    def test_env_false_wins_over_config_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", "0")
        with patch(
            "tools.tts_tool._load_tts_config",
            return_value={"ssml_enabled": True},
        ):
            from tools.tts_tool import _ssml_feature_enabled
            assert _ssml_feature_enabled() is False

    def test_config_on(self, monkeypatch):
        with patch(
            "tools.tts_tool._load_tts_config",
            return_value={"ssml_enabled": True},
        ):
            from tools.tts_tool import _ssml_feature_enabled
            assert _ssml_feature_enabled() is True

    def test_config_load_error_returns_false(self):
        with patch("tools.tts_tool._load_tts_config", side_effect=RuntimeError("boom")):
            from tools.tts_tool import _ssml_feature_enabled
            assert _ssml_feature_enabled() is False


# ===========================================================================
# Sber SaluteSpeech: wrap + retry behaviour
# ===========================================================================
def _ok_token_response():
    import time
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "tok-123",
        "expires_at": int((time.time() + 1800) * 1000),
    }
    return response


def _ok_audio_response():
    response = MagicMock()
    response.status_code = 200
    response.content = b"OGGOPUS-DATA"
    response.headers = {"Content-Type": "audio/ogg;codecs=opus"}
    return response


def _err_response(status: int, body: str):
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.content = body.encode("utf-8")
    response.headers = {"Content-Type": "application/json"}
    return response


class TestSberSaluteSsmlPath:
    def test_ssml_body_wrapped_and_sent_as_ssml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", "1")
        out = str(tmp_path / "out.ogg")
        body = 'Привет <break time="500ms"/>мир'

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_audio_response()]
            from tools.tts_tool import _generate_sber_salute_tts
            _generate_sber_salute_tts(body, out, {})

        synth_call = mock_post.call_args_list[1]
        sent_body = synth_call.kwargs["data"].decode("utf-8")
        assert sent_body.startswith("<speak>") and sent_body.endswith("</speak>")
        assert synth_call.kwargs["headers"]["Content-Type"] == "application/ssml"

    def test_ssml_disabled_keeps_raw_text(self, tmp_path, monkeypatch):
        # Even with SSML in the body, when the feature is OFF we don't
        # auto-wrap. The legacy auto-detect (text starts with <speak>) still
        # picks ssml content-type — but a body that *starts* with plain text
        # plus inline tags would go as application/text.
        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        # HERMES_TTS_SSML_ENABLED unset → off
        out = str(tmp_path / "out.ogg")
        body = 'Привет <break time="500ms"/>мир'

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_audio_response()]
            from tools.tts_tool import _generate_sber_salute_tts
            _generate_sber_salute_tts(body, out, {})

        synth_call = mock_post.call_args_list[1]
        assert synth_call.kwargs["data"].decode("utf-8") == body
        assert synth_call.kwargs["headers"]["Content-Type"] == "application/text"

    def test_400_on_ssml_triggers_plain_text_retry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", "1")
        out = str(tmp_path / "out.ogg")
        body = 'Привет <break time="500ms"/>мир'

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _ok_token_response(),
                _err_response(400, '{"status":400,"message":"invalid SSML"}'),
                _ok_audio_response(),
            ]
            from tools.tts_tool import _generate_sber_salute_tts
            _generate_sber_salute_tts(body, out, {})

        # Three POSTs: OAuth, SSML attempt, plain-text retry
        assert mock_post.call_count == 3
        retry_call = mock_post.call_args_list[2]
        assert retry_call.kwargs["headers"]["Content-Type"] == "application/text"
        sent_body = retry_call.kwargs["data"].decode("utf-8")
        # Tags stripped, plain text preserved
        assert "<break" not in sent_body
        assert "Привет" in sent_body and "мир" in sent_body

    def test_400_non_ssml_body_does_not_retry(self, tmp_path, monkeypatch):
        # Plain text body + HTTP 400 → no retry, original error raised.
        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        out = str(tmp_path / "out.ogg")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _ok_token_response(),
                _err_response(400, "boom"),
            ]
            from tools.tts_tool import _generate_sber_salute_tts
            with pytest.raises(ValueError, match="HTTP 400"):
                _generate_sber_salute_tts("просто текст", out, {})

        assert mock_post.call_count == 2  # OAuth + one failed synth, no retry


# ===========================================================================
# Dispatcher: provider-aware SSML stripping
# ===========================================================================
class TestProviderAwareSsmlStrip:
    def test_openai_receives_stripped_text(self, tmp_path, monkeypatch):
        # When provider != sber_salute, SSML tags get stripped before dispatch.
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-fake")
        out = str(tmp_path / "out.mp3")

        body = '<speak>Привет <break time="500ms"/>мир</speak>'
        captured: dict = {}

        def fake_openai_tts(text, output_path, _cfg):
            captured["text"] = text
            from pathlib import Path
            Path(output_path).write_bytes(b"MP3DATA")
            return output_path

        with patch(
            "tools.tts_tool._load_tts_config",
            return_value={"provider": "openai", "openai": {"voice": "alloy"}},
        ), patch("tools.tts_tool._import_openai_client"), patch(
            "tools.tts_tool._generate_openai_tts", side_effect=fake_openai_tts
        ):
            from tools.tts_tool import text_to_speech_tool
            text_to_speech_tool(text=body, output_path=out)

        assert "<speak" not in captured["text"]
        assert "<break" not in captured["text"]
        assert "Привет" in captured["text"] and "мир" in captured["text"]

    def test_sber_salute_keeps_ssml(self, tmp_path, monkeypatch):
        # When provider == sber_salute, the dispatcher must NOT strip SSML.
        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        monkeypatch.setenv("HERMES_TTS_SSML_ENABLED", "1")
        out = str(tmp_path / "out.ogg")
        body = '<speak>Привет <break time="500ms"/>мир</speak>'

        with patch(
            "tools.tts_tool._load_tts_config",
            return_value={
                "provider": "sber_salute",
                "sber_salute": {"voice": "Nec_24000", "format": "opus"},
                "ssml_enabled": True,
            },
        ), patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_audio_response()]
            from tools.tts_tool import text_to_speech_tool
            text_to_speech_tool(text=body, output_path=out)

        synth_call = mock_post.call_args_list[1]
        sent = synth_call.kwargs["data"].decode("utf-8")
        assert sent == body
        assert synth_call.kwargs["headers"]["Content-Type"] == "application/ssml"
