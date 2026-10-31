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


def _scripted(replies):
    """An LLMClient whose provider answers from a list, one per request."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient

    class _Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    queue = list(replies)
    client = LLMClient(AppConfig({"llm": {"openrouter": {"api_key": "x"}}}))
    client._client.post = lambda *_a, **_k: _Response(
        {"choices": [queue.pop(0)]})
    return client


def _leader_snapshot():
    from ai_race_engineer.telemetry import Rival

    field = [Rival(name="Rossi", car_number="13", position=1, gap=None),
             Rival(name="You", car_number="22", position=4, gap=5.89,
                   is_player=True)]
    return TelemetrySnapshot(CarTelemetry(position=4),
                             TrackConfig(session_type="Race", field=field))


def test_a_model_that_falls_silent_after_its_tool_is_answered_by_the_tool():
    """Measured live: Haiku 4.5 went 5 for 5 on "how far behind the leader".

    It answers in the same turn as the tool call, then returns nothing after
    the result, having already said it. Only the final turn was spoken, so
    the driver heard "I lost that one, say again" every time — and it took a
    working gap_to to expose it, because a failing one made the model try
    again in words.
    """
    from ai_race_engineer import race_math

    snapshot = _leader_snapshot()
    client = _scripted([
        {"finish_reason": "tool_calls", "message": {
            "content": "You're about six seconds back.",
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "gap_to", "arguments": '{"target": "leader"}'}}]}},
        {"finish_reason": "stop", "message": {"content": ""}},
    ])

    answer = client.generate("How far am I behind the leader?", snapshot)

    computed = race_math.gap_to(snapshot.car, snapshot.track, "leader")["spoken"]
    assert answer == computed
    # Not the model's text from before the tool ran: nothing checked its figure.
    assert "six seconds" not in answer


def test_a_real_final_answer_is_never_replaced_by_the_tool_sentence():
    client = _scripted([
        {"finish_reason": "tool_calls", "message": {
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "gap_to", "arguments": '{"target": "leader"}'}}]}},
        {"finish_reason": "stop", "message": {
            "content": "Rossi's five point nine up the road."}},
    ])
    answer = client.generate("How far am I behind the leader?", _leader_snapshot())
    assert answer == "Rossi's five point nine up the road."


def test_a_sentence_cut_short_by_thinking_is_retried_not_spoken():
    """Measured live: 484 of 512 tokens thinking, then "…so I'd ease off and watch it".

    The few words that leaked meant "it said nothing" was false, so the
    retry never fired and a sentence stopped mid-way went out on air. When
    thinking is most of what the turn spent, the cut answer is the same
    fault as an empty one. A model that rambles to the limit with little
    thinking is not, and is still not retried.
    """
    from ai_race_engineer.llm import _thought_past_the_budget

    cut = {"finish_reason": "length",
           "message": {"content": "Oil pressure's on, so I'd ease off and watch it"}}
    assert _thought_past_the_budget(cut, {
        "completion_tokens": 512,
        "completion_tokens_details": {"reasoning_tokens": 484}})
    assert not _thought_past_the_budget(cut, {
        "completion_tokens": 512,
        "completion_tokens_details": {"reasoning_tokens": 40}})
    assert not _thought_past_the_budget(cut, {"completion_tokens": 512})
    # A turn that ended on its own is never retried, whatever it thought.
    assert not _thought_past_the_budget(
        {"finish_reason": "stop", "message": cut["message"]},
        {"completion_tokens": 512,
         "completion_tokens_details": {"reasoning_tokens": 484}})


# ── What the model says that it did not mean to ─────────────────────────

def _answering(reply, finished=False):
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import CarTelemetry, TelemetrySnapshot, TrackConfig

    client = LLMClient(AppConfig({"driver_callsign": "Bart", "llm": {
        "provider": "local", "use_tools": "off",
        "local": {"endpoint": "http://127.0.0.1:11434/v1", "model": "m"}}}))
    client._chat_raw = lambda messages, max_tokens=None, tools=None, **kw: {
        "content": reply}
    snapshot = TelemetrySnapshot(
        CarTelemetry(), TrackConfig(session_type="Race", finished=finished,
                                    driver_name="Bart Kowal"))
    return client, snapshot


def test_a_draft_and_its_correction_are_not_both_spoken():
    """Measured on air: the model answered, wrote itself a note about the word
    limit, and answered again — and all three paragraphs were spoken."""
    client, snapshot = _answering(
        "Yes, Rossi is quicker by about 0.9 seconds a lap.\n\n"
        "Wait, I need to keep that to one sentence under 25 words.\n\n"
        "Yes, Rossi's about eight tenths quicker on best laps.")

    answer = client.generate("Is Rossi faster than me?", snapshot)

    assert answer == "Yes, Rossi's about eight tenths quicker on best laps."


def test_the_drivers_name_is_not_used_to_address_them_mid_session():
    """In a third of answers, measured — "Eighteen laps to go, Bart"."""
    client, snapshot = _answering("Eighteen laps to go, Bart.")

    assert client.generate("How many laps left?", snapshot) == "Eighteen laps to go."


def test_the_debrief_keeps_the_name_and_its_sentences():
    client, snapshot = _answering(
        "Great drive, Bart.\n\nP4 from P7 is a proper result.", finished=True)

    answer = client.generate("How did that go?", snapshot)

    assert "Bart" in answer and "P4 from P7" in answer


def test_the_app_leaves_reasoning_effort_to_the_model():
    """Measured, not assumed: on Claude Haiku 5.5 `low` was slower on every
    calculator question and dropped more of the model check, and `none`
    stopped calling the calculators. The evals can still send one to measure."""
    from ai_race_engineer.llm import LLMClient

    sent = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": "Copy."}}]}

    def post(_url, json=None, **_kw):
        sent.append(json)
        return _Response()

    config = AppConfig({"llm": {"openrouter": {"api_key": "x"}}})
    app = LLMClient(config)
    app._client.post = post
    app._chat([{"role": "user", "content": "?"}])
    assert "reasoning" not in sent[-1]

    probe = LLMClient(config, reasoning_effort="low")
    probe._client.post = post
    probe._chat([{"role": "user", "content": "?"}])
    assert sent[-1]["reasoning"] == {"effort": "low"}

    local = LLMClient(AppConfig({"llm": {"provider": "local"}}), reasoning_effort="low")
    local._client.post = post
    local._chat([{"role": "user", "content": "?"}])
    assert "reasoning" not in sent[-1]
