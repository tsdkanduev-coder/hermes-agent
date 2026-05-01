from gateway.voice_call_runtime import (
    CallRecord,
    SaluteSpeechTranscriber,
    VoiceCallRuntime,
    _voice_vad_eagerness,
)


def test_voice_transcript_filters_realtime_junk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = VoiceCallRuntime()
    call = CallRecord(call_id="call_test", to="+70000000000", task="Позвонить")

    runtime._add_transcript(call, "callee", "😎", source="openai_realtime")
    runtime._add_transcript(call, "callee", " Алло. ", source="openai_realtime")
    runtime._add_transcript(call, "callee", "Алло.", source="openai_realtime")

    assert len(call.raw_transcript) == 3
    assert call.transcript == [
        {
            "role": "callee",
            "text": "Алло.",
            "source": "openai_realtime",
            "timestamp": call.transcript[0]["timestamp"],
        }
    ]


def test_salute_transcript_extractor_handles_common_payloads():
    assert SaluteSpeechTranscriber._extract_transcript('{"result":["Алло, ресторан."]}') == (
        "Алло, ресторан."
    )
    assert SaluteSpeechTranscriber._extract_transcript(
        '{"results":[{"text":"Добрый день."},{"normalized_text":"Слушаю вас."}]}'
    ) == "Добрый день. Слушаю вас."


def test_voice_vad_defaults_to_labota_fast_turn_taking(monkeypatch):
    monkeypatch.delenv("VOICE_CALL_VAD_EAGERNESS", raising=False)
    assert _voice_vad_eagerness() == "high"

    monkeypatch.setenv("VOICE_CALL_VAD_EAGERNESS", "low")
    assert _voice_vad_eagerness() == "low"

    monkeypatch.setenv("VOICE_CALL_VAD_EAGERNESS", "unexpected")
    assert _voice_vad_eagerness() == "high"


def test_telegram_calendar_link_is_hidden_behind_text_entity():
    text = (
        "Бронь подтверждена.\n\n"
        "📅 Добавить в календарь: "
        "https://calendar.google.com/calendar/render?action=TEMPLATE&text=%D0%A2%D0%B5%D1%81%D1%82"
    )

    visible, entities = VoiceCallRuntime._telegram_calendar_link_entities(text)

    assert visible == "Бронь подтверждена.\n\n📅 Добавить в календарь"
    assert entities == [
        {
            "type": "text_link",
            "offset": len("Бронь подтверждена.\n\n"),
            "length": VoiceCallRuntime._telegram_utf16_len("📅 Добавить в календарь"),
            "url": "https://calendar.google.com/calendar/render?action=TEMPLATE&text=%D0%A2%D0%B5%D1%81%D1%82",
        }
    ]


def test_russian_greeting_name_transliterates_first_latin_token():
    from gateway.run import GatewayRunner

    assert GatewayRunner._russian_greeting_name("Tsevdn Kanduev") == "Цевдн"
    assert GatewayRunner._russian_greeting_name("Павел Богомолов") == "Павел"
