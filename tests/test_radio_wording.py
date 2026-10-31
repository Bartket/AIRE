"""The calculators that speak word for word must sound like an engineer.

Eight of them answer the most-asked questions with no model involved, so
nothing held them to the prompt's own speech rules. What the driver heard,
through the speech layer, was "You're position four; M. Rossi one point five
one seconds ahead" and "forty nine point six litres for eighteen laps, four
point six litres spare, about one point eight laps in hand" — false
precision, roster names, the same fact twice in two units.

The rounding here is one-way on purpose. A gap can go to a tenth either way,
but a fuel margin only ever rounds down, and a margin too small to round
says so instead of borrowing a comfortable figure.
"""

import re

import pytest

from ai_race_engineer import race_math as rm
from ai_race_engineer.telemetry import CarTelemetry, Rival, TrackConfig


def _car(**kw):
    base = dict(fuel_unit="litres", fuel_level=40.0, fuel_capacity=110.0,
                fuel_per_lap=3.2, best_lap_time=91.2)
    base.update(kw)
    return CarTelemetry(**base)


# ── The helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds, words", [
    (1.51, "a second and a half"),
    (1.98, "2 seconds"),
    (2.17, "2.2 seconds"),
    (1.0, "a second"),
    (0.37, "4 tenths"),
    (0.1, "a tenth"),
    (0.04, "under a tenth"),
])
def test_a_gap_is_said_to_the_tenth(seconds, words):
    assert rm._spoken_gap(seconds) == words


@pytest.mark.parametrize("laps, words", [
    (1.4, "a lap"),              # down, not to "a lap and a half"
    (1.5, "a lap and a half"),
    (0.5, "half a lap"),
    (2.9, "2 and a half laps"),
    (4.9, "about 4 laps"),
])
def test_a_fuel_margin_only_ever_rounds_down(laps, words):
    assert rm._spoken_margin_laps(laps) == words


def test_a_margin_under_half_a_lap_has_no_round_figure():
    assert rm._spoken_margin_laps(0.49) is None


# ── Fuel: the verdict, and the amount only when asked ───────────────────

def _comfortable():
    # 40 litres at 3.2 a lap against 11 laps: 4.8 litres, 1.5 laps, spare.
    return (_car(fuel_burn_samples=5, fuel_burn_min=3.0, fuel_burn_max=3.2),
            TrackConfig(session_type="Race", laps_remaining=11))


def test_a_healthy_tank_is_the_verdict_and_one_margin():
    car, track = _comfortable()

    result = rm.fuel_plan(car, track)

    assert result["spoken"] == "Fuel's fine to the end, a lap and a half spare."
    assert "litres" not in result["spoken"]


def test_how_much_fuel_still_gets_the_litres():
    car, track = _comfortable()
    result = rm.fuel_plan(car, track)

    asked_amount = rm.fuel_radio(result, "How much fuel have I got?")
    asked_verdict = rm.fuel_radio(result, "How's my fuel?")

    assert asked_amount.startswith("40.0 litres on board")
    assert "litres" not in asked_verdict


def test_the_terminal_tool_path_hears_the_question_too():
    """The model routes "how much fuel" to fuel_plan, which is spoken word for
    word — so the amount has to be decided there as well, not only on the
    deterministic route."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    client = LLMClient(AppConfig({"llm": {
        "provider": "local", "use_tools": "auto",
        "local": {"endpoint": "http://127.0.0.1:11434/v1", "model": "m"}}}))
    client._chat_raw = lambda messages, max_tokens=None, tools=None: {
        "content": "", "tool_calls": [{
            "id": "f1", "function": {"name": "fuel_plan", "arguments": "{}"}}]}
    car, track = _comfortable()

    answer = client.generate("How much fuel have I got left?",
                             TelemetrySnapshot(car, track))

    assert "40.0 litres on board" in answer


def test_a_thin_margin_is_never_rounded_into_comfort():
    # 0.3 litres over at 3.2 a lap — about a tenth of a lap.
    car = _car(fuel_level=35.5, fuel_burn_samples=5,
               fuel_burn_min=3.0, fuel_burn_max=3.2)

    spoken = rm.fuel_plan(car, TrackConfig(session_type="Race",
                                           laps_remaining=11))["spoken"]

    assert "only just" in spoken
    assert "a lap" not in spoken.replace("half a lap", "")


def test_a_question_about_the_next_laps_is_not_answered_about_the_finish():
    car, track = _comfortable()

    spoken = rm.fuel_plan(car, track, target_laps=5)["spoken"]

    assert "to the end" not in spoken
    assert "next 5 laps" in spoken


def test_a_shortfall_keeps_its_exact_figures():
    """Only the comfortable case was reworded; a deficit is acted on."""
    car = _car(fuel_level=20.0, fuel_burn_samples=5,
               fuel_burn_min=3.0, fuel_burn_max=3.2)

    spoken = rm.fuel_plan(car, TrackConfig(session_type="Race",
                                           laps_remaining=11))["spoken"]

    assert "short" in spoken.lower()
    assert "fine" not in spoken.lower()


# ── Rivals by surname ───────────────────────────────────────────────────

def test_a_rival_is_called_by_surname():
    car = _car(position=7, ahead=Rival(name="Sean Ambrose", gap=1.1, position=6))

    spoken = rm.position_report(car, TrackConfig(session_type="Race"))["spoken"]

    assert "Ambrose" in spoken and "Sean" not in spoken


def test_a_shared_surname_keeps_the_full_name():
    ahead = Rival(name="Sean Ambrose", car_number="3", gap=1.1, position=6)
    field = [ahead, Rival(name="Lily Ambrose", car_number="9", position=2)]
    car = _car(position=7, ahead=ahead)

    spoken = rm.position_report(car, TrackConfig(session_type="Race",
                                                 field=field))["spoken"]

    assert "Sean Ambrose" in spoken


# ── Every verbatim answer, on the source new users try first ────────────

def test_no_verbatim_answer_reads_like_a_spreadsheet():
    from ai_race_engineer.telemetry.simulated import SimulatedTelemetryAdapter

    snapshot = SimulatedTelemetryAdapter().get_telemetry_snapshot()
    car, track = snapshot.car, snapshot.track
    answers = {
        "fuel": rm.fuel_plan(car, track),
        "pit window": rm.pit_window(car, track),
        "position": rm.position_report(car, track),
        "status": rm.status_report(car, track),
        "gap ahead": rm.adjacent_gap_trend(car, track, "ahead"),
        "gap behind": rm.adjacent_gap_trend(car, track, "behind"),
    }

    for name, result in answers.items():
        spoken = result["spoken"]
        assert not re.search(r"\d\.\d\d", spoken), f"{name} has hundredths: {spoken}"
        assert ";" not in spoken, f"{name} reads as a template: {spoken}"
        assert not re.search(r"\b[A-Z]\. [A-Z]", spoken), \
            f"{name} names a rival by initial: {spoken}"
