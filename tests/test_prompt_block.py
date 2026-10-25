"""Tests over the context block the model is handed.

The calculators refuse cleanly when a figure does not exist. The block is the
second, weaker copy of the same facts, and it has no refusal path — so what it
writes for a missing measurement is what the engineer says out loud.

    uv run pytest tests/ -q
"""

from ai_race_engineer.prompt_builder import (PromptBuilder, _fuel_line,
                                             _lap_counter, _position_line,
                                             _qualifying_format, _remaining_line)
from ai_race_engineer.telemetry import CarTelemetry, TrackConfig


def _car(**kw):
    base = dict(fuel_unit="litres", fuel_level_pct=46.0, fuel_level=50.1,
                fuel_per_lap=3.5, fuel_last_lap=3.62, fuel_used_stint=12.1,
                fuel_remaining_laps=14.3)
    base.update(kw)
    return CarTelemetry(**base)


def _unmeasured(**kw):
    """The opening laps: the burn average needs two completed laps."""
    return _car(fuel_per_lap=None, fuel_last_lap=None, fuel_used_stint=None,
                fuel_remaining_laps=None, **kw)


def test_an_unmeasured_fuel_range_is_not_written_as_a_number_shaped_blank():
    """This is the bug, from a real race at Barcelona.

    The line used to end "~unknown laps of fuel left", three lines under
    "Remaining: 2 laps". Asked how much fuel was left, twenty-two seconds
    after the calculator had correctly refused, the engineer filled the blank
    with the lap count and said "about two laps of fuel left". The driver had
    fourteen. A slot the block itself supplies a plausible number for cannot
    be left blank, however the blank is spelled.
    """
    line = _fuel_line(_unmeasured())
    assert "laps of fuel left" not in line.replace("LAPS OF FUEL LEFT", "")
    assert "unknown" not in line.lower()
    assert "NOT MEASURED YET" in line


def test_an_unmeasured_burn_is_not_offered_as_a_litre_figure_either():
    """"burn unknown litres per racing lap" is the same hole in the same
    line — a per-lap figure the driver could be told and act on."""
    line = _fuel_line(_unmeasured())
    assert "burn" not in line
    assert "per racing lap" not in line


def test_the_absence_says_what_to_do_about_it():
    """Naming the missing figure is not enough on its own: the engineer has
    to be told to say so, and told not to reach for the lap count sitting a
    few lines above it."""
    line = _fuel_line(_unmeasured())
    assert "cannot call the fuel yet" in line
    assert "laps remaining" in line


def test_fuel_still_reports_what_it_does_have():
    """The tank reading exists from the first poll and is worth having on
    its own — refusing the range is not a reason to go silent on the fuel."""
    line = _fuel_line(_unmeasured(fuel_level=50.1))
    assert "46% of tank" in line
    assert "50.1 litres" in line


def test_a_measured_fuel_line_is_unchanged():
    """The fix is for the missing case only. Once the burn exists the line is
    what it always was, in the driver's own units."""
    assert _fuel_line(_car()) == (
        "- Fuel: 46% of tank (50.1 litres) | burn 3.50 litres per racing lap"
        " (last lap actually 3.62) | 12.1 used this stint"
        " | ~14.3 laps of fuel left")


def test_an_in_lap_burn_is_labelled_as_measurement_not_prediction():
    line = _fuel_line(_car(fuel_last_lap=1.3, fuel_last_lap_clean=False))
    assert "last lap actually 1.30" in line
    assert "NOT a clean racing lap" in line
    assert "do not use it to predict range" in line


def test_a_range_without_a_burn_still_reports_the_range():
    """_fuel_laps() needs the burn, so these normally arrive together — but
    the two fields are read separately and the block must not drop a figure
    it has because a neighbouring one is missing."""
    line = _fuel_line(_car(fuel_per_lap=None, fuel_last_lap=None))
    assert "~14.3 laps of fuel left" in line
    assert "NOT MEASURED YET" not in line


# ── A position the sim has not given ────────────────────────────────


def _quali_track(**kw):
    """Lone Qualify, from a captured session: two laps inside eight minutes."""
    base = dict(session_type="Lone Qualify", current_lap=3, laps_total=2,
                laps_remaining=1, time_remaining=300.0, solo_qualifying=True,
                qualify_scoring="best lap")
    base.update(kw)
    return TrackConfig(**base)


def test_an_unclassified_position_is_not_rendered_as_p0():
    """This is the bug, from a real qualifying session. iRacing publishes 0
    for a car it has not placed, the block wrote it out as "P0", and three
    lines below sat a running order with the driver second. Asked where they
    were, the engineer said pole.

    Same hole as the fuel line, in a different slot: a blank the block itself
    fills with something number-shaped gets read as the answer."""
    line = _position_line(CarTelemetry(position=None), TrackConfig())
    assert "P0" not in line
    assert "PNone" not in line
    assert "unknown" in line


def test_the_absent_position_says_not_to_count_the_standings_instead():
    """Naming the gap is not enough. The running order is right there, and
    counting down it is exactly how a plausible wrong place gets produced."""
    line = _position_line(CarTelemetry(position=None), TrackConfig())
    assert "haven't got it" in line
    assert "running order" in line


def test_a_real_position_still_reads_as_one():
    line = _position_line(CarTelemetry(position=2, class_position=1),
                          TrackConfig())
    assert line == "- Position: P2 (P1 in class)"


# ── A qualifying session is not a short race ────────────────────────


def test_a_qualifying_lap_allowance_is_not_a_race_distance():
    """SessionLaps is the distance in a race and an allowance in qualifying.
    Rendering both the same way produced "Lap 3 of 2" for a driver on their
    out lap plus two fliers, which reads as having overrun."""
    assert _lap_counter(_quali_track()) == " | Lap 3"
    assert _lap_counter(TrackConfig(session_type="Race", current_lap=3,
                                    laps_total=20)) == " | Lap 3 of 20"


def test_the_block_says_which_limit_actually_ends_the_session():
    """Laps and clock were both printed with nothing said about their
    relationship, and the engineer answered from whichever it read first."""
    line = _remaining_line(CarTelemetry(best_lap_time=68.5), _quali_track())
    assert "LAPS are what run out first" in line
    assert "clock is not the limit" in line


def test_the_qualifying_format_is_stated_from_what_the_sim_published():
    block = _qualifying_format(_quali_track())
    assert "out alone" in block
    assert "2 laps" in block
    assert "That allowance is the budget" in block
    assert "NOT how long they may keep trying" in block
    assert "best lap" in block


def test_nothing_is_claimed_about_a_format_the_sim_did_not_publish():
    """An assumed format spoken confidently is the bug, not a smaller one."""
    block = _qualifying_format(TrackConfig(session_type="Qualifying"))
    assert "out alone" not in block
    assert "the clock is what limits the running" in block


def test_the_format_is_dropped_once_qualifying_is_over():
    """The allowance is spent; describing the budget would have the engineer
    telling a driver what to do with laps they no longer have."""
    assert _qualifying_format(_quali_track(finished=True)) == ""


def test_a_finished_qualifying_session_is_not_announced_as_a_finished_race():
    """It read "RACE OVER — finished P2" with the race still to come, and
    told the model the lap it compared against was the fastest of a race
    nobody had started."""
    car = CarTelemetry(position=2, class_position=2, best_lap_time=68.5)
    block = PromptBuilder.format_telemetry(car, _quali_track(finished=True), "Bart")
    assert "RACE OVER" not in block
    assert "QUALIFYING OVER — Bart qualified P2" in block


def test_a_compacted_block_is_only_used_when_a_tool_can_fetch_the_rest():
    """Focus filtering assumes the model can ask for what was cut.

    The pace group keeps lap times and drops the rival lines and the running
    order. That is a fair trade while `driver_report` is one tool call away.
    With tools off — by setting, or because the provider rejected them for the
    session — nothing can fetch them back, and "am I faster than the car
    ahead?" arrived with no car ahead anywhere in it.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.telemetry import Rival, TelemetrySnapshot

    car = _car(position=4,
               ahead=Rival(name="Rossi", car_number="13", position=3, gap=1.2),
               behind=Rival(name="Silva", car_number="16", position=5, gap=0.8))
    track = TrackConfig(session_type="Race", laps_remaining=12,
                        field=[Rival(name="Rossi", position=3),
                               Rival(name="You", position=4, is_player=True)])
    snapshot = TelemetrySnapshot(car, track)
    builder = PromptBuilder(AppConfig())
    question = "Am I faster than the car ahead?"

    without_tools = builder.build_user_turn(question, snapshot, tools_enabled=False)
    assert "- Ahead: Rossi" in without_tools
    assert "Running order" in without_tools

    # Still compacted where a calculator can supply what is missing, or the
    # saving this filter exists for disappears.
    with_tools = builder.build_user_turn(question, snapshot, tools_enabled=True)
    assert "- Ahead: Rossi" not in with_tools
    assert len(with_tools) < len(without_tools)


def test_a_detected_topic_still_gates_the_strategy_lines_with_tools_off():
    """Compacting the block and gating the strategy lines are separate calls.

    The strategy summary is folded in only for a question that is about
    strategy, or one with no single topic at all — two lines of fuel and pit
    window on a tyre question are tokens nobody asked for. Tying that gate to
    whether the block was compacted silently turned it off for every
    tools-off question, because the block is no longer compacted there.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.telemetry import Rival, TelemetrySnapshot

    car = _car(position=4, fuel_capacity=60.0, fuel_burn_samples=5,
               fuel_burn_min=3.4, fuel_burn_max=3.6,
               ahead=Rival(name="Rossi", car_number="13", position=3, gap=1.2))
    track = TrackConfig(session_type="Race", laps_remaining=12,
                        field=[Rival(name="Rossi", position=3)])
    snapshot = TelemetrySnapshot(car, track)
    builder = PromptBuilder(AppConfig())

    tyres = builder.build_user_turn("How are my tyres?", snapshot,
                                    tools_enabled=False)
    fuel = builder.build_user_turn("How's my fuel?", snapshot,
                                   tools_enabled=False)

    assert "- Fuel plan:" not in tyres
    assert "- Fuel plan:" in fuel
    # Both keep the whole block, because nothing can fetch back what is cut.
    assert "- Ahead: Rossi" in tyres
    assert "- Ahead: Rossi" in fuel
