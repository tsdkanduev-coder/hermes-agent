"""Tests for the Sber SaluteSpeech STT provider in tools/transcription_tools.py."""

import struct
import wave
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
    ):
        monkeypatch.delenv(key, raising=False)
    from tools import sber_salute_auth
    sber_salute_auth._cache.invalidate()


def _ok_token_response(token="tok-1", expires_in=1800):
    import time
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": token,
        "expires_at": int((time.time() + expires_in) * 1000),
    }
    return response


def _ok_recognize_response(text="привет"):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"result": [text], "status": 200}
    return response


def _make_wav(path, sample_rate=16000, channels=1, sample_width=2, n_frames=16000):
    fmt = "h" if sample_width == 2 else "b" if sample_width == 1 else "i"
    pcm = struct.pack(f"<{n_frames * channels}{fmt}", *([0] * n_frames * channels))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return pcm


class TestPrepareSaluteyPayload:
    def test_wav_16k_mono_strips_header(self, tmp_path):
        from tools.transcription_tools import _prepare_salute_payload

        wav_path = tmp_path / "in.wav"
        pcm = _make_wav(wav_path, sample_rate=16000, channels=1, n_frames=8000)

        body, ct = _prepare_salute_payload(str(wav_path))
        assert ct == "audio/x-pcm;bit=16;rate=16000"
        # The body should be raw PCM, not the original WAV (which is 44 bytes longer)
        assert body == pcm
        assert len(body) < wav_path.stat().st_size

    def test_wav_8k_mono_uses_8k_content_type(self, tmp_path):
        from tools.transcription_tools import _prepare_salute_payload

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=8000, channels=1, n_frames=4000)

        _, ct = _prepare_salute_payload(str(wav_path))
        assert ct == "audio/x-pcm;bit=16;rate=8000"

    def test_wav_24k_passes_rate_to_content_type(self, tmp_path):
        # Sber's own TTS returns 24kHz WAVs; we should not reject them.
        from tools.transcription_tools import _prepare_salute_payload

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=24000, channels=1, n_frames=4000)

        _, ct = _prepare_salute_payload(str(wav_path))
        assert ct == "audio/x-pcm;bit=16;rate=24000"

    def test_wav_8bit_raises(self, tmp_path):
        from tools.sber_salute_auth import SmartSpeechError
        from tools.transcription_tools import _prepare_salute_payload

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=16000, channels=1, sample_width=1, n_frames=400)

        with pytest.raises(SmartSpeechError, match="16-bit"):
            _prepare_salute_payload(str(wav_path))

    def test_mp3_passes_through(self, tmp_path):
        from tools.transcription_tools import _prepare_salute_payload

        mp3 = tmp_path / "in.mp3"
        mp3.write_bytes(b"ID3\x03\x00fakemp3body")

        body, ct = _prepare_salute_payload(str(mp3))
        assert ct == "audio/mpeg"
        assert body == b"ID3\x03\x00fakemp3body"

    def test_ogg_passes_through(self, tmp_path):
        from tools.transcription_tools import _prepare_salute_payload

        ogg = tmp_path / "in.ogg"
        ogg.write_bytes(b"OggS\x00fake")

        body, ct = _prepare_salute_payload(str(ogg))
        assert ct == "audio/ogg;codecs=opus"
        assert body == b"OggS\x00fake"


class TestTranscribeSalute:
    def test_no_credentials_returns_error(self, tmp_path):
        from tools.transcription_tools import _transcribe_salute

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path)

        result = _transcribe_salute(str(wav_path), "salute")
        assert result["success"] is False
        assert "credentials" in result["error"].lower()

    def test_oversize_file_rejected_without_api_call(self, tmp_path, monkeypatch):
        from tools.transcription_tools import _transcribe_salute, SALUTE_MAX_BYTES

        big = tmp_path / "big.mp3"
        big.write_bytes(b"\x00" * (SALUTE_MAX_BYTES + 1))

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        with patch("requests.post") as mock_post:
            result = _transcribe_salute(str(big), "salute")

        assert result["success"] is False
        assert "MB" in result["error"]
        mock_post.assert_not_called()

    def test_successful_transcription(self, tmp_path, monkeypatch):
        from tools.transcription_tools import _transcribe_salute

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=16000, channels=1, n_frames=16000)

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), _ok_recognize_response("привет мир")]
            result = _transcribe_salute(str(wav_path), "salute")

        assert result == {"success": True, "transcript": "привет мир", "provider": "salute"}

        recognize_call = mock_post.call_args_list[1]
        assert recognize_call.args[0] == "https://smartspeech.sber.ru/rest/v1/speech:recognize"
        assert recognize_call.kwargs["headers"]["Authorization"] == "Bearer tok-1"
        assert recognize_call.kwargs["headers"]["Content-Type"] == "audio/x-pcm;bit=16;rate=16000"

    def test_401_triggers_retry(self, tmp_path, monkeypatch):
        from tools.transcription_tools import _transcribe_salute

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=16000, channels=1, n_frames=16000)

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        bad = MagicMock(status_code=401, text="expired")

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _ok_token_response(token="old"),
                bad,
                _ok_token_response(token="new"),
                _ok_recognize_response("ok"),
            ]
            result = _transcribe_salute(str(wav_path), "salute")

        assert result["success"] is True
        assert mock_post.call_count == 4
        retry = mock_post.call_args_list[3]
        assert retry.kwargs["headers"]["Authorization"] == "Bearer new"

    def test_empty_result_returns_failure(self, tmp_path, monkeypatch):
        from tools.transcription_tools import _transcribe_salute

        wav_path = tmp_path / "in.wav"
        _make_wav(wav_path, sample_rate=16000, channels=1, n_frames=16000)

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        empty = MagicMock(status_code=200)
        empty.json.return_value = {"result": [""], "status": 200}

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [_ok_token_response(), empty]
            result = _transcribe_salute(str(wav_path), "salute")

        assert result["success"] is False
        assert "empty" in result["error"].lower()


class TestProviderSelection:
    def test_explicit_salute_with_credentials(self, monkeypatch):
        from tools.transcription_tools import _get_provider

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        assert _get_provider({"provider": "salute", "enabled": True}) == "salute"

    def test_explicit_salute_without_credentials_falls_to_none(self, monkeypatch):
        from tools.transcription_tools import _get_provider

        # No SBER_SALUTE_AUTH_KEY in env
        assert _get_provider({"provider": "salute", "enabled": True}) == "none"

    def test_auto_detect_prefers_salute_when_keyed(self, monkeypatch):
        from tools.transcription_tools import _get_provider

        monkeypatch.setenv("SBER_SALUTE_AUTH_KEY", "abc==")
        # No "provider" key — auto-detect
        assert _get_provider({"enabled": True}) == "salute"
