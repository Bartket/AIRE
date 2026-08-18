"""Production traces must be replayable without contacting a model."""

from ai_race_engineer.config import AppConfig
from ai_race_engineer.evals import evaluate, grade_record
from ai_race_engineer.telemetry import (CarTelemetry, LapRecord, Rival,
                                        TelemetrySnapshot, TrackConfig)


def _record(answer):
    snapshot = TelemetrySnapshot(
        CarTelemetry(
            position=4,
            ahead=Rival(name="Rossi", gap=1.2, position=3),
            behind=Rival(name="Silva", gap=0.8, position=5),
            closing_ahead_status="confirmed",
            closing_ahead_change=0.2,
            closing_ahead_segments=3,
        ),
        TrackConfig(
            session_id=10, subsession_id=20, session_num=1,
            session_generation=0, session_type="Race",
            laps=[LapRecord(lap=3, time=91.2)],
            field=[Rival(name="Rossi", position=3)],
            fastest_lap=Rival(name="Rossi", best_lap=90.5),
        ),
    )
    return {
        "question": "Am I gaining on the car ahead?",
        "answer": answer,
        "route": "deterministic",
        "snapshot": snapshot.to_dict(),
    }


def test_snapshot_round_trip_rebuilds_nested_dataclasses():
    restored = TelemetrySnapshot.from_dict(_record("Fine.")["snapshot"])

    assert restored.car.ahead.name == "Rossi"
    assert restored.track.laps[0].clean is True
    assert restored.track.field[0].position == 3
    assert restored.track.fastest_lap.best_lap == 90.5


def test_trace_grader_catches_the_old_closing_rate_failure():
    failures = grade_record(
        _record("Closing at 26 metres per second, contact in one second."))

    assert any("physical rate" in failure for failure in failures)


def test_stored_trace_evaluation_does_not_need_an_api_call():
    record = _record(
        "You're gaining on Rossi, two tenths over the last 3 mini-sectors; "
        "gap 1.2 seconds.")
    results = evaluate([record], AppConfig(), replay_model=False)

    assert results[0]["failures"] == []


def test_a_qualifying_budget_answered_from_the_clock_is_graded_a_failure():
    """The bug the session_status calculation exists to stop: a Lone Qualify
    session publishes a lap allowance, and answering "how long have I got"
    with the eight minutes on the session clock tells a driver with two laps
    that they have time to keep trying."""
    record = {
        "question": "how long have I got left?",
        "answer": "You've got about five minutes left in this session.",
        "route": None,
        "snapshot": {"session_type": "Lone Qualify", "laps_total": 2,
                     "laps_remaining": 1, "time_remaining": 300.0},
    }
    assert any("clock" in f for f in grade_record(record))


def test_answering_a_qualifying_budget_in_laps_passes():
    record = {
        "question": "how long have I got left?",
        "answer": "One lap left of your two; the five minutes is your window.",
        "route": None,
        "snapshot": {"session_type": "Lone Qualify", "laps_total": 2,
                     "laps_remaining": 1, "time_remaining": 300.0},
    }
    assert grade_record(record) == []


def test_a_race_clock_answer_is_not_graded_as_a_qualifying_one():
    """Timed races are answered from the clock, correctly."""
    record = {
        "question": "how long have I got left?",
        "answer": "About twenty minutes left.",
        "route": None,
        "snapshot": {"session_type": "Race", "laps_total": None,
                     "time_remaining": 1200.0},
    }
    assert grade_record(record) == []
