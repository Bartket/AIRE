"""Tests over the context block the model is handed.

The calculators refuse cleanly when a figure does not exist. The block is the
second, weaker copy of the same facts, and it has no refusal path — so what it
writes for a missing measurement is what the engineer says out loud.

    uv run pytest tests/ -q
"""

from ai_race_engineer.prompt_builder import _fuel_line
from ai_race_engineer.telemetry import CarTelemetry


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
