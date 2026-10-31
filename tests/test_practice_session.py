"""A practice session must not be answered like a race.

In a public practice session every kind of answer came back race-shaped:
"how are we looking" was a place and nothing else, cars either side were
"pulling away" and "gaining", and the model talked fuel and stops. Most of
the calculators already branched on the session; these are the paths that
did not, plus the exchange log, which now says which session each answer
was written for — an unrecognised session type is classified as a race by
design, and nothing showed when that had happened.
"""

import pytest

from ai_race_engineer import race_math as rm
from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.prompt_builder import PromptBuilder
from ai_race_engineer.telemetry import (CarTelemetry, Rival, TelemetrySnapshot,
                                        TrackConfig)


def _car(**kw):
    base = dict(fuel_unit="litres", fuel_level=40.0, fuel_per_lap=3.2,
                best_lap_time=91.2, position=4)
    base.update(kw)
    return CarTelemetry(**base)


def _field(**player):
    return [
        Rival(name="Rossi", car_number="13", position=1, best_lap=90.9),
        Rival(name="Silva", car_number="16", position=2, best_lap=91.0),
        Rival(name="You", car_number="1", position=4, best_lap=91.2,
              is_player=True, **player),
    ]


def _practice(**kw):
    return TrackConfig(session_type="Practice", time_remaining=1800.0,
                       current_lap=8, field=_field(), **kw)


# ── "How are we looking" ────────────────────────────────────────────────

def test_practice_status_leads_with_the_lap_time_not_the_place():
    spoken = rm.status_report(_car(), _practice())["spoken"]

    assert spoken.startswith("You're P4 on lap time")
    assert "3 tenths of a second off the session best" in spoken


def test_the_session_best_holder_is_told_so():
    car = _car(position=1, best_lap_time=90.9)

    spoken = rm.position_report(car, _practice())["spoken"]

    assert spoken == "You're P1 on lap time, that's the session best."


def test_no_lap_yet_is_only_the_place():
    spoken = rm.position_report(_car(best_lap_time=None), _practice())["spoken"]

    assert spoken == "You're P4 on lap time."


def test_a_multiclass_practice_measures_against_the_class_best():
    """The overall best is a faster class's lap — seconds the GT car cannot find."""
    field = [
        Rival(name="Proto", car_number="5", position=1, best_lap=82.0,
              car_class_id=1),
        Rival(name="Silva", car_number="16", position=2, best_lap=91.0,
              car_class_id=2),
        Rival(name="You", car_number="1", position=4, best_lap=91.2,
              is_player=True, car_class_id=2),
    ]
    track = TrackConfig(session_type="Practice", field=field, car_class_id=2)

    spoken = rm.position_report(_car(), track)["spoken"]

    assert "2 tenths of a second off the class best" in spoken
    assert "session best" not in spoken


def test_a_race_still_gives_the_cars_either_side():
    car = _car(ahead=Rival(name="Silva", gap=1.1, position=3))

    spoken = rm.position_report(car, TrackConfig(session_type="Race"))["spoken"]

    assert spoken == "You're P4. Silva 1.1 seconds ahead."


# ── The cars either side are traffic ────────────────────────────────────

_RIVAL_VERBS = ("gaining", "pulling away", "edging away")


@pytest.mark.parametrize("status", ["early", "confirmed"])
@pytest.mark.parametrize("direction", ["ahead", "behind"])
@pytest.mark.parametrize("change", [0.3, -0.3])
def test_a_practice_gap_trend_is_traffic_not_a_battle(status, direction, change):
    other = Rival(name="Rossi", car_number="13", position=3, gap=1.2)
    car = _car(**{direction: other,
                  f"closing_{direction}_status": status,
                  f"closing_{direction}_seconds": 10.0,
                  f"closing_{direction}_change": change,
                  f"closing_{direction}_segments": 3})

    spoken = rm.adjacent_gap_trend(car, _practice(), direction)["spoken"]

    assert "Rossi" in spoken
    assert not any(verb in spoken for verb in _RIVAL_VERBS), spoken


def test_a_practice_car_closing_from_behind_is_said_plainly():
    other = Rival(name="Rossi", car_number="13", position=5, gap=1.2)
    car = _car(behind=other, closing_behind_status="confirmed",
               closing_behind_seconds=10.0, closing_behind_change=0.3,
               closing_behind_segments=3)

    spoken = rm.adjacent_gap_trend(car, _practice(), "behind")["spoken"]

    assert spoken.startswith("Rossi is catching you")


# ── The prompt rules out race topics ────────────────────────────────────

def test_the_practice_block_rules_out_pit_strategy_and_battles():
    builder = PromptBuilder(AppConfig())

    practice = builder.format_telemetry(_car(), _practice())
    race = builder.format_telemetry(_car(), TrackConfig(session_type="Race"))

    for phrase in ("no pit window", "no fuel to the end", "never rivals"):
        assert phrase in practice
        assert phrase not in race


# ── The exchange log says which session it answered for ─────────────────

def _orchestrator():
    return Orchestrator(AppConfig({"telemetry": {"source": "simulated"}}))


@pytest.mark.parametrize("session_type, kind", [
    ("Practice", "practice"),
    ("Lone Qualify", "qualify"),
    ("", "race"),
])
def test_each_exchange_records_the_session_it_was_answered_for(session_type, kind):
    snapshot = TelemetrySnapshot(_car(), TrackConfig(session_type=session_type))

    entry = _orchestrator()._log_exchange("update", "You're P4.", "voice", snapshot)

    # The raw string beside the verdict: an empty type reads as empty, so a
    # race verdict reached by default can be told from a real one.
    assert entry["session_type"] == session_type
    assert entry["session_kind"] == kind
