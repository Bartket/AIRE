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
