"""The final speech boundary must be safer than the text shown in the log."""

import asyncio

from ai_race_engineer import pronounce
from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.tts import TTS
from ai_race_engineer.tts_cache import TTSCache


class _SpeechResponse:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _pcm(marker=1):
    return bytes((marker, 0)) * 2400


def test_display_notation_is_written_for_the_ear_before_synthesis():
    spoken = pronounce.for_speech(
        "You're P4; Jobling is 1.42 seconds ahead, #07 is behind. "
        "Last lap 1:42.315, speed 220 km/h, FL 91°C, 172 kPa, 6800 RPM."
    )

    assert "position four" in spoken
    assert "one point four two seconds" in spoken
    assert "car zero seven" in spoken
    assert "one minute forty two point three one five seconds" in spoken
    assert "two hundred twenty kilometres per hour" in spoken
    assert "front left ninety one degrees Celsius" in spoken
    assert "one hundred seventy two kilopascals" in spoken
    assert "six thousand eight hundred revolutions per minute" in spoken
    assert not any(ch.isdigit() for ch in spoken), spoken


def test_the_normalized_text_is_what_elevenlabs_and_the_cache_receive(tmp_path):
    sent = []
    client = TTS("key", cache=TTSCache(tmp_path))

    def post(url, json=None, params=None, headers=None):
        sent.append(json["text"])
        return _SpeechResponse(_pcm())

    client._client.post = post
    text = "You're P4, 1.42 seconds behind."

    first = client.synthesize(text)
    second = client.synthesize(text)

    assert first == second == _pcm()
    assert sent == ["You're position four, one point four two seconds behind."]


def test_language_and_text_normalization_are_sent_and_affect_the_cache(tmp_path):
    payloads = []
    client = TTS("key", language="EN-gb", cache=TTSCache(tmp_path))
    other_language = TTS("key", language="pl", cache=TTSCache(tmp_path))

    def post(url, json=None, params=None, headers=None):
        payloads.append(json)
        return _SpeechResponse(_pcm())

    client._client.post = post
    other_language._client.post = post
    client.synthesize("DRS is available.")
    other_language.synthesize("DRS is available.")

    assert payloads[0]["text"] == "D R S is available."
    assert payloads[0]["language_code"] == "en"
    assert payloads[0]["apply_text_normalization"] == "on"
    assert payloads[1]["language_code"] == "pl"


def test_regeneration_replaces_only_the_requested_cached_phrase(tmp_path):
    sent = []
    client = TTS("key", cache=TTSCache(tmp_path))

    def post(url, json=None, params=None, headers=None):
        sent.append(json["text"])
        return _SpeechResponse(_pcm(len(sent)))

    client._client.post = post
    first = client.synthesize("You're P4.")
    other = client.synthesize("Fuel margin 2.1 litres.")
    assert len(sent) == 2

    replacement = client.synthesize("You're P4.", regenerate=True)
    assert replacement != first
    assert len(sent) == 3

    assert client.synthesize("Fuel margin 2.1 litres.") == other
    assert len(sent) == 3, "regenerating one reply evicted another cached phrase"


def test_natural_repeat_phrases_are_recognised_as_radio_controls():
    from ai_race_engineer.orchestrator import _is_repeat_request

    for phrase in ("say again", "Repeat that!", "could you repeat that?",
                   "that was gibberish", "your voice glitched", "bad audio"):
        assert _is_repeat_request(phrase), phrase
    assert not _is_repeat_request("what is my position")


def test_say_again_regenerates_the_last_spoken_text_without_the_llm(monkeypatch):
    from ai_race_engineer import orchestrator as orchestrator_module

    orch = Orchestrator(AppConfig({
        "telemetry": {"source": "simulated"},
        "elevenlabs": {"api_key": "key"},
    }))
    orch._last_spoken_text = "You're P4."
    calls = []

    async def speak(text, regenerate=False):
        calls.append((text, regenerate))
        return True

    def fail_if_called(*args):
        raise AssertionError("repeat reached the LLM")

    monkeypatch.setattr(orch, "_speak", speak)
    monkeypatch.setattr(orchestrator_module, "audio_available", lambda: True)
    monkeypatch.setattr(orch.llm, "generate", fail_if_called)

    result = asyncio.run(orch.ask("say again", speak=True, kind="voice"))
    assert result["answer"] == "You're P4."
    assert calls == [("You're P4.", True)]


def test_low_confidence_say_again_is_not_rejected_as_off_topic():
    from ai_race_engineer.stt import Transcription, should_repeat

    heard = Transcription(
        text="say again", language_probability=0.2, word_logprobs=(-2.0, -2.0))
    assert should_repeat(heard) is False


def test_low_confidence_gibberish_complaint_is_not_rejected_as_off_topic():
    from ai_race_engineer.stt import Transcription, should_repeat

    heard = Transcription(
        text="that was gibberish", language_probability=0.2,
        word_logprobs=(-2.0, -2.0, -2.0))
    assert should_repeat(heard) is False


def test_malformed_audio_is_not_cached(tmp_path):
    client = TTS("key", cache=TTSCache(tmp_path))
    responses = iter((b"short", _pcm()))
    client._client.post = lambda *args, **kwargs: _SpeechResponse(next(responses))

    import pytest
    from ai_race_engineer.tts import TTSError

    with pytest.raises(TTSError, match="truncated"):
        client.synthesize("Box this lap.")
    assert client.synthesize("Box this lap.") == _pcm()
