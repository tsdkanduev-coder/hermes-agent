"""Tests for the Sber SaluteSpeech TTS provider in tools/tts_tool.py."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "SBER_SALUTE_AUTH_KEY",
        "SBER_SALUTE_CLIENT_ID",
        "SBER_SALUTE_CLIENT_SECRET",
        "SBER_SALUTE_SCOPE",
        "SBER_SALUTE_BASE_URL",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(key, raising=False)
    # Drop the module-level token cache between tests.
    from tools import sber_salute_auth
    sber_salute_auth._cache.invalidate()


def _ok_audio_response(payload=b"FAKEWAV", content_type="audio/wav"):
    response = MagicMock()
    response.status_code = 200
    response.content = payload
    response.headers = {"Content-Type": content_type}
    return response


def _ok_token_response(token="tok-123", expires_in_seconds=1800):
    import time
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": token,
        # Sber returns ms-since-epoch
        "expires_at": int((time.time() + expires_in_seconds) * 1000),
    }
    return response


class TestGenerateSaluteTts:
    def test_missing_credentials_raises(self, tmp_path):
        from tools.sber_salute_auth import SmartSpeechError
        from tools.tts_tool import _generate_salute_tts

        output_path = str(tmp_path / "out.wav")
        with pytest.raises(SmartSpeechError, match="credentials not configured"):
            _generate_salute_tts("Привет", output_path, {})

    def test_successful_generation_with_auth_key(self, tmp_path, monkeypatch):
        from tools.tts_tool import DEFAULT_SALUTE_VOICE, _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.wav")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _ok_token_response(),
                _ok_audio_response(b"WAVDATA"),
            ]
            result = _generate_salute_tts("Привет, мир!", output_path, {})

        assert result == output_path
        assert (tmp_path / "out.wav").read_bytes() == b"WAVDATA"

        # First call: OAuth (Basic auth, scope, RqUID)
        oauth_call = mock_post.call_args_list[0]
        assert oauth_call.args[0] == "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        assert oauth_call.kwargs["headers"]["Authorization"] == "Basic abc=="
        assert oauth_call.kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert "RqUID" in oauth_call.kwargs["headers"]
        assert oauth_call.kwargs["data"] == {"scope": "SALUTE_SPEECH_CORP"}

        # Second call: text:synthesize with Bearer token, voice, format, UTF-8 body
        synth_call = mock_post.call_args_list[1]
        assert synth_call.args[0] == "https://smartspeech.sber.ru/rest/v1/text:synthesize"
        assert synth_call.kwargs["headers"]["Authorization"] == "Bearer tok-123"
        assert synth_call.kwargs["headers"]["Content-Type"] == "application/text"
        assert synth_call.kwargs["params"] == {"format": "wav16", "voice": DEFAULT_SALUTE_VOICE}
        assert synth_call.kwargs["data"] == "Привет, мир!".encode("utf-8")

    def test_format_picked_from_extension(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.ogg")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_audio_response()]
            _generate_salute_tts("hi", output_path, {})

        synth_call = mock_post.call_args_list[1]
        assert synth_call.kwargs["params"]["format"] == "opus"

    def test_voice_override_from_config(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.wav")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_audio_response()]
            _generate_salute_tts("hi", output_path, {"salute": {"voice": "Tur_24000"}})

        synth_call = mock_post.call_args_list[1]
        assert synth_call.kwargs["params"]["voice"] == "Tur_24000"

    def test_401_triggers_token_refresh_and_retry(self, tmp_path, monkeypatch):
        from tools.tts_tool import _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.wav")

        bad_resp = MagicMock(status_code=401, text="token expired")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _ok_token_response(token="old-tok"),
                bad_resp,
                _ok_token_response(token="new-tok"),
                _ok_audio_response(b"OK"),
            ]
            result = _generate_salute_tts("hi", output_path, {})

        assert result == output_path
        # 4 calls: oauth → synth(401) → oauth(force) → synth(200)
        assert mock_post.call_count == 4
        first_synth = mock_post.call_args_list[1]
        retry_synth = mock_post.call_args_list[3]
        assert first_synth.kwargs["headers"]["Authorization"] == "Bearer old-tok"
        assert retry_synth.kwargs["headers"]["Authorization"] == "Bearer new-tok"

    def test_non_200_raises_smart_speech_error(self, tmp_path, monkeypatch):
        from tools.sber_salute_auth import SmartSpeechError
        from tools.tts_tool import _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.wav")

        bad_resp = MagicMock(status_code=500, text="boom")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), bad_resp]
            with pytest.raises(SmartSpeechError, match="HTTP 500"):
                _generate_salute_tts("hi", output_path, {})

    def test_200_with_json_body_is_rejected(self, tmp_path, monkeypatch):
        # Sber sometimes returns 200 with a JSON error envelope; we must not
        # write that into a .wav file and pretend it succeeded.
        from tools.sber_salute_auth import SmartSpeechError
        from tools.tts_tool import _generate_salute_tts

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        output_path = str(tmp_path / "out.wav")

        json_body = MagicMock(status_code=200, text='{"status":429,"error":"quota"}')
        json_body.headers = {"Content-Type": "application/json"}
        json_body.content = b'{"status":429,"error":"quota"}'

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), json_body]
            with pytest.raises(SmartSpeechError, match="non-audio"):
                _generate_salute_tts("hi", output_path, {})

        assert not (tmp_path / "out.wav").exists()
