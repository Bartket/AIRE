"""Failures at the speech/session boundary must not reach the driver's ears."""

from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.stt import STT, Transcription, should_repeat
from ai_race_engineer.telemetry import CarTelemetry, TelemetrySnapshot, TrackConfig


def test_stt_keeps_confidence_metadata_instead_of_throwing_it_away():
    result = STT._parse_result({
        "text": "Am I gaining?",
        "language_code": "en",
        "language_probability": 0.97,
        "words": [
            {"type": "word", "text": "Am", "logprob": -0.1},
            {"type": "spacing", "text": " ", "logprob": -9.0},
            {"type": "word", "text": "gaining", "logprob": -0.3},
        ],
    })

    assert result.text == "Am I gaining?"
    assert result.language_probability == 0.97
    assert result.mean_word_logprob == -0.2


def test_only_low_confidence_off_domain_transcripts_are_rejected():
    weak = Transcription("Am I going to the garden", "en", 0.9, (-1.4, -1.2))
    racing = Transcription("Am I gaining on the car ahead", "en", 0.9,
                           (-1.4, -1.2))
    clear = Transcription("How are you", "en", 0.9, (-0.1, -0.2))

    assert should_repeat(weak) is True
    assert should_repeat(racing) is False
    assert should_repeat(clear) is False


def test_llm_memory_resets_when_the_session_segment_changes(tmp_path):
    config = AppConfig({"telemetry": {"source": "simulated"}},
                       path=tmp_path / "config.json")
    orch = Orchestrator(config)
    first = TelemetrySnapshot(
        CarTelemetry(),
        TrackConfig(session_id=100, subsession_id=200, session_num=0,
                    session_generation=0),
    )
    race = TelemetrySnapshot(
        CarTelemetry(),
        TrackConfig(session_id=100, subsession_id=200, session_num=1,
                    session_generation=0),
    )

    orch._adopt_session(first)
    orch.llm._history.append(("Practice question", "Practice answer"))
    orch._adopt_session(first)
    assert orch.llm._history

    orch._adopt_session(race)
    assert not orch.llm._history


def test_llm_memory_resets_on_a_same_id_session_restart(tmp_path):
    config = AppConfig({"telemetry": {"source": "simulated"}},
                       path=tmp_path / "config.json")
    orch = Orchestrator(config)
    before = TelemetrySnapshot(
        CarTelemetry(),
        TrackConfig(session_id=0, subsession_id=0, session_num=0,
                    session_generation=0),
    )
    restarted = TelemetrySnapshot(
        CarTelemetry(),
        TrackConfig(session_id=0, subsession_id=0, session_num=0,
                    session_generation=1),
    )

    orch._adopt_session(before)
    orch.llm._history.append(("Old run", "Old answer"))
    orch._adopt_session(restarted)

    assert not orch.llm._history


def test_cache_counters_are_read_back_rather_than_assumed():
    """Prompt caching fails silently when the prefix is under the minimum.

    Claude Haiku 4.5 will not cache below 4,096 tokens and AIRE's static
    prefix is around 1,340, so markers would be accepted and nothing would
    cache — with no error to say so. That makes `cached` a number to read,
    not a saving to assume, which is what this records.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient

    client = LLMClient(AppConfig())
    client.reset_usage()
    client._record_usage({
        "prompt_tokens": 2300, "completion_tokens": 41,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    })
    # A tool round trip re-sends the same prefix, so counters accumulate.
    client._record_usage({
        "prompt_tokens": 2400, "completion_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 0},
    })

    usage = client.last_usage
    assert usage["calls"] == 2
    assert usage["prompt"] == 4700
    assert usage["cached"] == 0

    # A provider that reports nothing must not be recorded as zero tokens:
    # absent and zero are different readings and only one of them is a fact.
    client.reset_usage()
    client._record_usage({"prompt_tokens": 100})
    assert "cached" not in client.last_usage
    client._record_usage(None)
    assert client.last_usage["prompt"] == 100


def test_a_billed_call_is_counted_even_when_its_body_carries_an_error():
    """OpenRouter reports upstream provider failures inside a 200 body.

    The call still reached the provider and may still have been billed, so
    counting it only on the success path understates what a question cost —
    and understating is the wrong direction to be wrong in when the whole
    point of these counters is deciding what to optimise.
    """
    import pytest

    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient, LLMError

    class _Response:
        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {
                "error": {"message": "upstream is down"},
                "usage": {"prompt_tokens": 2300, "completion_tokens": 0},
            }

    config = AppConfig({"llm": {"openrouter": {"api_key": "test-key"}}})
    client = LLMClient(config)
    client._client.post = lambda *_a, **_k: _Response()
    client.reset_usage()

    with pytest.raises(LLMError, match="upstream is down"):
        client._chat_raw([{"role": "user", "content": "Hi"}])

    assert client.last_usage["prompt"] == 2300
    assert client.last_usage["calls"] == 1


def test_hidden_reasoning_still_earns_the_retry_with_room():
    """Some models think with the reasoning text withheld.

    Claude Haiku 5.5 through OpenRouter returns no reasoning text, only a
    count. The retry was keyed on the text, so a forced final answer that
    spent its whole 80-token budget thinking came back empty and the driver
    heard "I lost that one, say again" — measured once in eighteen live runs.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient

    budgets = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    def post(_url, json=None, **_kw):
        budgets.append(json["max_tokens"])
        if len(budgets) == 1:
            # The whole budget went on thinking nobody can see.
            return _Response({
                "choices": [{"finish_reason": "length",
                             "message": {"content": ""}}],
                "usage": {"completion_tokens": 80,
                          "completion_tokens_details": {"reasoning_tokens": 80}},
            })
        return _Response({"choices": [{"finish_reason": "stop", "message": {
            "content": "You're five point nine behind the leader."}}]})

    client = LLMClient(AppConfig({"llm": {"openrouter": {"api_key": "x"}}}))
    client._client.post = post

    reply = client._chat([{"role": "user", "content": "gap to the leader?"}],
                         max_tokens=80)

    assert reply == "You're five point nine behind the leader."
    assert budgets[0] == 80 and budgets[1] > 80

    # A model that says nothing and reports no reasoning is still a different
    # fault, and still not retried — the existing rule is unchanged.
    budgets.clear()
    silent = LLMClient(AppConfig({"llm": {"openrouter": {"api_key": "x"}}}))
    silent._client.post = lambda _u, json=None, **_k: (budgets.append(json["max_tokens"]), _Response({
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"completion_tokens": 80,
                  "completion_tokens_details": {"reasoning_tokens": 0}}}))[1]
    assert silent._chat([{"role": "user", "content": "?"}], max_tokens=80) == ""
    assert budgets == [80]
