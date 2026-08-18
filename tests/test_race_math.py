"""Tests over the arithmetic the driver actually acts on.

Every case here is a bug that reached a real race, not a hypothetical. The
value of these is not that they pass today — it is that they fail loudly if
any of it comes back, because none of these read wrong. A fuel figure that is
30% out looks exactly like one that is right.

    uv run pytest tests/ -q
"""

from ai_race_engineer import race_math as rm
from ai_race_engineer.telemetry import CarTelemetry, LapRecord, Rival, TrackConfig


def _car(**kw):
    base = dict(fuel_unit="litres", fuel_level=40.0, fuel_capacity=110.0,
                fuel_per_lap=3.2, best_lap_time=91.2)
    base.update(kw)
    return CarTelemetry(**base)


def _field(leader_gap=None):
    """A grid with the driver at P4.

    The leader's gap defaults to None on purpose: that is what the reader
    produces, because _positive() maps the leader's 0 s behind themselves to
    None. A fixture that sets 0.0 here cannot reproduce the bug and the test
    passes either way — which it did, until this was fixed.
    """
    return [
        Rival(name="Rossi", car_number="13", position=1, gap=leader_gap, best_lap=90.35),
        Rival(name="Silva", car_number="16", position=2, gap=2.3, best_lap=90.7),
        Rival(name="Becker", car_number="19", position=3, gap=3.9, best_lap=91.0),
        Rival(name="You", car_number="44", position=4, gap=5.4,
              best_lap=91.2, is_player=True),
        Rival(name="Novak", car_number="34", position=5, gap=7.2, best_lap=91.9),
    ]


# ── Fuel ────────────────────────────────────────────────────────────

def test_fuel_reports_range_not_a_verdict_outside_a_race():
    """Practice has no finish, so "will it last" has no meaning.

    It used to answer "no stop needed, 40 litres covers the 20 laps left" in
    qualifying, where those 20 laps were the session clock divided by a lap
    time — laps you could run, not laps you must.
    """
    track = TrackConfig(session_type="Practice", time_remaining=1200.0)
    result = rm.fuel_plan(_car(), track)
    assert result["available"]
    assert "laps" in result["spoken"]
    assert "short" not in result["spoken"].lower()

    window = rm.pit_window(_car(), track)
    assert not window["available"]
    assert "outside a race" in window["spoken"]


def test_fuel_verdict_still_given_in_a_race():
    track = TrackConfig(session_type="Race", laps_remaining=20)
    result = rm.fuel_plan(_car(), track)
    assert result["available"]
    # 20 laps at 3.2 needs 64 litres; 40 on board is 24 short.
    assert result["margin"] == -24.0
    assert "short" in result["spoken"].lower()


def test_safety_car_pace_is_flagged_against_the_average():
    """A racing average is pessimistic behind a safety car.

    Without this the engineer says "you are short, save fuel" while the
    driver cruises home with plenty.
    """
    track = TrackConfig(session_type="Race", laps_remaining=20)
    result = rm.fuel_plan(_car(fuel_last_lap=1.0, fuel_last_lap_clean=True), track)
    assert "Last lap only used" in result["spoken"]


def test_a_cheap_in_lap_is_not_called_extra_range():
    """The raw last-lap burn remains useful, but an in-lap cannot predict
    what the next racing laps will use."""
    track = TrackConfig(session_type="Race", laps_remaining=8)
    result = rm.fuel_plan(
        _car(fuel_level=42.5, fuel_per_lap=3.5,
             fuel_last_lap=1.3, fuel_last_lap_clean=False), track)

    assert result["burn_last_lap"] == 1.3
    assert result["burn_last_lap_clean"] is False
    assert "Last lap only used" not in result["spoken"]
    assert "go further" not in result["spoken"]


def test_unknown_last_lap_provenance_never_changes_the_range_call():
    result = rm.fuel_plan(
        _car(fuel_last_lap=1.0, fuel_last_lap_clean=None),
        TrackConfig(session_type="Race", laps_remaining=20))
    assert "Last lap only used" not in result["spoken"]


def test_fixed_lap_fuel_uses_the_distance_left_in_the_current_lap():
    car = _car(fuel_level=30.0, fuel_per_lap=3.0, lap_progress=0.5)
    result = rm.fuel_plan(car, TrackConfig(session_type="Race", laps_remaining=10))

    assert result["laps_to_go"] == 9.5
    assert result["fuel_needed"] == 28.5
    assert result["margin"] == 1.5


def test_timed_race_laps_include_the_run_to_the_finish_line():
    car = _car(fuel_level=30.0, fuel_per_lap=3.0, avg_lap_time=90.0,
               lap_progress=0.5)
    result = rm.fuel_plan(car, TrackConfig(session_type="Race", time_remaining=600.0))

    assert result["laps_to_go"] == 7.5
    assert result["fuel_needed"] == 22.5


def test_fuel_plan_uses_the_measured_burn_envelope_not_only_the_mean():
    """A mean can say the tank is good while recent clean laps disagree.

    The driver cannot act on the average without knowing that the measured
    high-burn case misses the flag.  Where the observed range spans the
    finish, the only honest verdict is marginal.
    """
    car = _car(
        fuel_level=31.0,
        fuel_per_lap=3.0,
        fuel_burn_samples=5,
        fuel_burn_min=2.8,
        fuel_burn_max=3.3,
    )
    result = rm.fuel_plan(
        car, TrackConfig(session_type="Race", laps_remaining=10))

    assert result["fuel_confidence"] == "measured"
    assert result["fuel_margin_best"] == 3.0
    assert result["fuel_margin_conservative"] == -2.0
    assert result["will_finish"] is None
    assert result["stop_required"] is None
    assert result["pit_now"] is False
    assert "marginal" in result["spoken"].lower()
    assert "2.8 to 3.3 litres" in result["spoken"]


def test_timed_fuel_plan_carries_an_explicit_extra_lap_case():
    car = _car(
        fuel_level=27.0,
        fuel_per_lap=3.0,
        fuel_burn_samples=5,
        fuel_burn_min=2.9,
        fuel_burn_max=3.1,
        avg_lap_time=90.0,
        lap_progress=0.5,
    )
    result = rm.fuel_plan(
        car, TrackConfig(session_type="Race", time_remaining=600.0))

    assert result["laps_to_go"] == 7.5
    assert result["extra_lap_margin"] == 0.65
    assert "extra timed lap" in result["spoken"].lower()


def test_series_resources_are_short_grounded_radio_answers():
    car = _car(
        pits_open=False,
        tire_sets_available={"total": 3},
        push_to_pass_count=5,
        push_to_pass_active=True,
        joker_laps_remaining=1,
        on_joker_lap=False,
        pit_autofill_active=True,
        pit_service_requests=["all four tyres", "fuel"],
        pit_tire_compound=2,
    )
    track = TrackConfig(session_type="Race")

    assert rm.car_resources(car, track, "pits")["spoken"] == "The pits are closed."
    assert "3 tyre sets" in rm.car_resources(car, track, "tyres")["spoken"]
    assert "5 uses remaining" in rm.car_resources(car, track, "push to pass")["spoken"]
    assert "one joker lap remaining" in rm.car_resources(car, track, "joker")["spoken"]
    service = rm.car_resources(car, track, "pit service")
    assert "fuel" in service["spoken"] and "four tyres" in service["spoken"]
    assert service["pit_tire_compound"] == 2


def test_pit_window_moves_with_current_lap_progress():
    car = _car(fuel_level=6.9, fuel_per_lap=3.0, fuel_capacity=22.5,
               lap_progress=0.75)
    track = TrackConfig(session_type="Race", current_lap=20, laps_remaining=10)
    result = rm.pit_window(car, track)

    assert result["laps_to_go"] == 9.2
    assert result["earliest_lap"] == 22
    assert result["latest_lap"] == 22


def test_empty_tank_is_a_reading_not_a_missing_channel():
    """0.0 litres means empty, not "no data".

    _positive() maps zero to None, which is right for lap times and was
    catastrophic here: a car run dry reported "I've got no fuel reading".
    """
    track = TrackConfig(session_type="Race", laps_remaining=5)
    result = rm.fuel_plan(_car(fuel_level=0.0), track)
    assert result["available"]
    assert result["fuel_in_tank"] == 0.0


def test_an_unsaveable_shortfall_is_a_pit_call_not_a_saving_target():
    """Short by more than a driver can lift and coast back is a stop.

    12 laps of fuel against 15 to run at 3.5 a lap needs 1.05 saved per lap
    — 30% of the lap's fuel, which is not driving, it is coasting with the
    engine off. The engineer said "save 3.1 litres a lap" on figures like
    these, an instruction the driver cannot carry out and would try to.
    """
    track = TrackConfig(session_type="Race", laps_remaining=15)
    result = rm.fuel_plan(_car(fuel_level=42.0, fuel_per_lap=3.5), track)
    assert result["available"]
    assert result["stop_required"]
    assert result["pit_now"] is False
    assert result["save_per_lap_required"] is None
    spoken = result["spoken"].lower()
    assert "per lap" not in spoken, "gave a saving figure it should have refused"
    assert "12.0 laps" in spoken and "15 laps" in spoken
    assert "fuel stop is required" in spoken


def test_a_required_stop_is_not_a_call_to_pit_now_on_a_nearly_full_tank():
    """Needing more fuel than is on board says a stop is required, not when.

    This reached a live race as "you need to pit now" immediately after a
    fill. Seventy laps need two tanks after the current one, but the first
    valid window is still several laps away.
    """
    car = _car(fuel_level=100.0, fuel_capacity=110.0, fuel_per_lap=3.5,
               lap_progress=0.2)
    track = TrackConfig(session_type="Race", current_lap=1,
                        laps_remaining=70)

    plan = rm.fuel_plan(car, track)
    window = rm.pit_window(car, track)

    assert plan["stop_required"] is True
    assert plan["stops_required"] == 2
    assert plan["pit_now"] is False
    assert "pit now" not in plan["spoken"].lower()
    assert "box for fuel" not in plan["spoken"].lower()
    assert "stay out" in plan["spoken"].lower()

    assert window["stops_required"] == 2
    assert window["earliest_lap"] == 8
    assert window["latest_lap"] == 28
    assert window["pit_now"] is False
    assert window["earliest_lap"] <= window["latest_lap"]

    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import TelemetrySnapshot
    block = PromptBuilder(AppConfig()).build_user_turn(
        "Race status", TelemetrySnapshot(car, track), tools_enabled=False)
    assert "stay out for now" in block
    assert "call the driver in" not in block


def test_the_last_safe_fuel_lap_is_the_only_automatic_pit_now_call():
    car = _car(fuel_level=3.0, fuel_capacity=30.0, fuel_per_lap=3.0,
               lap_progress=0.0)
    track = TrackConfig(session_type="Race", current_lap=20,
                        laps_remaining=8)

    result = rm.pit_window(car, track)

    assert result["latest_lap"] == 20
    assert result["pit_now"] is True
    assert "this lap" in result["spoken"].lower()


def test_current_fuel_is_answerable_before_a_burn_average_exists():
    """FuelLevel is live immediately; only range needs completed laps."""
    result = rm.fuel_plan(_car(fuel_level=97.4, fuel_per_lap=None),
                          TrackConfig(session_type="Race", laps_remaining=40))

    assert result["available"]
    assert result["fuel_in_tank"] == 97.4
    assert result["strategy_available"] is False
    assert "97.4 litres" in result["spoken"]
    assert "range" in result["spoken"].lower()


def test_a_shortfall_bigger_than_the_tank_is_two_stops_not_one():
    """Box for fuel is the wrong call when the stop cannot fix it.

    60 laps at 3.5 needs 210 litres; 40 on board and a 110 litre tank is 150
    even after running this tank dry and filling it. The driver stops, takes
    a full load, and is still 60 litres short — having been told the stop
    solved it. The capacity was read from DriverCarFuelMaxLtr all along and
    only pit_window looked at it.
    """
    track = TrackConfig(session_type="Race", laps_remaining=60)
    result = rm.fuel_plan(_car(fuel_level=40.0, fuel_per_lap=3.5), track)
    assert result["available"]
    assert result["one_stop_covers_it"] is False
    assert result["tank_capacity"] == 110.0
    assert result["stop_required"]
    assert result["stops_required"] == 2
    assert result["save_per_lap_required"] is None
    spoken = result["spoken"].lower()
    assert "2 fuel stops" in spoken, "called it without saying how many stops"
    assert "box for fuel" not in spoken, "sent them in for a stop that cannot fix it"


def test_no_capacity_reading_does_not_become_a_two_stop_call():
    """Unknown tank size must fall back, not be read as "the tank is small".

    DriverCarFuelMaxLtr is session YAML, not a channel, and it is absent
    before the YAML parses. A None treated as False here would announce a
    two-stopper on every race that asked in the first seconds.
    """
    track = TrackConfig(session_type="Race", laps_remaining=60)
    result = rm.fuel_plan(_car(fuel_level=40.0, fuel_per_lap=3.5,
                               fuel_capacity=None), track)
    assert result["available"]
    assert result["one_stop_covers_it"] is None
    assert result["tank_capacity"] is None
    assert "two" not in result["spoken"].lower()


def test_a_shortfall_within_lifting_and_coasting_still_gets_a_target():
    """The pit call must not swallow the case saving actually solves.

    30 laps at 3.5 needs 105; 100 on board is 5 short, 0.17 a lap — under
    five percent of the lap, which is ordinary lift and coast.
    """
    track = TrackConfig(session_type="Race", laps_remaining=30)
    result = rm.fuel_plan(_car(fuel_level=100.0, fuel_per_lap=3.5), track)
    assert result["available"]
    assert not result["stop_required"]
    assert result["save_per_lap_required"] == 0.167
    assert "save 0.17 litres per lap" in result["spoken"]


# ── Gaps and the field ──────────────────────────────────────────────

def test_gap_to_the_leader_is_answerable():
    """The leader is 0 s behind the leader, and _positive() turned that into
    None, so every gap-to-leader question was unanswerable."""
    track = TrackConfig(session_type="Race", field=_field(), field_size=5)
    result = rm.gap_to(_car(), track, "leader")
    assert result["available"]
    assert result["gap_seconds"] == 5.4
    assert result["ahead"] is True


def test_gap_direction_is_not_inverted():
    track = TrackConfig(session_type="Race", field=_field(), field_size=5)
    assert rm.gap_to(_car(), track, "P2")["ahead"] is True
    assert rm.gap_to(_car(), track, "P5")["ahead"] is False


def test_position_report_prefers_adjacent_battle_gaps_over_gap_to_leader():
    ahead = Rival(name="Sean Ambrose", car_number="71", gap=1.1,
                  gap_metres=62.0, position=6)
    behind = Rival(name="Mike Gladfelter", car_number="7", gap=0.3,
                   gap_metres=17.0, position=8)
    car = _car(position=7, ahead=ahead, behind=behind)

    result = rm.position_report(car, TrackConfig(session_type="Race"))

    assert result["available"]
    assert "P7" in result["spoken"]
    assert "Sean Ambrose" in result["spoken"] and "1.10 seconds ahead" in result["spoken"]
    assert "Mike Gladfelter" in result["spoken"] and "0.30 seconds behind" in result["spoken"]
    assert "leader" not in result["spoken"].lower()


def test_position_report_uses_the_grid_before_green_not_physical_order():
    car = _car(
        position=16,
        ahead=Rival(name="Wrong physical neighbour", gap=0.2, position=15),
        behind=Rival(name="Another physical neighbour", gap=0.3, position=17),
    )
    track = TrackConfig(
        session_type="Race", session_state="GetInCar",
        grid_position=7, grid_class_position=7,
    )

    result = rm.position_report(car, track)

    assert result["spoken"] == "You start P7; the race hasn't started."
    assert result["position"] == 7
    assert result["ahead"] is None and result["behind"] is None
    assert "P16" not in result["spoken"]


def test_a_faster_car_ahead_is_not_reported_as_catchable():
    """laps_to_contact means nothing when the quicker car is already ahead —
    the model narrated a catch that could never happen."""
    track = TrackConfig(session_type="Race", field=_field(), field_size=5)
    result = rm.pace_compare(_car(), track, "P2")
    assert result["closing"] is False
    assert result["laps_to_contact"] is None


# ── Spoken numbers ──────────────────────────────────────────────────

def test_sub_second_deltas_never_render_as_a_bare_decimal():
    """0.85 s was read aloud as "a tenth and a half", a 5.7x error, because
    the model was converting. Hand over the words instead."""
    for value in (0.03, 0.25, 0.5, 0.85, 0.99):
        assert "0." not in rm._spoken_delta(value)
    assert rm._spoken_delta(0.5) == "5 tenths of a second"
    assert rm._spoken_delta(0.85) == "85 hundredths of a second"


# ── Pace trend ──────────────────────────────────────────────────────

def _laps(times, clean=None):
    clean = clean or [True] * len(times)
    return [LapRecord(lap=i + 1, time=t, clean=c)
            for i, (t, c) in enumerate(zip(times, clean))]


def test_degradation_is_measured_from_clean_laps_only():
    """One off-track lap in the window otherwise reads as a cliff."""
    track = TrackConfig(session_type="Race",
                        laps=_laps([90.1, 90.4, 98.7, 90.8, 91.1, 91.5],
                                   [True, True, False, True, True, True]))
    result = rm.pace_trend(_car(), track)
    assert result["verdict"] == "degrading"
    assert result["laps_skipped"] == 1
    assert 0.2 < result["trend_per_lap"] < 0.5


def test_scattered_laps_are_not_called_stable():
    """A flat trend line across widely spread laps is true and useless."""
    track = TrackConfig(session_type="Race",
                        laps=_laps([90.0, 90.1, 97.5, 90.2, 90.1]))
    assert rm.pace_trend(_car(), track)["verdict"] == "inconsistent"


# ── Race history ────────────────────────────────────────────────────

def _race(positions, session_num=2, **kw):
    """A race so far, one lap per entry, with the driver at those positions."""
    laps = [LapRecord(lap=i + 1, time=92.0 + i * 0.05, position=p,
                      session_num=session_num)
            for i, p in enumerate(positions)]
    base = dict(session_type="Race", session_num=session_num, laps=laps)
    base.update(kw)
    return TrackConfig(**base)


def test_grid_position_is_never_inferred_from_the_first_lap():
    """The earliest lap record is the position at the END of lap one.

    After a first-lap scrap that is several places from the grid slot, and
    the grid slot is the number being asked for. With no qualifying result
    and nothing latched at green, the honest answer is that we do not know —
    substituting lap one would be a plausible number reached by inference,
    which is the whole failure this feature exists to avoid.
    """
    track = _race([9, 7, 6, 5])          # scrapped back to P9 on lap one
    result = rm.race_history(_car(position=5), track)

    assert result["grid_position"] is None
    assert result["places_gained"] is None
    assert "P9" not in result["spoken"]
    assert "grid position" in result["spoken"]


def test_places_gained_is_measured_from_the_grid_not_the_first_lap():
    track = _race([9, 7, 6, 5], grid_position=7)
    result = rm.race_history(_car(position=5), track)

    assert result["places_gained"] == 2          # P7 grid to P5, not P9 to P5
    assert "Started P7" in result["spoken"]
    assert "2 places gained" in result["spoken"]


def test_practice_laps_do_not_count_toward_the_race():
    """The history deliberately survives a session change, so it must be
    filtered. Carried in, a practice out-lap becomes part of the race story
    and can hold the best lap of a race it was never part of."""
    practice = [LapRecord(lap=i + 1, time=88.0, position=1, session_num=0)
                for i in range(6)]
    track = _race([6, 5, 4], session_num=2, grid_position=7)
    track.laps = practice + track.laps

    result = rm.race_history(_car(position=4), track)

    assert result["laps_recorded"] == 3
    assert result["best_lap_number"] == 1        # of the race, not the 88.0s
    assert result["best_lap"] > 90.0


def test_position_trace_records_changes_not_every_lap():
    """One row per lap is sixty rows in a sixty-lap race — paid for on every
    call, and it reads worse than naming the three laps it actually moved."""
    track = _race([7, 7, 7, 6, 6, 6, 6, 5, 5, 5], grid_position=7)
    trace = rm.race_history(_car(position=5), track)["position_trace"]

    assert [t["position"] for t in trace] == [7, 6, 5]
    assert [t["lap"] for t in trace] == [1, 4, 8]


def test_laps_led_of_zero_is_kept_as_a_fact():
    """Leading no laps is an answer. _positive() would drop it — the same
    slip that made the leader's gap permanently unavailable."""
    track = _race([4, 4], grid_position=4, laps_led=0)
    result = rm.race_history(_car(position=4), track)

    assert result["laps_led"] == 0
    assert "led" not in result["spoken"]         # true, but not worth saying


def test_a_lap_with_no_position_does_not_become_p0():
    """position is Optional and is None before iRacing classifies the car."""
    laps = [LapRecord(lap=1, time=92.0, position=None, session_num=2),
            LapRecord(lap=2, time=92.1, position=5, session_num=2)]
    track = TrackConfig(session_type="Race", session_num=2, laps=laps,
                        grid_position=7)
    result = rm.race_history(_car(position=5), track)

    assert result["position_trace"] == [{"lap": 2, "position": 5}]
    assert "P0" not in result["spoken"]


def test_a_truncated_history_says_so():
    """The reader keeps a bounded deque, so a long race outruns it. Silently
    describing the last hundred laps as the whole race would be wrong about
    where it started."""
    track = _race([4] * 10, grid_position=7, race_laps=90)
    assert rm.race_history(_car(position=4), track)["history_partial"] is True

    short = _race([4] * 10, grid_position=7, race_laps=10)
    assert rm.race_history(_car(position=4), short)["history_partial"] is False


def test_race_history_cannot_claim_places_lost_on_the_formation_lap():
    """The live report said P7 to P16 before the race had even started."""
    track = TrackConfig(session_type="Race", session_num=2,
                        session_state="ParadeLaps", grid_position=7)
    result = rm.race_history(_car(position=16), track)

    assert result["available"]
    assert result["spoken"] == "You start P7; we're on the formation lap."
    assert result["places_gained"] is None
    assert result["position_now"] is None
    assert "P16" not in result["spoken"] and "lost" not in result["spoken"]
    assert result["position_trace"] == []


# ── Nothing may raise ───────────────────────────────────────────────

def test_no_tool_raises_on_empty_or_junk_input():
    """The model may call these with anything, and a crash mid-corner takes
    the radio down."""
    from ai_race_engineer.telemetry import TelemetrySnapshot

    empty = TelemetrySnapshot(CarTelemetry(), TrackConfig())
    for name in rm.TOOL_NAMES:
        for args in ({}, {"target": None}, {"target": "nobody"}, {"laps": "x"}):
            result = rm.dispatch(name, args, empty)
            assert isinstance(result, dict)
            assert "spoken" in result


def test_every_advertised_tool_has_an_implementation():
    assert {t["function"]["name"] for t in rm.tool_schemas()} == set(rm.TOOL_NAMES)


# ── Running order ───────────────────────────────────────────────────

class _FakeSDK:
    """Just enough of the irsdk surface for the running-order maths."""

    def __init__(self, **channels):
        self._c = channels

    def __getitem__(self, key):
        return self._c.get(key)

    def get(self, key, default=None):
        value = self._c.get(key)
        return default if value is None else value


def _reader(**channels):
    """A reader wired to fixed channel values, with the session YAML stubbed."""
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    channels.setdefault("SessionState", 4)  # green race unless a test says otherwise
    reader = R.__new__(R)
    reader._prev_lap = {}
    reader._get = lambda name, default=None: channels.get(name, default)
    reader._drivers = lambda: {
        0: {"name": "You", "class_id": 1},
        1: {"name": "Rossi", "class_id": 1},
    }
    reader._session_type = lambda: channels.get("_session", "Race")
    reader._finished = lambda: channels.get("_finished", False)
    reader._sector_times = {}
    return reader


def test_taking_the_lead_is_reported_before_the_line():
    """iRacing's PlayerCarPosition only moves as cars cross start/finish.

    Overtake for the lead in a corner and the classification still says P2
    for the rest of the lap, while the driver's own display says P1. The
    engineer sounded a lap behind the race.
    """
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=2,             # classification, not yet updated
        PlayerCarClassPosition=2,
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],   # player is now ahead on track
    )
    assert reader._live_positions() == (1, 1)


def test_formation_lap_does_not_turn_physical_order_into_race_position():
    """Cars leaving the grid and forming rows are not racing for position.

    The reported session started P7, but lap-distance sorting called it P16
    before the green and race_history announced nine places lost already.
    """
    reader = _reader(
        SessionState=3,                    # ParadeLaps
        PlayerCarIdx=0,
        PlayerCarPosition=7,
        PlayerCarClassPosition=7,
        CarIdxLapCompleted=[0, 0],
        CarIdxLap=[0, 0],
        CarIdxLapDistPct=[0.10, 0.80],     # physically behind while forming up
    )

    assert reader._live_ranks() == {}
    assert reader._live_positions() == (7, 7)
    assert reader._rivals() == (None, None)


def test_the_lap_counter_racing_ahead_of_the_fraction_is_corrected():
    """CarIdxLap is laps *started* and increments a tick or two before
    CarIdxLapDistPct wraps to zero.

    Reading laps started shifts every car by one, so the order survives —
    until two cars are desynced by different amounts, which is precisely
    what happens when they cross the line a tick apart. Here the player
    leads and has wrapped; the rival crossed one tick later and still reads
    0.998. Uncorrected, the rival jumps a whole lap clear and is called the
    leader, which is the "P1 while I had dropped to P2" report inverted.
    """
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=1,
        PlayerCarClassPosition=1,
        CarIdxLapCompleted=[11, 11],
        CarIdxLap=[12, 12],          # both have started lap 12
        CarIdxLapDistPct=[0.01, 0.998],   # player wrapped, rival has not
    )
    reader._prev_lap = {0: 11, 1: 11}
    assert reader._live_positions()[0] == 1


def test_classification_is_used_once_the_flag_is_out():
    """After the checkered this is a finishing position and must not drift."""
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=2,
        PlayerCarClassPosition=2,
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],
        _finished=True,
    )
    assert reader._live_positions() == (2, 2)


def test_qualifying_uses_the_classification_not_track_order():
    """Outside a race the order is decided by lap time, not track position."""
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=4,
        PlayerCarClassPosition=4,
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],
        _session="Practice",
    )
    assert reader._live_positions() == (4, 4)


def test_single_class_field_never_splits_position_from_class():
    """A live overall against a classified class position produced
    "P1, P2 in class" in an all-GT3 race."""
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=2,
        PlayerCarClassPosition=2,
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],
    )
    overall, in_class = reader._live_positions()
    assert overall == in_class


def test_standings_and_the_position_line_cannot_disagree():
    """The prompt carried two position numbers from two different sources.

    The position line came from the live track order; the standings block
    and every race_math tool came from CarIdxPosition, the classification.
    Mid-lap they disagree, so the engineer answered correctly or a lap
    stale depending on which one the model happened to read — which is
    what made it look intermittent rather than simply wrong.
    """
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=2,             # classification, not yet updated
        PlayerCarClassPosition=2,
        CarIdxPosition=[2, 1],           # ditto, for the whole field
        CarIdxClassPosition=[2, 1],
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],   # player has just taken the lead
        CarIdxF2Time=[0.8, 0.0],
        CarIdxOnPitRoad=[False, False],
        CarIdxTrackSurface=[3, 3],
        CarIdxBestLapTime=[91.0, 91.4],
    )
    reader._session = lambda *a, **k: {}
    field, size, _ = reader._standings()
    me = next(r for r in field if r.is_player)

    assert reader._live_positions()[0] == 1
    assert me.position == 1, "standings still reporting the classification"
    # race_math treats P1 as zero gap to the leader; that must follow the
    # position actually reported, or gap_to("leader") answers nonsense.
    assert me.gap == 0.0
    assert [r.position for r in field] == [1, 2]


def test_standings_use_the_classification_once_finished():
    """After the flag the standings are a result, not a running order."""
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=2,
        PlayerCarClassPosition=2,
        CarIdxPosition=[2, 1],
        CarIdxClassPosition=[2, 1],
        CarIdxLapCompleted=[10, 10],
        CarIdxLap=[11, 11],
        CarIdxLapDistPct=[0.55, 0.50],
        CarIdxF2Time=[0.8, 0.0],
        CarIdxOnPitRoad=[False, False],
        CarIdxTrackSurface=[3, 3],
        CarIdxBestLapTime=[91.0, 91.4],
        _finished=True,
    )
    reader._session = lambda *a, **k: {}
    field, _, _ = reader._standings()
    me = next(r for r in field if r.is_player)
    assert me.position == 2
    assert reader._live_positions()[0] == 2


def test_cars_the_server_is_not_transmitting_keep_their_slot():
    """iRacing's Max Cars setting caps how many cars the server sends.

    Per iRacing: "If you have this set at 15, and there are 60 cars in the
    field, you will only ever be given positions for 15 cars at any one
    time... over time, as you move through the field, the particular set
    of 15 cars will change."

    Untransmitted cars report a negative lap distance. Dropping them
    renumbered everyone below, promoting the driver by however many cars
    ahead happened to be missing — and because the set changes as you move
    through the field, so did the error.
    """
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=3,
        PlayerCarClassPosition=3,
        CarIdxPosition=[3, 1, 2],
        CarIdxClassPosition=[3, 1, 2],
        CarIdxLapCompleted=[10, 10, 10],
        CarIdxLap=[11, 11, 11],
        # Car 1 leads and is transmitted. Car 2 is running second but is
        # out of the transmitted set, so its distance reads -1.
        CarIdxLapDistPct=[0.40, 0.60, -1.0],
    )
    reader._drivers = lambda: {
        0: {"name": "You", "class_id": 1},
        1: {"name": "Rossi", "class_id": 1},
        2: {"name": "Silva", "class_id": 1},
    }
    assert reader._live_positions()[0] == 3, "missing car promoted the driver"


def test_a_full_field_is_ordered_purely_live():
    """The pinning must not disturb the normal case: with every car
    transmitted, the order is the track order and nothing else."""
    reader = _reader(
        PlayerCarIdx=0,
        PlayerCarPosition=3,
        PlayerCarClassPosition=3,
        CarIdxPosition=[3, 1, 2],       # classification is a lap behind
        CarIdxClassPosition=[3, 1, 2],
        CarIdxLapCompleted=[10, 10, 10],
        CarIdxLap=[11, 11, 11],
        CarIdxLapDistPct=[0.70, 0.60, 0.50],   # player has passed both
    )
    reader._drivers = lambda: {
        0: {"name": "You", "class_id": 1},
        1: {"name": "Rossi", "class_id": 1},
        2: {"name": "Silva", "class_id": 1},
    }
    assert reader._live_positions()[0] == 1


# ── Asking about another car ────────────────────────────────────────

def test_the_car_ahead_reports_lap_times_not_just_a_name():
    """car.ahead identifies the car the driver can see but carries no
    timing — it is built from track order, not the classification.

    Resolving "the car ahead" off that record answered "M. Rossi is P3"
    with no lap time at all, which is the one thing the question is
    actually asking for.
    """
    field = _field()
    car = _car(ahead=Rival(name="Silva", car_number="16", position=2))
    track = TrackConfig(session_type="Race", field=field, field_size=5)
    result = rm.driver_report(car, track, "ahead")
    assert result["available"]
    assert result["driver"] == "Silva"
    assert result["best_lap"] == 90.7, "resolved to the on-track stub, not the classification"


def test_driver_report_finds_a_car_every_way_it_might_be_named():
    track = TrackConfig(session_type="Race", field=_field(), field_size=5)
    car = _car()
    for query in ("leader", "P1", "Rossi", "#13", "1"):
        assert rm.driver_report(car, track, query)["driver"] == "Rossi", query
    assert not rm.driver_report(car, track, "Hamilton")["available"]


def test_driver_report_exposes_the_opponents_model_and_class():
    rival = Rival(name="Rossi", car_number="13", position=1,
                  car_class_id=20, car_class_name="GT3", car_id=456,
                  car_model="McLaren 720S GT3 EVO",
                  car_model_short="McLaren 720S GT3")
    player = Rival(name="You", car_number="44", position=2,
                   car_class_id=10, car_class_name="Porsche Cup", is_player=True)
    track = TrackConfig(session_type="Race", field=[rival, player], field_size=2,
                        car_class_id=10, car_class_name="Porsche Cup")

    result = rm.driver_report(_car(), track, "Rossi")
    assert result["car_model"] == "McLaren 720S GT3 EVO"
    assert result["car_model_short"] == "McLaren 720S GT3"
    assert result["car_class"] == "GT3"
    assert result["car_id"] == 456
    assert result["same_class_as_you"] is False
    assert "McLaren 720S GT3" in result["spoken"]
    assert "GT3 class" in result["spoken"]
    assert "different class" in result["spoken"]


def test_different_class_traffic_is_not_called_a_position_rival():
    rival = Rival(name="Prototype", car_number="1", position=3, gap=3.0,
                  best_lap=82.0, car_class_id=20, car_class_name="GTP",
                  class_est_lap_time=82.0)
    player = Rival(name="You", car_number="44", position=4, gap=5.0,
                   car_class_id=10, car_class_name="GT3",
                   class_est_lap_time=92.0, is_player=True)
    track = TrackConfig(session_type="Race", field=[rival, player],
                        car_class_id=10, car_class_name="GT3",
                        car_class_est_lap_time=92.0)

    gap = rm.gap_to(_car(), track, "Prototype")
    assert gap["same_class_as_you"] is False
    assert gap["class_speed"] == "faster"
    assert gap["position_rival"] is False
    assert "not a battle for this position" in gap["spoken"]

    report = rm.driver_report(_car(), track, "Prototype")
    assert report["class_speed"] == "faster"
    assert report["position_rival"] is False
    assert "faster class" in report["spoken"]

    order = rm.standings(_car(), track, around_me=1)
    prototype = order["entries"][0]
    assert prototype["same_class_as_you"] is False
    assert prototype["class_speed"] == "faster"
    assert prototype["position_rival"] is False
    assert "not for position" in order["spoken"]


def test_missing_car_identity_stays_unavailable_not_inferred():
    rival = Rival(name="Rossi", car_number="13", position=1)
    player = Rival(name="You", car_number="44", position=2, is_player=True)
    result = rm.driver_report(
        _car(), TrackConfig(session_type="Race", field=[rival, player]), "Rossi")

    assert result["car_model"] is None
    assert result["car_class"] is None
    assert result["same_class_as_you"] is None
    assert "class" not in result["spoken"].lower()


def test_prompt_names_the_players_car_and_immediate_traffic_only():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder

    ahead = Rival(name="Rossi", car_number="13", car_class_id=20,
                  car_class_name="GT3", car_model="McLaren 720S GT3 EVO",
                  car_model_short="McLaren 720S GT3", gap=1.2)
    car = _car(ahead=ahead)
    track = TrackConfig(session_type="Race", car_class_id=10,
                        car_class_name="Porsche Cup",
                        car_model="Porsche 911 GT3 Cup (992)",
                        car_model_short="Porsche 911 Cup")

    block = PromptBuilder(AppConfig()).format_telemetry(car, track)
    assert "Your car: Porsche 911 Cup | Class: Porsche Cup" in block
    assert "McLaren 720S GT3" in block
    assert "GT3 class" in block

    without_identity = PromptBuilder(AppConfig()).format_telemetry(
        _car(), TrackConfig(session_type="Race"))
    assert "Your car:" not in without_identity


def test_prompt_marks_multiclass_closing_rates_as_traffic_not_position():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder

    ahead = Rival(name="Prototype", car_number="1", car_class_id=20,
                  car_class_name="GTP", class_est_lap_time=82.0, gap=1.2)
    car = _car(ahead=ahead, closing_ahead_status="confirmed",
               closing_ahead_change=-0.3, closing_ahead_segments=3)
    track = TrackConfig(session_type="Race", car_class_id=10,
                        car_class_name="GT3", car_class_est_lap_time=92.0)

    block = PromptBuilder(AppConfig()).format_telemetry(car, track)
    assert "Ahead on track (faster-class traffic, not a rival for position)" in block
    assert "losing to faster-class traffic (the car ahead, not for position)" in block
    assert "0.30 seconds over 3 mini-sectors" in block


def test_prompt_marks_a_same_class_car_a_lap_up_as_lapping_not_a_rival():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder

    ahead = Rival(name="Leader", car_class_id=10, laps_down=-1, gap=1.0)
    track = TrackConfig(session_type="Race", car_class_id=10)
    block = PromptBuilder(AppConfig()).format_telemetry(_car(ahead=ahead), track)
    assert "Ahead on track (car lapping you, not a rival for position)" in block


def test_a_rival_off_his_own_best_is_called_out():
    """Last lap answers "is he quick now", best answers "was he ever".
    A rival well off his own best is the tell that something is wrong."""
    field = _field()
    field[1] = Rival(name="Silva", car_number="16", position=2, gap=2.3,
                     best_lap=90.7, last_lap=94.9)
    track = TrackConfig(session_type="Race", field=field, field_size=5)
    result = rm.driver_report(_car(), track, "P2")
    assert result["last_lap"] == 94.9
    assert "off their own best" in result["spoken"]


# ── Fuel over an arbitrary window ───────────────────────────────────

def test_fuel_for_a_number_of_minutes_is_converted_here_not_by_the_model():
    """"How much fuel for twenty minutes" needs a division the model must
    never do — it read a 0.85s delta as "a tenth and a half" once already.

    20 minutes at 90s a lap is 13.3 laps, which rounds *up* to 14: a
    partial lap still has to be driven.
    """
    car = _car(fuel_level=40.0, fuel_per_lap=3.2, avg_lap_time=90.0)
    track = TrackConfig(session_type="Race", laps_remaining=10)
    result = rm.fuel_plan(car, track, target_minutes=20)
    assert result["available"]
    assert "14 laps" in result["spoken"]
    # It must not be passed off as a known lap count.
    assert "estimate" in result["spoken"]


def test_fuel_for_minutes_declines_without_a_lap_time():
    car = _car(fuel_level=40.0, fuel_per_lap=3.2,
               avg_lap_time=None, last_lap_time=None, best_lap_time=None)
    track = TrackConfig(session_type="Race", laps_remaining=10)
    result = rm.fuel_plan(car, track, target_minutes=20)
    assert not result["available"]
    assert "lap time" in result["spoken"]


# ── Penalties ───────────────────────────────────────────────────────

def test_black_and_black_with_furled_are_different_penalties():
    """Black alone is a stop-and-go; black with the furled flag is a
    slow-down. They need different driving, and _decode_flag reports a
    single most-severe label, so both read as just "Black" — the driver
    could not tell which one they were serving.

    The pairing is CrewChief's, run against live iRacing for years.
    """
    from ai_race_engineer.telemetry.irsdk_reader import _decode_penalty, _decode_flag
    BLACK, FURLED, REPAIR, DQ, YELLOW = (
        0x00010000, 0x00080000, 0x00100000, 0x00020000, 0x00000008)

    assert _decode_flag(BLACK) == _decode_flag(BLACK | FURLED) == "Black"
    assert _decode_penalty(BLACK) == "stop-and-go penalty"
    assert _decode_penalty(BLACK | FURLED) == "slow-down penalty"

    assert _decode_penalty(0) is None
    assert _decode_penalty(YELLOW) is None, "a yellow is not a penalty"
    assert "repairs" in _decode_penalty(REPAIR)
    assert _decode_penalty(DQ) == "disqualified"
    # A penalty must survive a concurrent yellow.
    assert _decode_penalty(BLACK | YELLOW) == "stop-and-go penalty"


def test_penalty_decoding_never_raises_on_junk():
    from ai_race_engineer.telemetry.irsdk_reader import _decode_penalty
    for junk in (None, "", "x", -1, 3.5):
        _decode_penalty(junk)


# ── Tyre compound ───────────────────────────────────────────────────

def test_compound_is_compared_never_named():
    """iRacing publishes an integer index and no names anywhere. Saying
    "he's on softs" would be invention; "on a different tyre to you" is
    the whole of what the data supports."""
    field = _field()
    field[1] = Rival(name="Silva", car_number="16", position=2, gap=2.3,
                     best_lap=90.7, tyre_compound=2)
    track = TrackConfig(session_type="Race", field=field, field_size=5)

    result = rm.driver_report(_car(tyre_compound=1), track, "P2")
    assert result["same_compound_as_you"] is False
    assert "different tyre compound" in result["spoken"]
    for word in ("soft", "medium", "hard", "compound 2"):
        assert word not in result["spoken"].lower()

    same = rm.driver_report(_car(tyre_compound=2), track, "P2")
    assert same["same_compound_as_you"] is True


def test_compound_is_silent_when_either_car_lacks_it():
    track = TrackConfig(session_type="Race", field=_field(), field_size=5)
    result = rm.driver_report(_car(tyre_compound=None), track, "P2")
    assert result["same_compound_as_you"] is None
    assert "tyre" not in result["spoken"].lower()


# ── How the engineer addresses the driver ───────────────────────────

def test_a_callsign_replaces_the_name_the_voice_mangles():
    """iRacing supplies a full legal name and ElevenLabs mispronounces
    some of them audibly. A name mangled on every single answer is worse
    than no name, so the callsign is the only thing spoken."""
    from ai_race_engineer.prompt_builder import _driver, _result

    track = TrackConfig(driver_name="Jean-Baptiste Grandchamp",
                        driver_short_name="Grandchamp, J.",
                        driver_car_number="44")
    line = _driver(track, "JB")
    assert "JB" in line
    assert "nothing else" in line
    # The real name stays reachable, but must be gated behind being asked.
    assert "Jean-Baptiste Grandchamp" in line
    assert "only if they ask" in line
    # The timing-screen abbreviation is another way to say the same name
    # aloud, so it must not survive either.
    assert "Grandchamp, J." not in line

    assert "JB finished P3" in _result(CarTelemetry(position=3), track, "JB")


def test_without_a_callsign_the_sim_name_is_used_as_before():
    from ai_race_engineer.prompt_builder import _driver
    track = TrackConfig(driver_name="Alex Smith", driver_car_number="7")
    line = _driver(track, "")
    assert "Alex Smith" in line and "nothing else" not in line
    assert _driver(TrackConfig(), "") == ""


def test_voice_defaults_ship_on_the_stable_preset():
    """Style above 0 was the live cause of stuttering, and expression is
    worth little on a pit radio — the shipped defaults sit at the
    consistent end of every slider."""
    from ai_race_engineer.config import _DEFAULT_CONFIG
    v = _DEFAULT_CONFIG["elevenlabs"]["voice_settings"]
    assert v["style"] == 0.0
    assert v["stability"] >= 0.75
    assert v["similarity_boost"] >= 0.85


def test_the_prompt_builds_end_to_end_with_and_without_a_callsign():
    """The callsign change was tested through _driver() alone and shipped
    broken: format_telemetry is a @staticmethod, so the `self.config` read
    added to it raised NameError on *every* question.

    Nothing else caught it — the app started, the API answered, the
    settings saved. Only asking anything failed, which no offline check
    did. So exercise the real entry point, not the helper underneath it.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import TelemetrySnapshot

    snap = TelemetrySnapshot(
        CarTelemetry(position=3),
        TrackConfig(session_type="Race", driver_name="Jean-Baptiste Grandchamp",
                    driver_car_number="44"))

    for callsign in ("", "JB"):
        config = AppConfig()
        config._data["driver_callsign"] = callsign
        builder = PromptBuilder(config)
        messages = builder.build_messages("Radio check.", snap, [], tools_enabled=True)
        assert len(messages) >= 2
        body = messages[-1]["content"]
        assert (callsign or "Jean-Baptiste Grandchamp") in body
        # The other entry point has to survive too.
        assert builder.build_prompt("Radio check.", snap)

    # A config.json written before the setting existed has no such key, and
    # AppConfig raises AttributeError rather than returning None.
    legacy = AppConfig()
    legacy._data.pop("driver_callsign", None)
    assert PromptBuilder(legacy).build_messages("Radio check.", snap, [], tools_enabled=True)


# ── Changing track or AI roster mid-session ─────────────────────────

class _FakeIrsdk:
    """Enough of pyirsdk to reproduce its session-YAML caching.

    The important part is the invalidation rule, copied from
    irsdk.py::_get_session_info — it only drops parsed data when the
    counter goes UP:

        if self.last_session_info_update < self._header.session_info_update:
    """

    def __init__(self, yaml, counter):
        self._yaml = yaml
        self.session_info_update = counter
        self.last_session_info_update = 0
        self._parsed = {}
        self.parses = 0

    def load(self, yaml, counter):
        """iRacing tears the session down and builds a new one."""
        self._yaml = yaml
        self.session_info_update = counter

    def __getitem__(self, key):
        if self.last_session_info_update < self.session_info_update:
            self.last_session_info_update = self.session_info_update
            self._parsed.clear()
        if key not in self._parsed:
            self.parses += 1
            self._parsed[key] = self._yaml.get(key)
        return self._parsed[key]


def _reader_with(ir):
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    r = R.__new__(R)
    r._ir = ir
    r._session_cache = {}
    r._session_update = -1
    r._sector_times = {}
    return r


def test_changing_track_does_not_leave_the_old_name_behind():
    """Reported from a race: the track was changed and the engineer kept
    saying Road America while the driver was at Barcelona.

    iRacing restarts the counter when it rebuilds the session, so pyirsdk's
    strict less-than never fires and it serves the previous session's YAML
    forever. Clearing our own cache achieves nothing on its own — the next
    read comes back stale from the same place.
    """
    ir = _FakeIrsdk({"WeekendInfo": {"TrackDisplayName": "Road America"}}, counter=57)
    reader = _reader_with(ir)
    assert reader._session("WeekendInfo")["TrackDisplayName"] == "Road America"

    # New session on a different track. The counter starts low again.
    ir.load({"WeekendInfo": {"TrackDisplayName": "Circuit de Barcelona"}}, counter=2)

    assert reader._session("WeekendInfo")["TrackDisplayName"] == "Circuit de Barcelona"


def test_changing_the_ai_roster_does_not_keep_the_old_drivers():
    """The other half of the same report: after editing the AI settings the
    engineer gave wrong names and car numbers.

    Car indices are reassigned when the grid is rebuilt, so a stale
    DriverInfo pairs the previous roster against live positions — every
    name and number attached to the wrong car.
    """
    ir = _FakeIrsdk({"DriverInfo": {"Drivers": [
        {"CarIdx": 1, "UserName": "Rossi", "CarNumber": "13"}]}}, counter=44)
    reader = _reader_with(ir)
    assert reader._session("DriverInfo")["Drivers"][0]["UserName"] == "Rossi"

    ir.load({"DriverInfo": {"Drivers": [
        {"CarIdx": 1, "UserName": "Silva", "CarNumber": "16"}]}}, counter=3)

    drivers = reader._session("DriverInfo")["Drivers"]
    assert drivers[0]["UserName"] == "Silva"
    assert drivers[0]["CarNumber"] == "16"


def test_a_settled_session_is_still_only_parsed_once():
    """The fix must not turn every poll into a YAML re-parse — that is what
    the cache was for, and parsing it per poll throttled the snapshot rate."""
    ir = _FakeIrsdk({"WeekendInfo": {"TrackDisplayName": "Spa"}}, counter=9)
    reader = _reader_with(ir)
    for _ in range(50):
        reader._session("WeekendInfo")
    assert ir.parses == 1, f"re-parsed {ir.parses} times while nothing changed"


def _reader_for_identity(weekend):
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    from collections import deque
    r = R.__new__(R)
    r._session_cache = {}
    r._session_update = -1
    r._session_identity = None
    r._laps = deque(maxlen=120)
    r._fuel_usage = deque(maxlen=5)
    r._gap_hist_ahead = deque(maxlen=40)
    r._gap_hist_behind = deque(maxlen=40)
    r._prev_lap = {}
    r._sector_prev, r._sector_marks = {}, {}
    r._sector_times, r._sector_dirty = {}, {}
    r._sector_best = [12.3]
    r._lap_marker = 7
    r._lap_dirty = True
    r._lap_dirty_reason = "off track"
    r._last_lap_clean = False
    r._fuel_marker = (7, 40.0)
    r._fuel_marker_is_partial = False
    r._last_lap_fuel = 3.1
    r._stint_start_fuel = 50.0
    r._pit_lap = 4
    r._oil_level_start = 5.0
    r._water_level_start = 6.0
    r._tyre_signature = (1, 2, 3)
    r._tyre_changed_at = 123.0
    r._session = lambda key, default=None: weekend[0] if key == "WeekendInfo" else default
    r._laps.extend([1, 2, 3])
    r._fuel_usage.extend([3.0, 3.1])
    return r


def test_a_track_change_drops_measurements_from_the_old_track():
    """Lap times feed the representative lap behind laps-remaining, and the
    burn behind every fuel call. Carried across a track change they are not
    stale readings — they are confident wrong ones."""
    box = [{"SubSessionID": 1, "SessionID": 1, "TrackID": 18,
            "TrackDisplayName": "Road America"}]
    r = _reader_for_identity(box)
    r._check_session_identity()                       # first read: adopt
    assert len(r._laps) == 3

    box[0] = {"SubSessionID": 2, "SessionID": 1, "TrackID": 249,
              "TrackDisplayName": "Circuit de Barcelona"}
    r._check_session_identity()

    assert not r._laps and not r._fuel_usage
    assert r._fuel_marker is None and r._last_lap_fuel is None
    assert r._lap_marker is None and r._pit_lap == 0
    assert r._oil_level_start is None and r._water_level_start is None
    assert r._tyre_signature is None


def test_a_routine_republish_keeps_the_history():
    """session_info_update bumps on flags, incidents and practice rolling
    into qualifying. Clearing lap times on those would destroy the data the
    fuel and pace answers are built from — which is why the identity check
    is on track and subsession, not on the counter."""
    box = [{"SubSessionID": 1, "SessionID": 1, "TrackID": 18,
            "TrackDisplayName": "Road America"}]
    r = _reader_for_identity(box)
    r._check_session_identity()
    for _ in range(5):
        r._check_session_identity()
    assert len(r._laps) == 3
    assert r._fuel_marker == (7, 40.0)


def test_same_track_restart_drops_history_when_the_session_clock_resets():
    weekend = [{"SubSessionID": 0, "SessionID": 0, "TrackID": 345,
                "TrackDisplayName": "Barcelona"}]
    channels = {"SessionNum": 2, "SessionTime": 900.0, "SessionTick": 54000,
                "IsReplayPlaying": False}
    r = _reader_for_identity(weekend)
    r._get = lambda key, default=None: channels.get(key, default)
    r._check_session_identity()

    channels.update({"SessionTime": 1.0, "SessionTick": 60})
    r._check_session_identity()

    assert not r._laps and not r._fuel_usage
    assert r._fuel_marker is None and r._lap_marker is None


def test_segment_clock_reset_keeps_same_track_practice_measurements():
    weekend = [{"SubSessionID": 0, "SessionID": 0, "TrackID": 345,
                "TrackDisplayName": "Barcelona"}]
    channels = {"SessionNum": 0, "SessionTime": 900.0, "SessionTick": 54000,
                "IsReplayPlaying": False}
    r = _reader_for_identity(weekend)
    r._get = lambda key, default=None: channels.get(key, default)
    r._check_session_identity()

    channels.update({"SessionNum": 1, "SessionTime": 1.0, "SessionTick": 60})
    r._check_session_identity()

    assert len(r._laps) == 3
    assert list(r._fuel_usage) == [3.0, 3.1]


def test_replay_scrubbing_is_not_a_new_live_session():
    weekend = [{"SubSessionID": 0, "SessionID": 0, "TrackID": 345,
                "TrackDisplayName": "Barcelona"}]
    channels = {"SessionNum": 2, "SessionTime": 900.0, "SessionTick": 54000,
                "IsReplayPlaying": True}
    r = _reader_for_identity(weekend)
    r._get = lambda key, default=None: channels.get(key, default)
    r._check_session_identity()

    channels.update({"SessionTime": 100.0, "SessionTick": 6000})
    r._check_session_identity()

    assert len(r._laps) == 3
    assert list(r._fuel_usage) == [3.0, 3.1]


def test_identity_is_not_adopted_from_an_empty_weekend_block():
    """WeekendInfo can be missing while the YAML is still parsing; adopting
    a blank identity would fire a false change on the very next poll."""
    box = [{}]
    r = _reader_for_identity(box)
    r._check_session_identity()
    assert r._session_identity is None
    assert len(r._laps) == 3


def test_the_identity_check_is_actually_wired_into_every_snapshot():
    """The reset tests above call _check_session_identity() directly, so
    they pass whether or not anything invokes it — deleting the call from
    _read_snapshot left all of them green.

    A snapshot is hard to build offline (it touches ~90 channels), so guard
    the wiring rather than the behaviour: the call must be there, and it
    must come before the readings it protects.
    """
    import inspect
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader

    src = inspect.getsource(iRacingTelemetryReader._read_snapshot)
    assert "_check_session_identity()" in src, \
        "nothing clears history when the session changes"
    assert src.index("_check_session_identity()") < src.index("_update_laps()"), \
        "history must be cleared before this session's laps are recorded"


# ── Units ───────────────────────────────────────────────────────────

def _fmt(units, **car_kwargs):
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    car = CarTelemetry(units=units, **car_kwargs)
    return PromptBuilder(AppConfig()).format_telemetry(car, TrackConfig(session_type="Race"))


def test_temperatures_speed_and_distance_follow_the_chosen_system():
    fields = dict(speed=185.0, air_temp=22.0, track_surface_temp=32.0,
                  tire_fl_temp=89.0, water_temp=95.0, air_pressure=1013.0)
    metric = _fmt("metric", **fields)
    assert "185 km/h" in metric and "22 C" in metric and "1013 hPa" in metric

    imperial = _fmt("imperial", **fields)
    assert "115 mph" in imperial          # 185 km/h
    assert "72 F" in imperial             # 22 C
    assert "29.91 inHg" in imperial       # 1013 hPa, two decimals
    assert "C" not in imperial.replace("Celsius", "").split("Track conditions")[1][:40]


def test_no_metric_unit_survives_an_imperial_render():
    """A single leftover 'Celsius' is the engineer telling a US driver a
    number in units they do not read — the same class of wrong as the fuel
    unit, which is why fuel got its own setting first."""
    imperial = _fmt("imperial", speed=185.0, air_temp=22.0,
                    track_surface_temp=32.0, tire_fl_temp=89.0,
                    water_temp=95.0, air_pressure=1013.0)
    for metric_unit in ("Celsius", "km/h", "hPa"):
        assert metric_unit not in imperial, f"{metric_unit} leaked into an imperial block"


def test_unknown_readings_stay_unknown_in_both_systems():
    """A missing channel must not convert into a confident 32 F."""
    for system in ("metric", "imperial"):
        out = _fmt(system, air_temp=None, speed=0.0)
        assert "unknown" in out


def test_auto_follows_the_sim():
    """DisplayUnits: 0 = english, 1 = metric. Auto exists so the engineer
    reads back what the driver already sees on their own screen."""
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    for display_units, expected in ((0, "imperial"), (1, "metric")):
        r = R.__new__(R)
        r._units_setting = "auto"
        r._get = lambda k, d=None, _v=display_units: _v if k == "DisplayUnits" else d
        assert r._unit_system() == expected

    # An explicit choice overrides the sim.
    r = R.__new__(R)
    r._units_setting = "imperial"
    r._get = lambda k, d=None: 1 if k == "DisplayUnits" else d
    assert r._unit_system() == "imperial"


def test_a_units_change_swaps_the_adapter_rather_than_waiting_for_a_restart():
    """The source and fuel-unit settings are both in that comparison because
    a setting missing from it silently does nothing until the app restarts —
    which cost an evening the first time."""
    import inspect
    from ai_race_engineer.orchestrator import Orchestrator
    src = inspect.getsource(Orchestrator)
    block = src[src.index("Swap the telemetry adapter"):]
    block = block[:block.index("Rebind the hotkey")]
    assert "new_units != self._units" in block, \
        "changing units would not take effect until restart"


# ── Sector timing ───────────────────────────────────────────────────

def _sector_reader(bounds, player=0):
    """A reader wired for sector timing only, with the YAML stubbed."""
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    r = R.__new__(R)
    r._sector_prev, r._sector_marks = {}, {}
    r._sector_times, r._sector_dirty = {}, {}
    r._sector_best = []
    r._channels = {}
    r._session = lambda key, default=None: (
        {"Sectors": [{"SectorNum": n, "SectorStartPct": b}
                     for n, b in enumerate(bounds)]}
        if key == "SplitTimeInfo" else default)
    r._get = lambda k, d=None: r._channels.get(k, d)
    r._channels["PlayerCarIdx"] = player
    return r


def _drive(reader, laps, lap_time, hz=10.0, cars=1, offset=0.0):
    """Run a car round at constant speed, sampling at `hz`."""
    dt = 1.0 / hz
    t = 0.0
    # Half a second past the final line: the wrap is what closes a lap, and
    # accumulated float error otherwise ends the loop one sample short of it.
    total = laps * lap_time + offset + 0.5
    while t <= total:
        pct = ((t - offset) % lap_time) / lap_time if t >= offset else 0.0
        reader._channels["SessionTime"] = t
        reader._channels["CarIdxLapDistPct"] = [pct] * cars
        reader._channels["CarIdxOnPitRoad"] = [False] * cars
        reader._channels["CarIdxTrackSurface"] = [3] * cars
        reader._update_sectors()
        t += dt


def test_sector_times_are_interpolated_not_snapped_to_a_poll():
    """At 10 Hz a car covers one to two percent of a lap between samples.

    Taking the crossing as "the first poll that read past the boundary"
    puts sectors out by up to a tenth — useless against sector times that
    differ by hundredths, and exactly the confident wrong number this app
    exists to avoid. The crossing is interpolated between the two samples.

    **The numbers here are chosen so the crossings fall between polls.**
    The first version of this test used a 90s lap with boundaries at 30%
    and 70%, which put every crossing exactly on the 0.1s sample grid — so
    snapping and interpolating agreed and the test passed with the bug in.
    91.37s at 31% and 68% lands nothing on the grid.
    """
    lap = 91.37
    r = _sector_reader([0.0, 0.31, 0.68])
    _drive(r, laps=3, lap_time=lap)

    times = r._sector_times[0]
    expect = (0.31 * lap, (0.68 - 0.31) * lap, (1.0 - 0.68) * lap)
    assert len(times) == 3
    for n, (got, want) in enumerate(zip(times, expect)):
        assert got is not None, f"sector {n + 1} never timed"
        assert abs(got - want) < 0.01, \
            f"sector {n + 1}: {got:.3f} vs {want:.3f} — snapped, not interpolated"
    assert abs(sum(times) - lap) < 0.01, "sectors do not add up to the lap"


def test_sector_count_comes_from_the_track_not_a_hardcoded_three():
    """CrewChief splits iRacing laps into fixed thirds. Real tracks vary,
    and the boundaries are published — so nothing here assumes three."""
    r = _sector_reader([0.0, 0.2, 0.45, 0.6, 0.85])
    _drive(r, laps=3, lap_time=100.0)
    times = r._sector_times[0]
    assert len(times) == 5
    for got, want in zip(times, (20.0, 25.0, 15.0, 25.0, 15.0)):
        assert abs(got - want) < 0.01


def test_a_track_with_no_published_sectors_reports_nothing():
    """Some sessions publish only the start/finish line. That is the lap
    timer we already have, not sectors — better silent than inventing a
    split at a third of the way round."""
    r = _sector_reader([0.0])
    _drive(r, laps=2, lap_time=90.0)
    assert r._sector_times == {}
    assert r._sector_bounds() == []


def test_a_dirty_lap_never_sets_a_best_sector():
    """A lap through the pits or across the grass would otherwise set a
    'best' nobody can repeat, and every later comparison measures against
    a lap that never happened."""
    r = _sector_reader([0.0, 0.30, 0.70])
    _drive(r, laps=2, lap_time=90.0)
    clean = list(r._sector_best)
    assert clean and all(b is not None for b in clean)

    # Same speed, but the car is on pit road for part of it.
    r2 = _sector_reader([0.0, 0.30, 0.70])
    dt, t = 0.1, 0.0
    while t <= 200.0:
        pct = (t % 90.0) / 90.0
        r2._channels["SessionTime"] = t
        r2._channels["CarIdxLapDistPct"] = [pct]
        r2._channels["CarIdxOnPitRoad"] = [0.4 < pct < 0.5]
        r2._channels["CarIdxTrackSurface"] = [3]
        r2._update_sectors()
        t += dt
    assert r2._sector_times.get(0), "the lap should still be timed"
    assert not any(b is not None for b in r2._sector_best), \
        "a pit lap set a personal best sector"


def test_every_car_is_timed_not_only_the_driver():
    """"How do my sectors compare to the car ahead" needs their sectors
    too, and CarIdxLapDistPct carries the whole field."""
    r = _sector_reader([0.0, 0.30, 0.70])
    dt, t = 0.1, 0.0
    while t <= 280.0:
        r._channels["SessionTime"] = t
        r._channels["CarIdxLapDistPct"] = [(t % 90.0) / 90.0, (t % 96.0) / 96.0]
        r._channels["CarIdxOnPitRoad"] = [False, False]
        r._channels["CarIdxTrackSurface"] = [3, 3]
        r._update_sectors()
        t += dt
    assert 0 in r._sector_times and 1 in r._sector_times
    assert abs(sum(r._sector_times[0]) - 90.0) < 0.03
    assert abs(sum(r._sector_times[1]) - 96.0) < 0.03


# ── The tool ────────────────────────────────────────────────────────

def test_the_worst_sector_is_the_one_named():
    car = _car(sector_times=[28.4, 41.2, 21.9], sector_bests=[28.2, 40.1, 21.7])
    out = rm.sector_report(car, TrackConfig(session_type="Race"))
    assert out["available"]
    assert out["worst_sector"] == 2
    assert "Sector 2" in out["spoken"]
    # Sum of bests is a lap nobody drove, so it is offered as what is on
    # the table and never as a lap time.
    assert out["theoretical_best"] == 90.0


def test_sector_report_declines_before_a_lap_is_timed():
    out = rm.sector_report(_car(sector_times=[]), TrackConfig(session_type="Race"))
    assert not out["available"]
    out = rm.sector_report(_car(sector_times=[28.4, None, None]),
                           TrackConfig(session_type="Race"))
    assert not out["available"], "no clean best to compare against yet"


def test_sector_comparison_against_a_rival():
    field = _field()
    field[1] = Rival(name="Silva", car_number="16", position=2, gap=2.3,
                     best_lap=90.7, sector_times=[28.0, 40.0, 22.4])
    track = TrackConfig(session_type="Race", field=field, field_size=5)
    car = _car(sector_times=[28.4, 41.2, 21.9], sector_bests=[28.2, 40.1, 21.7])
    out = rm.sector_report(car, track, "P2")
    assert out["available"]
    assert out["worst_sector"] == 2          # 41.2 vs 40.0
    assert out["best_sector"] == 3           # 21.9 vs 22.4, the one gain
    assert "Silva" in out["spoken"]


# ── Two live-race bugs ──────────────────────────────────────────────

def test_the_drivers_own_sectors_are_found_when_their_car_index_is_zero():
    """`PlayerCarIdx or -1` looked the driver's sectors up under -1,
    because 0 is falsy — so sector times were empty for every car index 0,
    which is most of them. Reported from a race as "sector times do not
    work at all", after several laps across several sessions.

    Same class as the leader's gap being permanently unavailable: zero is
    a reading, not a missing value.
    """
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    r = R.__new__(R)
    r._sector_times = {0: [28.4, 41.2, 21.9]}
    r._get = lambda k, d=None: 0 if k == "PlayerCarIdx" else d

    idx = _int_or_shim(r._get("PlayerCarIdx"), -1)
    assert idx == 0, "car index 0 must survive the lookup"
    assert r._sector_times.get(idx), "the driver's own sectors went missing"


def _int_or_shim(value, default):
    from ai_race_engineer.telemetry.irsdk_reader import _int_or
    return _int_or(value, default)


def test_int_or_keeps_zero_where_or_would_discard_it():
    from ai_race_engineer.telemetry.irsdk_reader import _int_or
    assert _int_or(0, -1) == 0
    assert (0 or -1) == -1, "which is exactly the bug this replaced"
    assert _int_or(None, -1) == -1
    assert _int_or("x", -1) == -1


def test_the_closing_rate_resets_when_the_car_ahead_changes():
    """Overtake someone, or watch the car ahead pit, and the gap jumps to
    a completely different car. Measuring across that gives a closing speed
    nobody is closing at — a confident wrong number of the worst kind,
    because it sounds like exactly the answer the question wanted.
    """
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R
    from collections import deque

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)

    for progress, gap in ((0.10, 5.0), (0.15, 4.9),
                          (0.20, 4.8), (0.25, 4.7)):
        trend = r._closing_trend(hist, gap, "44|Rossi", 4, progress)
    assert trend["status"] == "confirmed"

    # The car ahead is now somebody else, eight seconds up the road.
    quiet = r._closing_trend(hist, 9.4, "16|Silva", 4, 0.30)
    assert quiet["status"] == "waiting"
    assert len(hist) == 1, "closing trend spanned two different cars"


# ── Saying names the voice can manage ───────────────────────────────

def test_track_names_are_shortened_the_way_an_engineer_says_them():
    from ai_race_engineer import pronounce
    out = pronounce.apply("Two tenths off at Circuit de Spa-Francorchamps.")
    assert "Spa" in out and "Francorchamps" not in out


def test_accents_are_folded_because_that_is_what_the_voice_stumbles_on():
    """Hungarian, Polish and Nordic names are the ones that break. Folding
    does not make them *correct* — Kovacs is not how Kovács sounds — but it
    is stable, and a name said the same plain way every time beats one that
    slurs at random."""
    from ai_race_engineer import pronounce
    assert pronounce.apply("Kovács Bálint") == "Kovacs Balint"
    assert pronounce.apply("Świątek and Łukasz") == "Swiatek and Lukasz"
    assert pronounce.apply("Björn Grønvold") == "Bjorn Gronvold"
    assert pronounce.apply("Strauß") == "Strauss"


def test_a_table_entry_never_doubles_an_article():
    """"the Nurburgring" as a value produces "the the Nurburgring" the
    moment the sentence already had one — which it did."""
    from ai_race_engineer import pronounce
    out = pronounce.apply("Fastest lap at the Nürburgring Nordschleife.")
    assert "the the" not in out
    assert "Nurburgring" in out
    for value in pronounce.build_table().values():
        assert not value.lower().startswith("the "), \
            f"{value!r} will double an article"


def test_the_drivers_own_entries_win_over_the_built_ins():
    from ai_race_engineer import pronounce
    assert pronounce.apply("Kovács is quicker", {"kovács": "Kovach"}) \
        == "Kovach is quicker"
    # Matching is case-insensitive and on whole words only.
    assert pronounce.apply("KOVÁCS", {"kovács": "Kovach"}) == "Kovach"
    assert "Kovach" not in pronounce.apply("Kovácsson", {"kovács": "Kovach"})


def test_longer_phrases_win_so_a_short_entry_cannot_strand_the_rest():
    from ai_race_engineer import pronounce
    out = pronounce.apply("at Circuit de Spa-Francorchamps",
                          {"spa": "SPAAA"})
    assert "SPAAA" not in out, "a bare 'spa' matched inside the full name"


def test_respelling_happens_at_the_speech_boundary_only():
    """The engineer must still reason about the real name and the exchange
    log must still record it — only the audio request sees the respelling.
    Doing it any earlier would put "Spa" in the transcript and lose the
    thing the driver actually said.
    """
    import inspect
    from ai_race_engineer import tts, prompt_builder

    assert "pronounce.apply" in inspect.getsource(tts.TTS.synthesize), \
        "nothing respells the text before it is spoken"
    # Match the call, not the word — "mispronounced" appears in a comment
    # there, and an assertion that trips on prose is a test about wording.
    prompt_src = inspect.getsource(prompt_builder)
    assert "pronounce.apply" not in prompt_src, \
        "the prompt must carry real names, not spoken ones"
    assert "import pronounce" not in prompt_src


def test_respelling_is_inside_the_cache_key_not_outside_it():
    """Two answers that sound identical should share one cached clip, and
    editing the table must invalidate the old audio rather than keep
    serving a name the driver just fixed."""
    import inspect
    src = inspect.getsource(__import__("ai_race_engineer.tts",
                                       fromlist=["TTS"]).TTS.synthesize)
    assert src.index("pronounce.apply") < src.index("cache_key"), \
        "the cache would key on the pre-respelling text"


def test_risky_names_are_detected_not_listed():
    """iRacing has hundreds of thousands of drivers. A curated table cannot
    cover that, so the names that break the voice are found by shape."""
    from ai_race_engineer.pronounce import looks_hard
    # The one that actually artefacted in a race, via its consonant run.
    assert looks_hard("Baldur Karlsson")
    for hungarian in ("Nagy", "Kovács", "Szabó"):
        assert looks_hard(hungarian), hungarian
    for plain in ("Rossi", "Silva", "Smith", "Hamilton", "Verstappen"):
        assert not looks_hard(plain), plain


def test_the_fallback_is_the_car_number_because_getting_it_wrong_is_free():
    """A false positive says "the 7 car" instead of a name — which is what
    a real engineer says half the time anyway. That is what makes a guess
    acceptable here: the wrong branch is still a correct radio call."""
    from ai_race_engineer import pronounce
    from ai_race_engineer.telemetry import Rival
    grid = [Rival(name="Baldur Karlsson", car_number="7"),
            Rival(name="Marco Rossi", car_number="13")]
    table = pronounce.auto_table(grid)
    assert table["karlsson"] == "car 7"
    assert not any("rossi" in k for k in table), "a plain name was replaced"

    spoken = pronounce.apply("Karlsson is ahead of Rossi.", table)
    assert "Car 7" in spoken and "Rossi" in spoken


def test_the_driver_is_never_turned_into_their_own_car_number():
    """The driver is in the field list too, and they are the one being
    spoken to. "Car 7, you're P4" is not something an engineer says, and
    their own name has a lever already — the callsign setting."""
    from ai_race_engineer import pronounce
    from ai_race_engineer.telemetry import Rival
    grid = [Rival(name="Baldur Karlsson", car_number="7", is_player=True),
            Rival(name="Anja Szymanska", car_number="9")]
    table = pronounce.auto_table(grid)
    assert not any("karlsson" in k for k in table), \
        "the engineer would call its own driver a car number"
    assert table["anja szymanska"] == "car 9", "rivals still substituted"


def test_two_spellings_of_one_track_do_not_get_said_twice():
    """Several spellings collapse to the same word by design, so an answer
    that offers both forms — a natural thing to say — came out as "you're
    at Spa, Spa to you and me". Heard live, and blamed on the voice."""
    from ai_race_engineer import pronounce
    out = pronounce.apply(
        "You're at Circuit de Spa-Francorchamps - Spa to you and me, Bart.")
    assert out.count("Spa") == 1, out

    # The driver being addressed sits between the two names as often as
    # not, and collapsing must not swallow them.
    named = pronounce.apply("Spa, Bart - Circuit de Spa-Francorchamps.")
    assert named.count("Spa") == 1 and "Bart" in named, named

    # Two real mentions with a clause between them are not a repeat.
    real = pronounce.apply("Spa is quick today, and the last sector at "
                           "Circuit de Spa-Francorchamps is where you lose it.")
    assert real.count("Spa") == 2, real


def test_a_name_with_no_car_number_is_left_alone():
    """Substituting a number we do not have would say "car " and stop."""
    from ai_race_engineer import pronounce
    from ai_race_engineer.telemetry import Rival
    assert pronounce.auto_table([Rival(name="Kovács", car_number="")]) == {}


def test_the_policy_can_force_or_forbid_numbers():
    from ai_race_engineer import pronounce
    from ai_race_engineer.telemetry import Rival
    grid = [Rival(name="Marco Rossi", car_number="13")]
    assert pronounce.auto_table(grid, "names") == {}
    assert pronounce.auto_table(grid, "numbers")["marco rossi"] == "car 13"
    assert pronounce.auto_table(grid, "auto") == {}, "Rossi is not risky"


def test_the_drivers_own_spelling_beats_the_automatic_number():
    """Their table is deliberate; the automatic one is only ever a guess."""
    import inspect
    from ai_race_engineer.tts import TTS
    src = inspect.getsource(TTS.synthesize)
    assert src.index("self._auto_pronunciation") < src.index("self._pronunciation"), \
        "the automatic table would overwrite the driver's own"


def test_a_substitution_at_a_sentence_start_is_capitalised():
    from ai_race_engineer import pronounce
    out = pronounce.apply("Karlsson is ahead.", {"karlsson": "car 7"})
    assert out.startswith("Car 7")


def test_the_closing_window_is_actually_reachable():
    """Three consecutive mini-sectors are enough for a confirmed trend."""
    from collections import deque
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)
    for progress, gap in ((0.10, 6.0), (0.15, 5.9),
                          (0.20, 5.8), (0.25, 5.7)):
        trend = r._closing_trend(hist, gap, "44|Rossi", 4, progress)

    assert trend["status"] == "confirmed"
    assert trend["segments"] == 3
    assert trend["change_seconds"] == 0.3
    assert trend["rate"] is None


def test_early_adjacent_movement_is_qualitative_not_a_numeric_rate():
    from collections import deque
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)
    r._closing_trend(hist, 6.0, "44|Rossi", 4, 0.10)
    trend = r._closing_trend(hist, 5.9, "44|Rossi", 4, 0.15)

    assert trend["status"] == "early"
    assert trend["rate"] is None
    assert trend["change_seconds"] == 0.1


def test_a_direction_change_returns_fluctuating_instead_of_waiting_forever():
    from collections import deque
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)
    for progress, gap in ((0.10, 6.0), (0.15, 5.9),
                          (0.20, 6.0), (0.25, 5.9)):
        trend = r._closing_trend(hist, gap, "44|Rossi", 4, progress)

    assert trend["status"] == "fluctuating"
    assert trend["rate"] is None


def test_adjacent_gap_tool_reports_timing_change_without_contact_prediction():
    ahead = Rival(name="Rossi", car_number="13", position=2,
                  gap=0.8, gap_metres=40.0)
    car = _car(ahead=ahead, closing_ahead=None,
               closing_ahead_status="confirmed",
               closing_ahead_seconds=10.0,
               closing_ahead_change=0.3,
               closing_ahead_segments=3)

    result = rm.adjacent_gap_trend(
        car, TrackConfig(session_type="Race"), "ahead")

    assert result["available"]
    assert result["confidence"] == "confirmed"
    assert result["contact_seconds"] is None
    assert result["gap_change_seconds"] == 0.3
    assert "mini-sectors" in result["spoken"]
    assert "metres per second" not in result["spoken"]
    assert "lap" not in result["spoken"].lower()
    assert result["spoken"].count("Rossi") == 1
    assert "Measured over" not in result["spoken"]


def test_an_early_adjacent_rate_never_becomes_a_contact_prediction():
    ahead = Rival(name="Rossi", car_number="13", position=2,
                  gap=0.8, gap_metres=40.0)
    car = _car(ahead=ahead, closing_ahead=0.5,
               closing_ahead_status="early", closing_ahead_seconds=2.6,
               closing_ahead_change=0.1, closing_ahead_segments=1)

    result = rm.adjacent_gap_trend(
        car, TrackConfig(session_type="Race"), "ahead")

    assert result["contact_seconds"] is None
    assert "right now" in result["spoken"].lower()
    assert "per second" not in result["spoken"].lower()
    assert "lap" not in result["spoken"].lower()
    assert result["spoken"].count("Rossi") == 1
    assert "2.6 seconds of live distance" not in result["spoken"]


def test_closing_trend_uses_estimated_time_at_fixed_track_points():
    """A static time gap is distance divided by current player speed.

    Braking can double that value with neither car gaining a metre, so its
    change cannot safely be described as a closing rate.
    """
    import inspect
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    source = inspect.getsource(R._read_snapshot)
    rivals = inspect.getsource(R._rivals)
    assert "ahead.trend_gap" in source
    assert "behind.trend_gap" in source
    assert "timing_gap = self._gap_seconds" in rivals
    assert "gap = seconds if seconds is not None else timing_gap" in rivals


def test_flat_or_missing_distance_never_reuses_a_closing_call():
    from collections import deque
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)
    for progress in (0.10, 0.15, 0.20, 0.25):
        trend = r._closing_trend(hist, 6.0, "44|Rossi", 4, progress)
    assert trend["status"] == "steady"
    assert trend["change_seconds"] == 0.0

    # A dropped current reading must not replay a rate calculated earlier.
    assert r._closing_trend(hist, None, "44|Rossi", 4, 0.30)["status"] \
        == "unavailable"


def test_one_bad_distance_sample_cannot_create_a_closing_rate():
    from collections import deque
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    r = R.__new__(R)
    r._closing_who = {}
    hist = deque(maxlen=40)
    for progress, gap in ((0.10, 6.0), (0.15, 5.9),
                          (0.20, 2.0), (0.25, 1.9)):
        trend = r._closing_trend(hist, gap, "44|Rossi", 4, progress)

    assert trend["status"] == "fluctuating"
    assert trend["change_seconds"] is None


def test_an_unmeasured_closing_rate_says_so_rather_than_going_silent():
    """Silence is what the model filled in — it offered that the rival
    might be in the pits, which was untrue and was not in the telemetry.
    Stating the real reason removes the gap it was filling."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import Rival

    car = _car(ahead=Rival(name="Rossi", car_number="13", position=2, gap=1.4),
               closing_ahead=None)
    block = PromptBuilder(AppConfig()).format_telemetry(
        car, TrackConfig(session_type="Race"))
    assert "still measuring" in block
    assert "NEVER laps" in block


def test_the_conditions_line_is_named_after_the_question_it_answers():
    """Asked about track conditions the engineer refused, while holding
    track temperature, air temperature and surface state — it read the
    no-forecast rule as covering conditions altogether."""
    block = _fmt("metric", air_temp=22.0, track_surface_temp=32.0)
    assert "Track conditions" in block
    assert "forecast" in block, "nothing distinguishes now from later"


def test_fuel_in_hand_is_stated_as_a_conclusion_not_left_as_a_sum():
    """Given a bare margin the engineer framed fuel as a worry with laps of
    it spare — asked for advice it warned about running out on a tank that
    finishes the race."""
    comfortable = _fmt("metric", fuel_per_lap=2.5, fuel_margin=8.0,
                       fuel_needed=40.0, laps_to_go=16)
    assert "NOT a concern" in comfortable
    assert "3.2 laps in hand" in comfortable

    # Under a lap of margin is a real one, and must stay unqualified.
    thin = _fmt("metric", fuel_per_lap=2.5, fuel_margin=1.0,
                fuel_needed=40.0, laps_to_go=16)
    assert "NOT a concern" not in thin


def test_an_unsaveable_shortfall_in_the_block_forbids_a_save_figure():
    """The block hands over margin, burn and laps to go, so a save-per-lap
    figure is one division away — and the engineer did it, telling a driver
    burning 3.5 a lap to save 3.1 of it. Short by 10.5 over 15 laps at 3.5
    is 0.7 a lap, a fifth of the lap's fuel: not saveable, so the block has
    to say so rather than leave the sum sitting there."""
    block = _fmt("metric", fuel_per_lap=3.5, fuel_margin=-10.5,
                 fuel_needed=52.5, laps_to_go=15)
    assert "SHORT by" in block
    assert "do NOT" in block and "save-per-lap" in block
    assert "a stop will be required" in block
    assert "does NOT mean pit now" in block

    # Within lift and coast, the target is real advice and must survive.
    saveable = _fmt("metric", fuel_per_lap=3.5, fuel_margin=-1.5,
                    fuel_needed=52.5, laps_to_go=15)
    assert "SHORT by" in saveable
    assert "do NOT" not in saveable


# ── Practice and qualifying: people come and go ─────────────────────

def test_a_rivals_rating_actually_reaches_the_standings_row():
    """`irating` was read per driver and never copied onto the row, so
    Rival.irating was None for every car in every session — "what's his
    iRating" could not be answered and the field-strength line rendered
    empty. Nothing offline caught it: the simulated adapter does not set
    these either, so the fixtures agreed with the bug."""
    import inspect
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader
    src = inspect.getsource(iRacingTelemetryReader._standings)
    for wanted in ("irating=info", "licence=info", "is_ai="):
        assert wanted in src, f"{wanted} never reaches the standings row"


def test_a_driver_who_has_left_is_not_talked_about_as_a_rival():
    """In practice people join and leave all session and iRacing keeps
    their times, so a rival can hold P3 on a lap set from a garage they
    have already quit."""
    from ai_race_engineer.race_math import driver_report
    from ai_race_engineer.telemetry import Rival

    gone = Rival(name="Anja Szymanska", car_number="9", position=3,
                 best_lap=91.4, in_world=False)
    track = TrackConfig(session_type="Practice", field=[
        Rival(name="You", car_number="1", position=1, is_player=True), gone])
    out = driver_report(_car(), track, "Szymanska")
    assert out["available"]
    assert out["in_session"] is False
    assert "set earlier" in out["spoken"], out["spoken"]


def test_the_running_order_marks_the_cars_nobody_is_racing():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import Rival

    track = TrackConfig(session_type="Practice", field_size=2, field=[
        Rival(name="Anja Szymanska", car_number="9", position=1, in_world=False),
        Rival(name="You", car_number="1", position=2, is_player=True)])
    block = PromptBuilder(AppConfig()).format_telemetry(_car(), track)
    assert "not on track, time from earlier" in block


def test_the_sims_own_words_for_where_a_car_is_are_not_read_on_air():
    """_enum_map returns pyirsdk's identifiers, and they reach the driver's
    ears. "not in world" is engine vocabulary, and "aproaching pits" is
    misspelled in the library — read aloud it came out as the typo."""
    from ai_race_engineer.telemetry.irsdk_reader import _SPOKEN_SURFACE
    assert _SPOKEN_SURFACE["not in world"] == "not in the session"
    assert _SPOKEN_SURFACE["aproaching pits"] == "in the pit lane"


# ── Finding a driver the way one is actually named on radio ─────────

def test_a_name_is_found_through_its_accents():
    """The roster holds "Kovács" and speech recognition writes "Kovacs".
    Every match in _find was a substring test between those two, so asking
    about a driver by name failed for exactly the names the driver was
    most likely to ask about."""
    from ai_race_engineer.race_math import _find
    from ai_race_engineer.telemetry import Rival
    field = [Rival(name="Kovács Bálint", short_name="Kovács, B.",
                   car_number="44", position=3),
             Rival(name="Marco Rossi", short_name="Rossi, M.",
                   car_number="7", position=1)]
    for spoken in ("kovacs", "Kovács", "kovacs balint", "balint"):
        assert _find(field, spoken) is field[0], spoken


def test_a_near_miss_from_speech_recognition_still_finds_the_car():
    from ai_race_engineer.race_math import _find
    from ai_race_engineer.telemetry import Rival
    field = [Rival(name="Kovács Bálint", car_number="44", position=3),
             Rival(name="Marco Rossi", car_number="7", position=1)]
    assert _find(field, "Kovach") is field[0]
    # But a name that is simply not here must still come back empty rather
    # than being fuzzed onto the nearest car.
    assert _find(field, "Verstappen") is None


def test_the_way_a_driver_says_a_car_number_out_loud():
    from ai_race_engineer.race_math import _find
    from ai_race_engineer.telemetry import Rival
    field = [Rival(name="Kovács Bálint", car_number="44", position=3),
             Rival(name="Marco Rossi", car_number="7", position=1)]
    assert _find(field, "the 44") is field[0]


# ── Setup advice the driver cannot act on ───────────────────────────

def test_a_fixed_setup_session_says_so():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    track = TrackConfig(session_type="Race", fixed_setup=True)
    block = PromptBuilder(AppConfig()).format_telemetry(_car(), track)
    assert "FIXED by the series" in block
    assert "Never suggest a setup change" in block

    open_setup = PromptBuilder(AppConfig()).format_telemetry(
        _car(), TrackConfig(session_type="Race"))
    assert "FIXED by the series" not in open_setup


def test_camber_is_not_named_when_the_setup_is_locked():
    """The reading is real; the remedy is greyed out. Naming camber sends
    the driver hunting for a control they do not have."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import TyreState

    hot_inner = TyreState(inner_temp=98.0, middle_temp=88.0, outer_temp=84.0)
    car = _car(tyre_fl=hot_inner)

    def profile(block):
        return next(l for l in block.splitlines() if "Tyre profile" in l)

    fixed = profile(PromptBuilder(AppConfig()).format_telemetry(
        car, TrackConfig(session_type="Race", fixed_setup=True)))
    assert "inner edge running hot" in fixed
    assert "camber" not in fixed, fixed

    changeable = profile(PromptBuilder(AppConfig()).format_telemetry(
        car, TrackConfig(session_type="Race")))
    assert "camber" in changeable


def test_no_setup_call_is_made_from_tyre_data_that_has_stopped_updating():
    """iRacing stops refreshing tyre telemetry for outside apps once the
    car leaves the pits, so what is left describes the last stop. A camber
    call off that profile is a setup change argued from the out-lap — which
    is what happened in a live practice session."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import TyreState

    hot_inner = TyreState(inner_temp=98.0, middle_temp=88.0, outer_temp=84.0)
    track = TrackConfig(session_type="Race")

    stale = PromptBuilder(AppConfig()).format_telemetry(
        _car(tyre_fl=hot_inner, tyre_data_live=False), track)
    assert "Tyre profile" not in stale
    assert "camber" not in stale
    # The numbers themselves are still reported, with the staleness said.
    assert "last stop" in stale

    live = PromptBuilder(AppConfig()).format_telemetry(
        _car(tyre_fl=hot_inner, tyre_data_live=True), track)
    assert "Tyre profile front left" in live


# ── Practice and qualifying are not races ───────────────────────────

def test_a_lap_time_is_never_reported_as_a_gap_to_the_leader():
    """CarIdxF2Time is, in the SDK's own words, "Race time behind leader or
    fastest lap time otherwise". Outside a race it is a LAP TIME. Read as a
    gap it made every car in practice and qualifying a minute and a half
    behind the leader — a confident wrong number, on the timing screen the
    driver can compare it against."""
    import inspect
    from ai_race_engineer.telemetry.irsdk_reader import (
        iRacingTelemetryReader, _lap_deficit)
    src = inspect.getsource(iRacingTelemetryReader._standings)
    assert "_lap_deficit" in src, "F2Time is still read as a gap in every session"
    assert "session_kind" in src, "nothing distinguishes a race from a time session"

    # Off the session best is what "behind" means when nobody is chasing.
    assert _lap_deficit(91.6, 91.2) == 0.4
    # A car with no lap yet is not zero off the pace.
    assert _lap_deficit(None, 91.2) is None
    assert _lap_deficit(91.6, None) is None


def test_the_running_order_says_which_kind_of_gap_it_is_showing():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder

    race = PromptBuilder(AppConfig()).format_telemetry(
        _car(), TrackConfig(session_type="Race"))
    assert "seconds behind the leader" in race

    for kind in ("Qualify", "Practice"):
        block = PromptBuilder(AppConfig()).format_telemetry(
            _car(), TrackConfig(session_type=kind))
        assert "off the SESSION BEST LAP" in block, kind
        assert "seconds behind the leader" not in block, kind


def test_the_cars_either_side_are_traffic_when_there_is_no_race_on():
    """In qualifying the car up the road decides whether the next lap is
    clear. It is not a rival for position, and calling it one invites the
    driver to race somebody who is on their own out lap."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import Rival

    car = _car(ahead=Rival(name="Rossi", car_number="13", position=2, gap=1.4))
    quali = PromptBuilder(AppConfig()).format_telemetry(
        car, TrackConfig(session_type="Qualify"))
    assert "traffic, not a rival for position" in quali

    race = PromptBuilder(AppConfig()).format_telemetry(
        car, TrackConfig(session_type="Race"))
    assert "traffic" not in race


def test_a_qualifying_gap_is_spoken_as_pace_not_as_track_position():
    from ai_race_engineer.race_math import gap_to
    from ai_race_engineer.telemetry import Rival

    field = [Rival(name="Rossi", car_number="13", position=1, gap=0.0),
             Rival(name="You", car_number="1", position=2, gap=0.4,
                   is_player=True)]
    quali = gap_to(_car(), TrackConfig(session_type="Qualify", field=field),
                   "Rossi")
    assert quali["available"]
    assert "quicker" in quali["spoken"], quali["spoken"]
    assert "ahead" not in quali["spoken"], quali["spoken"]

    race = gap_to(_car(), TrackConfig(session_type="Race", field=field), "Rossi")
    assert "ahead" in race["spoken"], race["spoken"]


def _standings_for(session_type, positions, drivers, surfaces=None, best=None):
    """Drive the real _standings() with stubbed channels.

    Exercises the reader itself rather than a hand-built field, because a
    hand-built field cannot show which rows the reader decided to drop —
    which is the whole question here.
    """
    from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader as R

    reader = R.__new__(R)
    reader._sector_times = {}
    channels = {
        "CarIdxPosition": positions,
        "CarIdxTrackSurface": surfaces or [3] * len(positions),
        "CarIdxBestLapTime": best or [-1.0] * len(positions),
        "PlayerCarIdx": 0,
    }
    reader._get = lambda key, default=None: channels.get(key, default)
    reader._drivers = lambda: drivers
    reader._session_type = lambda: session_type
    reader._live_ranks = lambda: {}
    return reader._standings()


def test_somebody_with_no_lap_time_can_still_be_asked_about():
    """The field is built from CarIdxPosition, and in practice a driver who
    has not set a lap may not have one — just joined, or still in the
    garage. Dropped, they cannot be asked about at all: their iRating comes
    back as "I can't find that car" for somebody in the next pit box."""
    from ai_race_engineer.race_math import driver_report

    drivers = {
        0: {"name": "Marco Rossi", "car_number": "7", "irating": 3400},
        1: {"name": "Emil Grzelak", "car_number": "12", "irating": 1980},
    }
    # Grzelak is in the session and has no classified position.
    order, size, _ = _standings_for("Practice", [1, 0], drivers,
                                    best=[91.2, -1.0])
    assert [r.name for r in order] == ["Marco Rossi", "Emil Grzelak"]
    assert order[1].position is None
    assert order[1].irating == 1980

    out = driver_report(_car(), TrackConfig(session_type="Practice",
                                            field=order), "Grzelak")
    assert out["available"], out
    assert out["irating"] == 1980
    assert "no lap time" in out["spoken"], out["spoken"]


def test_an_unclassified_car_is_never_added_to_a_race_running_order():
    """In a race the classified set *is* the running order. Padding it with
    somebody the timing screen does not show would invent a place."""
    drivers = {
        0: {"name": "Marco Rossi", "car_number": "7", "irating": 3400},
        1: {"name": "Emil Grzelak", "car_number": "12", "irating": 1980},
    }
    order, size, _ = _standings_for("Race", [1, 0], drivers)
    assert [r.name for r in order] == ["Marco Rossi"]
    assert size == 1


def test_the_standings_tool_labels_its_gaps_by_session_too():
    from ai_race_engineer.race_math import standings
    from ai_race_engineer.telemetry import Rival
    field = [Rival(name="Marco Rossi", car_number="7", position=1, gap=0.0),
             Rival(name="You", car_number="1", position=2, gap=0.4,
                   is_player=True)]

    race = standings(_car(), TrackConfig(session_type="Race", field=field))
    assert "behind the leader" in race["spoken"]

    practice = standings(_car(), TrackConfig(session_type="Practice", field=field))
    assert "off the best lap of the session" in practice["spoken"]
    assert "behind the leader" not in practice["spoken"]


# ── Reasoning models: thinking is charged to the speech budget ───────

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _thinking_reply(content="", finish="length", reasoning="weighing it up"):
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return {"choices": [{"finish_reason": finish, "message": message}]}


def _local_client(responses):
    """An LLMClient whose transport replays `responses` in order."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient

    cfg = AppConfig({"llm": {"provider": "local",
                             "local": {"endpoint": "http://127.0.0.1:11434/v1",
                                       "model": "qwen3.6:latest"},
                             "use_tools": "off"}})
    client = LLMClient(cfg)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _FakeResponse(responses[min(len(sent) - 1, len(responses) - 1)])

    client._client.post = post
    return client, sent


def test_a_model_that_thinks_past_its_budget_is_given_room_and_asked_again():
    """The speech budget is 80 tokens because the engineer says one
    sentence. A reasoning model charges its thinking to the same budget,
    hits the ceiling mid-thought, and returns a finished thought and no
    words — measured on a local qwen3.6, which needed 1,119 tokens to
    reach an answer."""
    client, sent = _local_client([
        _thinking_reply(),                                    # thought past 80
        _thinking_reply(content="Fuel's fine.", finish="stop"),
    ])
    assert client.generate("How's my fuel?") == "Fuel's fine."
    assert len(sent) == 2, "no retry happened"
    assert sent[1]["max_tokens"] > sent[0]["max_tokens"], "retried with the same ceiling"

    # And the next question skips the wasted round trip entirely.
    before = len(sent)
    client.generate("And now?")
    assert len(sent) == before + 1
    assert sent[-1]["max_tokens"] == sent[1]["max_tokens"]


def test_the_retry_happens_once_and_cannot_loop():
    """A model that is simply silent must not be asked forever — the
    driver is waiting."""
    from ai_race_engineer.llm import NO_ANSWER
    client, sent = _local_client([_thinking_reply()])
    assert client.generate("How's my fuel?") == NO_ANSWER
    assert len(sent) == 2, f"asked {len(sent)} times"


def test_an_empty_reply_is_not_reported_as_a_comms_failure():
    """It used to say the same thing as an unreachable endpoint, which
    sent a whole debugging session at Ollama, the endpoint and the
    firewall while the model was answering in 1.7 seconds and simply
    never producing any words."""
    from ai_race_engineer.llm import COMMS_FAILURE, NO_ANSWER

    client, _ = _local_client([_thinking_reply(finish="stop", reasoning="")])
    answer = client.generate("How's my fuel?")
    assert answer == NO_ANSWER
    assert answer != COMMS_FAILURE


def test_tool_rejection_rebuilds_the_turn_with_deterministic_calculations():
    """A provider can accept ordinary chat but reject the tools parameter.

    The first prompt deliberately omits precomputed fuel strategy because the
    model is expected to call Python. Reusing that prompt for the plain-chat
    retry leaves the model to do the arithmetic itself.
    """
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient, LLMError
    from ai_race_engineer.telemetry import TelemetrySnapshot

    cfg = AppConfig({"llm": {"provider": "local",
                             "local": {"endpoint": "http://127.0.0.1:11434/v1",
                                       "model": "no-tools"},
                             "use_tools": "auto"}})
    client = LLMClient(cfg)
    plain_messages = []

    def chat_raw(messages, max_tokens=None, tools=None):
        if tools:
            raise LLMError("model does not support tools")
        plain_messages.extend(messages)
        return {"content": "Fuel is covered."}

    client._chat_raw = chat_raw
    telemetry = TelemetrySnapshot(
        _car(fuel_level=40.0, fuel_per_lap=3.2),
        TrackConfig(session_type="Race", laps_remaining=10))

    assert client.generate("How much fuel for the next twenty minutes?", telemetry) \
        == "Fuel is covered."
    user_turn = next(m["content"] for m in reversed(plain_messages)
                     if m["role"] == "user")
    assert "- Fuel plan:" in user_turn
    assert "8.0 litres spare" in user_turn
    assert client._tools_unsupported is True


def test_live_gap_wording_bypasses_model_paraphrase_entirely():
    """The configured model received "movement history" and said laps anyway."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    cfg = AppConfig({"llm": {"provider": "local",
                             "local": {"endpoint": "http://127.0.0.1:11434/v1",
                                       "model": "weak-model"},
                             "use_tools": "auto"}})
    client = LLMClient(cfg)
    calls = []

    def chat_raw(messages, max_tokens=None, tools=None):
        calls.append(messages)
        return {"content": "", "tool_calls": [{
            "id": "gap-1",
            "function": {"name": "adjacent_gap_trend",
                         "arguments": '{"direction":"ahead"}'},
        }]}

    client._chat_raw = chat_raw
    ahead = Rival(name="Rossi", car_number="13", gap=0.8,
                  gap_metres=40.0, position=2)
    telemetry = TelemetrySnapshot(
        _car(ahead=ahead, closing_ahead_status="waiting"),
        TrackConfig(session_type="Race"))
    expected = rm.adjacent_gap_trend(telemetry.car, telemetry.track,
                                     "ahead")["spoken"]

    assert client.generate("Am I gaining on the car ahead?", telemetry) == expected
    assert len(calls) == 0, "the deterministic intent still reached the model"


def test_common_fuel_question_bypasses_the_model_entirely():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    cfg = AppConfig({"llm": {"provider": "local",
                             "local": {"endpoint": "http://127.0.0.1:11434/v1",
                                       "model": "weak-model"},
                             "use_tools": "auto"}})
    client = LLMClient(cfg)
    calls = []

    def chat_raw(messages, max_tokens=None, tools=None):
        calls.append(messages)
        return {"content": "", "tool_calls": [{
            "id": "fuel-1",
            "function": {"name": "fuel_plan", "arguments": "{}"},
        }]}

    client._chat_raw = chat_raw
    car = _car(fuel_level=100.0, fuel_capacity=110.0, fuel_per_lap=3.5,
               lap_progress=0.2)
    track = TrackConfig(session_type="Race", current_lap=1,
                        laps_remaining=70)
    telemetry = TelemetrySnapshot(car, track)

    answer = client.generate("Do I have enough fuel?", telemetry)

    assert "stay out for now" in answer
    assert "pit now" not in answer.lower()
    assert answer == rm.fuel_plan(car, track)["spoken"]
    assert len(calls) == 0


def test_weather_question_gets_a_small_relevant_telemetry_view():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    from ai_race_engineer.telemetry import TelemetrySnapshot

    telemetry = TelemetrySnapshot(
        _car(track_surface_temp=31.0, air_temp=22.0,
             tire_fl_temp=80.0, fuel_level_pct=50.0),
        TrackConfig(session_type="Race", track_name="Spa", laps_remaining=10,
                    field=_field(), field_size=5),
    )
    turn = PromptBuilder(AppConfig()).build_user_turn(
        "What are the track conditions?", telemetry, tools_enabled=True)

    assert "- Track conditions:" in turn
    assert "- Session:" in turn
    assert "- Fuel:" not in turn
    assert "- Running order" not in turn
    assert "- Tyre carcass" not in turn


def test_series_resource_question_bypasses_the_model():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    client = LLMClient(AppConfig())
    client._chat_raw = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("published pit state was left to the model"))
    telemetry = TelemetrySnapshot(
        _car(pits_open=False), TrackConfig(session_type="Race"))

    assert client.generate("Are the pits open?", telemetry) == "The pits are closed."


def test_race_control_question_bypasses_the_model():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    client = LLMClient(AppConfig())
    client._chat_raw = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("race-control status was left to the model"))
    telemetry = TelemetrySnapshot(
        _car(flag="Black", penalty="slow-down penalty"),
        TrackConfig(session_type="Race"),
    )

    assert client.generate("Do I have a penalty?", telemetry) == "Slow-down penalty."


def test_position_intent_uses_the_adjacent_gap_without_asking_the_model():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    client = LLMClient(AppConfig())
    client._chat_raw = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("position answer was left to the model"))
    car = _car(
        position=7,
        ahead=Rival(name="Sean Ambrose", gap=1.1, position=6),
        behind=Rival(name="Mike Gladfelter", gap=0.3, position=8))
    telemetry = TelemetrySnapshot(car, TrackConfig(session_type="Race"))

    answer = client.generate("Position status", telemetry)

    assert answer == rm.position_report(car, telemetry.track)["spoken"]
    assert "Sean Ambrose" in answer and "leader" not in answer.lower()


def test_adjacent_movement_intent_uses_the_short_calculator_answer_directly():
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.llm import LLMClient
    from ai_race_engineer.telemetry import TelemetrySnapshot

    client = LLMClient(AppConfig())
    client._chat_raw = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("adjacent movement answer was left to the model"))
    car = _car(
        ahead=Rival(name="Sean Ambrose", gap=0.5, gap_metres=28.0, position=6),
        closing_ahead=-1.4, closing_ahead_status="confirmed",
        closing_ahead_seconds=14.0)
    telemetry = TelemetrySnapshot(car, TrackConfig(session_type="Race"))

    answer = client.generate("Am I catching the car in front?", telemetry)

    assert answer == rm.adjacent_gap_trend(car, telemetry.track, "ahead")["spoken"]
    assert answer.count("Sean Ambrose") == 1
    assert "Measured over" not in answer


def test_the_connection_test_fails_when_the_model_says_nothing():
    """Reporting OK here is worse than failing: the round trip works, so
    the panel showed a green tick while every real question came back
    empty."""
    from ai_race_engineer.llm import LLMError

    client, _ = _local_client([_thinking_reply(finish="stop", reasoning="")])
    try:
        client.test_connection()
    except LLMError as exc:
        assert "replied with nothing" in str(exc)
    else:
        raise AssertionError("a silent model passed the connection test")


def test_only_a_thought_that_ran_out_of_room_counts():
    """Three things together, because each alone means something else."""
    from ai_race_engineer.llm import _thought_past_the_budget

    assert _thought_past_the_budget(_thinking_reply()["choices"][0])
    # Stopped normally — it had its say.
    assert not _thought_past_the_budget(
        _thinking_reply(finish="stop")["choices"][0])
    # Truncated mid-sentence, but it did speak.
    assert not _thought_past_the_budget(
        _thinking_reply(content="You're two litres")["choices"][0])
    # Truncated emitting a tool call, which the tool budget already covers.
    call = _thinking_reply()["choices"][0]
    call["message"]["tool_calls"] = [{"function": {"name": "fuel_plan"}}]
    assert not _thought_past_the_budget(call)
    # Empty with no reasoning is a different fault, not a budget problem.
    assert not _thought_past_the_budget(
        _thinking_reply(reasoning="")["choices"][0])


# ── Damage is a cost to weigh, not an order to pit ──────────────────

def _damaged(track, **kw):
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    car = _car(repair_required=18.0, last_lap_time=95.0, **kw)
    block = PromptBuilder(AppConfig()).format_telemetry(car, track)
    return next(l for l in block.splitlines() if l.startswith("- Damage:"))


def test_damage_is_not_stated_as_an_order_to_stop():
    """PitRepairLeft is the repair time iRacing forces on you once you
    stop, not an instruction to stop. Called "mandatory repairs
    outstanding" it read as one, and the engineer sent a driver in with
    two laps left and enough fuel to finish."""
    line = _damaged(TrackConfig(session_type="Race", laps_remaining=2))
    assert "mandatory" not in line
    assert "Damage on its own is NOT a reason to pit" in line
    # The other side of the trade, so the call is not made on repair time
    # alone: two laps at 95s is what the stop has to be won back inside.
    assert "2 laps" in line and "3m 10s" in line


def test_a_meatball_flag_is_passed_on_as_the_order_it_is():
    """The one case that is not a judgement call. Race control has
    already decided, and hedging it costs the driver a black flag."""
    line = _damaged(TrackConfig(session_type="Race", laps_remaining=2),
                    penalty="meatball — must pit for repairs")
    assert "ORDERED you in" in line
    assert "NOT a reason to pit" not in line


def test_there_is_nothing_to_weigh_outside_a_race():
    """Practice has no finish to reach and no position to give up, so the
    trade-off that keeps a driver out on the last lap of a race is just
    wrong advice here."""
    line = _damaged(TrackConfig(session_type="Practice"))
    assert "NOT a reason to pit" not in line
    assert "costs only the time it takes" in line


def test_strategy_is_framed_as_a_recommendation_whatever_the_persona_says():
    """The rule has to survive the system prompt being rewritten in the
    UI — which is where the installed one has been sitting, several
    versions behind the default, for the whole life of this bug."""
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder

    config = AppConfig({"system_prompt": "You are a race engineer."})
    system = PromptBuilder(config).build_messages("Should I pit?")[0]["content"]
    assert "recommendation, never an instruction" in system
    assert "Race control is the exception" in system


# ── The incident limit is per-session, not something you know ───────

def _incident_line(**kw):
    from ai_race_engineer.config import AppConfig
    from ai_race_engineer.prompt_builder import PromptBuilder
    block = PromptBuilder(AppConfig()).format_telemetry(
        _car(**kw), TrackConfig(session_type="Race"))
    return next(l for l in block.splitlines() if l.startswith("- Incident points:"))


def test_no_published_limit_means_no_limit_may_be_named():
    """iRacing writes "unlimited" in plenty of sessions and the reader
    gives None. Stated as a bare count, the model supplied a cap from
    somewhere else and a driver kept hearing "one more and you're out"
    against a number nobody had published."""
    line = _incident_line(incidents=8)
    assert "8" in line
    assert "never say how many they have left" in line
    assert "keep it clean" in line


def test_the_margin_is_subtracted_here_when_there_is_a_limit():
    """Two numbers side by side is an invitation to do arithmetic, and
    the model does it wrong."""
    line = _incident_line(incidents=12, incident_limit=17)
    assert "5 points in hand" in line
    assert "never work it out yourself" in line

    assert "1 point in hand" in _incident_line(incidents=16, incident_limit=17)
    assert "at or past it" in _incident_line(incidents=25, incident_limit=25)


def test_the_team_total_is_what_runs_against_the_limit():
    line = _incident_line(incidents=4, team_incidents=19, incident_limit=25)
    assert "6 points in hand" in line          # off the team's 19, not the driver's 4
    assert "TEAM total is what runs against the limit" in line


# ── Guards on the inputs themselves ────────────────────────────────────

def test_a_corrupt_fuel_capacity_cannot_spin_the_stop_count():
    """Stops iterate as laps / (capacity / per_lap).

    A capacity that came through as a fraction of a litre makes each stint
    cover almost nothing, and the loop counting stops runs away on the thread
    the driver is waiting on. Past a sane number of stops the inputs are
    wrong, not the strategy, so it gives the same "I can't call the window"
    answer as no capacity at all.
    """
    car = _car(fuel_level=0.5, fuel_capacity=0.01, fuel_per_lap=3.0,
               lap_progress=0.1)
    track = TrackConfig(session_type="Race", current_lap=1, laps_remaining=50)

    result = rm.pit_window(car, track)

    assert result["stops_required"] is None
    assert result["earliest_lap"] is None
    # Still true, and still worth saying: the tank in the car runs out now.
    assert result["pit_now"] is True


def test_a_real_multi_stop_race_still_counts_its_stops():
    """The guard must not swallow an ordinary two-stopper."""
    car = _car(fuel_level=100.0, fuel_capacity=110.0, fuel_per_lap=3.5,
               lap_progress=0.0)
    track = TrackConfig(session_type="Race", current_lap=1, laps_remaining=80)

    result = rm.pit_window(car, track)

    assert result["stops_required"] == 2


def test_the_annotations_in_this_module_still_resolve():
    """`Tuple` was used in an annotation and never imported.

    Invisible at runtime because of `from __future__ import annotations`,
    and a `NameError` the moment anything introspects — a tool schema
    generator, pydantic, a test like this one.
    """
    import inspect
    import typing

    checked = 0
    for value in vars(rm).values():
        if inspect.isfunction(value) and value.__module__ == rm.__name__:
            typing.get_type_hints(value)      # raises NameError on a missing import
            checked += 1
    assert checked > 20, "nothing was actually introspected"


# ── What runs out first ─────────────────────────────────────────────


def _quali(**kw):
    """Lone Qualify: a two-lap allowance inside an eight-minute window.

    The numbers are a captured session — `SessionType: Lone Qualify,
    SessionLaps: 2, SessionTime: 480.0000 sec`.
    """
    base = dict(session_type="Lone Qualify", laps_total=2, laps_remaining=1,
                time_remaining=300.0, time_total=480.0,
                solo_qualifying=True, qualify_scoring="best lap")
    base.update(kw)
    return TrackConfig(**base)


def test_the_qualifying_lap_allowance_is_the_budget_not_the_clock():
    """This is the bug. The engineer knew only that five minutes were left,
    so it told a driver on their last flier that they had time to keep
    trying. They had one lap. The clock is the window the allowance has to
    be spent in, which is a different thing and is now decided here rather
    than by whichever line the model read first."""
    result = rm.session_status(_car(), _quali())
    assert result["available"]
    assert result["binds"] == "laps"
    spoken = result["spoken"].lower()
    assert "1 lap left" in spoken
    assert "not your budget" in spoken


def test_the_clock_can_still_stop_a_driver_who_has_laps_in_hand():
    """The fix must not overshoot into the opposite error: with twenty
    seconds left, a lap in hand is not a lap you get to run."""
    result = rm.session_status(_car(), _quali(time_remaining=20.0))
    assert result["binds"] == "clock"
    assert "no lap limit" not in result["spoken"].lower()
    assert "clock stops you" in result["spoken"].lower()


def test_a_qualifying_session_with_no_allowance_is_limited_by_the_clock():
    """`SessionLaps: unlimited` in a Lone Qualify session is also real — a
    captured session pairs it with a twelve-minute clock. There the clock
    genuinely is the budget."""
    result = rm.session_status(
        _car(), _quali(laps_total=None, laps_remaining=None, time_remaining=720.0))
    assert result["binds"] == "clock"
    assert "no lap limit" in result["spoken"].lower()


def test_an_averaged_qualifying_result_is_said_so():
    """A driver whose result is an average of four laps cannot treat one
    scruffy lap the way a best-lap driver can."""
    result = rm.session_status(_car(), _quali(laps_to_average=4))
    assert "average over 4 laps" in result["spoken"]


def test_neither_limit_published_is_a_refusal_not_a_guess():
    track = TrackConfig(session_type="Lone Qualify")
    assert rm.session_status(_car(), track)["available"] is False


def test_the_session_limit_is_not_guessed_without_a_lap_time():
    """Converting a clock into laps needs a measured lap time. Without one
    the honest answer is that either limit could arrive first."""
    track = _quali()
    assert rm.session_limit(_car(best_lap_time=None), track)["binds"] == "both"


def test_a_finished_qualifying_session_is_not_a_race_that_was_finished():
    """The driver still has the race to come. "You finished P2" told them
    the day was over."""
    car = _car(position=2, class_position=2)
    track = TrackConfig(session_type="Lone Qualify", finished=True)
    assert rm.position_report(car, track)["spoken"] == "You qualified P2."
