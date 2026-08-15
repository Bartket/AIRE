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
