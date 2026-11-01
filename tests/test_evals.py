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


def test_the_grader_catches_an_answer_read_off_a_spreadsheet():
    """Both shapes reached the radio, measured: gaps in hundredths and rivals
    by roster initial. The grader is what stops them coming back unnoticed."""
    failures = grade_record(_record(
        "You're P4; M. Rossi 1.51 seconds ahead, J. Kowalski 2.17 seconds behind."))

    assert "race-speed answer gives a time to the hundredth" in failures
    assert "rival named by initial instead of surname" in failures


def test_the_grader_passes_the_radio_wording():
    assert grade_record(_record(
        "You're P4. Rossi a second and a half ahead, Kowalski 2.2 seconds behind.")) == []


def test_the_model_check_snapshot_only_holds_what_irsdk_publishes():
    """The simulated race never marked which car was the driver, so every gap
    to a named rival answered "I can't place you in the order" offline — and
    the model check measured a refusal no real session gives. It also publishes
    brake temperatures and live tyres, which iRacing does not out on track."""
    from ai_race_engineer import race_math
    from ai_race_engineer.evals import _floor_snapshot

    snapshot = _floor_snapshot()

    assert race_math.dispatch("gap_to", {"target": "Silva"}, snapshot)["available"]
    assert snapshot.car.brake_fl_temp is None
    assert snapshot.car.tyre_data_live is False


def test_practice_hundredths_are_allowed_and_thousandths_are_not():
    """Outside a race the hundredths are said on purpose — they separate two
    drivers — so the grader failing them contradicted the engineer's own
    rule. Thousandths still help nobody."""
    def graded(answer, session):
        return grade_record({"question": "how far off am I?", "route": None,
                             "snapshot": {"session_type": session}}, answer)

    assert graded("You're 0.85 seconds off the session best.", "Practice") == []
    assert graded("You're 0.853 seconds off.", "Open Qualify") == \
        ["answer gives a time to the thousandth"]
    assert graded("You're 0.85 seconds off.", "Race") == \
        ["race-speed answer gives a time to the hundredth"]


def test_a_rivals_recent_laps_survive_the_trace_round_trip():
    """The catch projection reads them as records; replayed as dicts it
    failed on the first trace that asked whether a car would catch."""
    snapshot = TelemetrySnapshot(
        CarTelemetry(position=2),
        TrackConfig(session_type="Race", field=[
            Rival(name="Novak", position=3,
                  recent_laps=[LapRecord(lap=4, time=91.0, caution=True)])]))

    restored = TelemetrySnapshot.from_dict(snapshot.to_dict())

    lap = restored.track.field[0].recent_laps[0]
    assert isinstance(lap, LapRecord) and lap.time == 91.0 and lap.caution


def _catch_record(answer, route):
    record = _record(answer)
    record["question"] = "When will the car behind catch me?"
    record["route"] = route
    return record


def test_a_lap_count_to_contact_must_come_from_the_calculator():
    """A lap count to contact is a division. Said on the calculator's route it
    is the calculator's; said by the model it is the model's arithmetic."""
    answer = "Silva catches you in about 4 laps, 10 to go."
    assert grade_record(_catch_record(answer, "deterministic")) == []
    assert grade_record(_catch_record(answer, "tool:catch_projection")) == []
    assert any("catch_projection" in failure for failure in
               grade_record(_catch_record(answer, "model")))
    named = _catch_record(answer, "tool:pace_compare")
    named["question"] = "Will Silva catch me?"
    assert any("catch_projection" in failure for failure in grade_record(named))


def test_a_projection_answer_is_not_graded_as_a_trend_that_spoke_of_laps():
    """The trend check fails any answer that mentions laps — right for the
    mini-sector trend, and wrong for the question about when."""
    failures = grade_record(_catch_record(
        "Silva catches you in about 4 laps, 10 to go.", "deterministic"))
    assert not any("adjacent trend" in failure for failure in failures)
