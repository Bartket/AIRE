"""Deterministic race calculations over a telemetry snapshot.

Every function here answers one question the driver actually asks, using
arithmetic done in Python rather than in the model's head. That is the whole
point: the system prompt forbids inventing data, but a language model doing
fuel maths or converting 112.188 into 1:52.188 is still guessing, just more
convincingly. Moving the sums here makes the rule structural.

Each function returns a dict that always carries:

  available : bool  — False when the sim did not supply what the sum needs
  reason    : str   — why, in words the engineer can say out loud
  spoken    : str   — a short phrase safe to read almost verbatim on radio

plus the structured numbers behind it, so the model can rephrase rather than
recompute. Nothing here raises: an unanswerable question comes back as
``available: False`` with a reason, never as a wrong number.

These are useful with or without tool calling — see `summary_lines()`, which
folds the results straight into the prompt block.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from . import pronounce
from .telemetry import (CarTelemetry, Rival, TelemetrySnapshot, TrackConfig,
                        session_kind)

logger = logging.getLogger("race-engineer")


# ── Result helpers ──────────────────────────────────────────────────

def _no(reason: str, **extra: Any) -> Dict[str, Any]:
    """An honest 'I don't have that', shaped like every other result."""
    return {"available": False, "reason": reason, "spoken": reason, **extra}


def _yes(spoken: str, **fields: Any) -> Dict[str, Any]:
    return {"available": True, "reason": "", "spoken": spoken, **fields}


def _laptime(value: Optional[float]) -> str:
    """Seconds -> m:ss.mmm. Duplicated from prompt_builder deliberately:
    this module must be usable without importing the prompt layer."""
    if value is None:
        return "unknown"
    if value < 60:
        return f"{value:.3f}s"
    minutes, seconds = divmod(float(value), 60)
    return f"{int(minutes)}:{seconds:06.3f}"


def _spoken_delta(value: float) -> str:
    """A time difference written the way an engineer says it.

    Sub-second values are given in tenths or hundredths, never as a decimal
    and never as a fraction. Told "0.50 seconds" — and then "half a second" —
    the model read both aloud as "half a tenth", a tenfold error, while it
    handled "0.85 of a second" correctly as "eight tenths". Naming the unit
    the number is already counted in leaves no conversion to get wrong.
    """
    value = abs(float(value))
    if value < 0.005:
        return "nothing"
    if value >= 1:
        return f"{value:.2f} seconds"
    hundredths = round(value * 100)
    if hundredths % 10 == 0:
        return f"{hundredths // 10} tenths of a second"
    return f"{hundredths} hundredths of a second"


def _plural(count: float, word: str) -> str:
    return f"{count:.0f} {word}" + ("" if abs(count - 1) < 0.5 else "s")


def _laps_to_go(car: CarTelemetry, track: TrackConfig) -> Optional[float]:
    """How many laps are actually left to run.

    Lap-limited sessions say so directly. Timed sessions do not, so the
    clock is divided by a real lap time — never by a guess.
    """
    if car.laps_to_go is not None:
        return float(car.laps_to_go)
    try:
        progress = min(1.0, max(0.0, float(car.lap_progress)))
    except (TypeError, ValueError):
        progress = 0.0
    if track.laps_remaining is not None:
        return max(0.0, float(track.laps_remaining) - progress)
    if track.time_remaining is not None:
        lap = car.avg_lap_time or car.last_lap_time or car.best_lap_time
        if lap and lap > 0:
            # The clock can expire between crossings. Finish is the first
            # start/finish crossing after that, so account for how far the
            # current lap has already been driven.
            return max(0.0, math.ceil(track.time_remaining / lap + progress)
                       - progress)
    return None


def _spoken_clock(seconds: float) -> str:
    """Session time as words. The model must never be handed 480.0 to read."""
    whole = int(max(0.0, seconds))
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{_plural(hours, 'hour')} {_plural(minutes, 'minute')}"
    if minutes:
        return (_plural(minutes, "minute") if secs < 10
                else f"{_plural(minutes, 'minute')} {_plural(secs, 'second')}")
    return _plural(secs, "second")


def session_limit(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """What actually ends the driver's running, and how much of it is left.

    One classifier, in one place, because a session can be bounded by a lap
    count, by the clock, or by whichever arrives first — and leaving the
    model to choose between the two lines of the telemetry block is how it
    came to offer a driver eight minutes of qualifying when what they had
    was two laps.

    "binds" is never guessed. Where only one limit is published, that one
    binds. Where both are and there is no measured lap time to convert
    between them, the honest answer is that either could, not a coin toss
    dressed up as a decision.
    """
    kind = session_kind(track.session_type)
    laps_left = track.laps_remaining
    time_left = track.time_remaining
    lap = car.avg_lap_time or car.last_lap_time or car.best_lap_time
    clock_laps = (time_left / lap) if (time_left is not None and lap and lap > 0) else None

    if laps_left is None and time_left is None:
        binds = None
    elif laps_left is None:
        binds = "clock"
    elif time_left is None:
        binds = "laps"
    elif clock_laps is None:
        binds = "both"
    elif clock_laps + 0.5 < laps_left:
        binds = "clock"
    elif laps_left + 0.5 < clock_laps:
        binds = "laps"
    else:
        binds = "both"

    return {
        "kind": kind,
        "laps_left": laps_left,
        "laps_allowed": track.laps_total,
        "time_left": time_left,
        "clock_laps": None if clock_laps is None else round(clock_laps, 1),
        "binds": binds,
    }


def _scoring_phrase(track: TrackConfig) -> str:
    """How the qualifying result is decided, where iRacing published it.

    Series differ — a single best lap is the common case, and an averaged
    format changes what a driver should do with a scruffy lap entirely.
    Both come from the session YAML, so neither is assumed.
    """
    average = track.laps_to_average
    if average and average > 1:
        return f"scored on your average over {_plural(average, 'lap')}"
    scoring = (track.qualify_scoring or "").strip().lower()
    return "scored on your best lap" if "best" in scoring else ""


def session_status(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """How much running is left, and what it is that runs out.

    Exists because "how long have I got" has a different answer in each
    session, and in qualifying the session clock is usually the wrong one:
    a Lone Qualify session publishes a lap allowance inside a window, so
    the minutes on the clock are when the driver may run, not how long
    they may keep trying.
    """
    limit = session_limit(car, track)
    laps, time_left, binds = limit["laps_left"], limit["time_left"], limit["binds"]
    if binds is None:
        return _no("I haven't got a time or a lap count for this session.")

    clock = None if time_left is None else _spoken_clock(time_left)
    lap_words = None if laps is None else _plural(laps, "lap")

    if limit["kind"] == "qualify":
        scoring = _scoring_phrase(track)
        solo = track.solo_qualifying
        allowance = limit["laps_allowed"]
        if binds == "laps":
            spoken = f"{lap_words} left to run"
            if allowance:
                spoken += f" of your {_plural(allowance, 'lap')}"
            if clock:
                spoken += f", and the {clock} on the clock is your window, not your budget"
        elif binds == "clock" and laps is None:
            # No allowance published, so the clock genuinely is the budget.
            spoken = f"{clock} left, and no lap limit on this one"
            if solo is False:
                spoken += ", so you can go again"
        elif binds == "clock":
            # There IS an allowance; the clock simply expires first. Saying
            # "no lap limit" here told a driver with one lap in hand that
            # they had none, which is the same error the other way round.
            spoken = f"{clock} left, so the clock stops you before the laps do"
        else:
            spoken = f"{lap_words} left and {clock} on the clock, whichever goes first"
        if scoring:
            spoken += f" — {scoring}"
        return _yes(
            spoken + ".", **limit,
            solo_qualifying=solo, scoring=scoring or None,
            basis="published session limits; in qualifying the lap allowance "
                  "and the clock are different things",
        )

    if binds == "laps":
        spoken = f"{lap_words} to go"
    elif binds == "clock":
        spoken = f"{clock} left"
    else:
        spoken = f"{lap_words} left and {clock} on the clock, whichever goes first"
    return _yes(spoken + ".", **limit,
                basis="published session limits")


def _plain(text: str) -> str:
    """A name reduced to what two spellings of it have in common.

    Accents come off because the roster has them and speech recognition
    does not: the field holds "Kovács" and the driver is transcribed saying
    "Kovacs", and a substring test between those fails. Punctuation goes
    too, so "O'Brien" and "OBrien" are the same lookup.
    """
    return re.sub(r"[^a-z0-9 ]", "", pronounce.fold(str(text or "")).lower()).strip()


def _fuel_burn_bounds(car: CarTelemetry) -> Tuple[Optional[float], Optional[float],
                                                   Optional[float], int, str]:
    """Typical, low and high measured clean-lap burn in spoken units."""
    typical = car.fuel_per_lap
    if typical is None or typical <= 0:
        return None, None, None, 0, "unavailable"
    samples = max(0, int(getattr(car, "fuel_burn_samples", 0) or 0))
    low = getattr(car, "fuel_burn_min", None)
    high = getattr(car, "fuel_burn_max", None)
    if low is None or low <= 0 or high is None or high <= 0 or low > high:
        return typical, typical, typical, samples, "single estimate"
    confidence = "measured" if samples >= 4 else "early"
    return typical, low, high, samples, confidence


def _find(field: List[Rival], query: str) -> Optional[Rival]:
    """Locate a car by position ("P3", "3"), car number, or driver name."""
    if not field or not query:
        return None
    q = str(query).strip().lower()

    if q in ("leader", "the leader", "p1", "first"):
        return next((r for r in field if r.position == 1), None)

    # "the 44" is what a driver actually says; without this it falls through
    # the number branches entirely and is looked up as a name.
    if q.startswith("the "):
        q = q[4:].strip()

    # "P3" / "3" — a bare number is a position, "#44" or "car 44" a number.
    number_only = q.lstrip("#").replace("car ", "").strip()
    if q.startswith("p") and q[1:].strip().isdigit():
        pos = int(q[1:].strip())
        return next((r for r in field if r.position == pos), None)
    if q.startswith("#") or q.startswith("car "):
        return next((r for r in field if r.car_number == number_only), None)
    if number_only.isdigit():
        by_number = next((r for r in field if r.car_number == number_only), None)
        if by_number:
            return by_number
        pos = int(number_only)
        return next((r for r in field if r.position == pos), None)

    # Name match: exact, then surname/substring, both with the accents
    # folded off. "Kovacs" spoken against "Kovács" on the timing screen
    # failed every one of these tests before folding.
    target = _plain(q)
    if not target:
        return None
    names = [(r, _plain(r.name), _plain(r.short_name)) for r in field]
    for r, name, short in names:
        if target in (name, short):
            return r
    for r, name, short in names:
        if target and (target in name or target in short):
            return r
    # A surname on its own, which is what gets said on the radio and what
    # AbbrevName ("Surname, F.") leads with.
    for r, name, short in names:
        if any(target == word for word in name.split() + short.split()):
            return r

    # Nothing matched exactly, so allow for the recognition being close
    # rather than right — "Kovach" for "Kovacs". Cheap: a field is at most
    # 64 names, and this only runs once the exact tests have all failed.
    words = {}
    for r, name, short in names:
        for word in set(name.split() + short.split()):
            if len(word) > 3:
                words.setdefault(word, r)
    close = difflib.get_close_matches(target, list(words), n=1, cutoff=0.8)
    if close:
        return words[close[0]]
    return None


# The most of a lap's fuel a driver can realistically lift and coast back.
# Past this, "save X per lap" is arithmetic rather than advice: asked to save
# 3.1 litres of a 3.5 litre lap, the driver is being told to switch the engine
# off. The honest call there is a pit stop.
_MAX_SAVE_FRACTION = 0.15

# Most stops a fuel window is willing to plan for. Past this the inputs are
# wrong, not the strategy; see `_fuel_stop_window`.
_MAX_FUEL_STOPS = 20


def save_per_lap(margin: float, laps: float, per_lap: float) -> Optional[float]:
    """Fuel to save each lap to reach the flag, or None if it cannot be saved.

    None is not "unknown" — it means no saving target exists, because the
    shortfall is beyond lifting and coasting. Nothing may be spoken as a
    saving figure then, and none may be worked out from the numbers beside
    it; the answer is a stop. The one classifier for that, so the prompt
    block and the calculator cannot disagree about whether saving is on.
    """
    if margin >= 0 or laps <= 0 or per_lap <= 0:
        return None
    save = abs(margin) / laps
    return save if save <= per_lap * _MAX_SAVE_FRACTION else None


def _me(track: TrackConfig) -> Optional[Rival]:
    return next((r for r in track.field if r.is_player), None)


def _class_context(track: TrackConfig, them: Rival) -> Dict[str, Any]:
    """Whether another car is a position rival or traffic from another lap/class."""
    me = _me(track)
    my_class = track.car_class_id
    if my_class is None and me is not None:
        my_class = me.car_class_id
    same_class = (None if my_class is None or them.car_class_id is None
                  else my_class == them.car_class_id)

    class_speed = None
    if same_class is False:
        my_estimate = track.car_class_est_lap_time
        if my_estimate is None and me is not None:
            my_estimate = me.class_est_lap_time
        their_estimate = them.class_est_lap_time
        if my_estimate and their_estimate:
            if their_estimate < my_estimate:
                class_speed = "faster"
            elif their_estimate > my_estimate:
                class_speed = "slower"

    position_rival = (same_class is True and them.laps_down == 0)
    if same_class is False:
        traffic = (f"{class_speed}-class traffic" if class_speed
                   else "different-class traffic")
    elif same_class is True and them.laps_down > 0:
        traffic = "a lapped same-class car"
    elif same_class is True and them.laps_down < 0:
        traffic = "a same-class car lapping you"
    else:
        traffic = ""
    return {
        "same_class_as_you": same_class,
        "class_speed": class_speed,
        "position_rival": position_rival if same_class is not None else None,
        "traffic_description": traffic,
    }


def _is_race(track: TrackConfig) -> bool:
    """Whether this session has a finish to reach.

    Practice and qualifying end on a clock, not a distance, so "will the
    fuel last to the end" has no meaning — dividing the session time by a
    lap time gives laps you *could* run, not laps you must complete.
    """
    return session_kind(getattr(track, "session_type", "")) == "race"


def _race_not_started(track: TrackConfig) -> bool:
    state = str(getattr(track, "session_state", "") or "").lower()
    return _is_race(track) and state in ("getincar", "warmup", "paradelaps")


def _starting_position_spoken(track: TrackConfig, position: int,
                              class_position: Optional[int]) -> str:
    spoken = f"You start P{position}"
    if class_position and class_position != position:
        spoken += f", P{class_position} in class"
    if str(getattr(track, "session_state", "") or "").lower() == "paradelaps":
        return spoken + "; we're on the formation lap."
    return spoken + "; the race hasn't started."


def _fuel_stop_window(car: CarTelemetry, track: TrackConfig, laps: float,
                      per_lap: float, have: float) -> Dict[str, Any]:
    """The next usable fuel window, including races needing several stops.

    A total-race shortfall proves that a stop is required; it does not prove
    the car should stop now. The first stop can happen only when the fuel left
    after it is reachable with the remaining full-tank stints, and no later
    than the last lap the current tank reaches.
    """
    try:
        progress = min(1.0, max(0.0, float(car.lap_progress)))
    except (TypeError, ValueError):
        progress = 0.0
    lap_now = track.current_lap or 0
    laps_of_fuel = have / per_lap
    laps_on_current_fuel = math.floor(laps_of_fuel + progress)
    latest = lap_now + max(0, math.floor(laps_of_fuel + progress - 1.0))

    def window_unknown() -> Dict[str, Any]:
        """What is still true when the stop count cannot be worked out."""
        return {
            "stops_required": None,
            "earliest_lap": None,
            "latest_lap": latest,
            "laps_on_current_fuel": laps_on_current_fuel,
            "laps_of_fuel": laps_of_fuel,
            "tank_covers_remaining": None,
            "window_open": None,
            "pit_now": latest <= lap_now,
        }

    capacity = car.fuel_capacity
    if not capacity or capacity <= 0:
        return window_unknown()

    laps_on_full = capacity / per_lap

    def first_stop_for(stops: int) -> int:
        return lap_now + max(
            0, math.ceil(laps - stops * laps_on_full + progress - 1.0))

    stops = 1
    earliest = first_stop_for(stops)
    # Iterations scale as laps / (capacity / per_lap), so a capacity that
    # came through wrong — a fraction of a litre — makes each stint cover
    # almost nothing and the count runs away. No race is won on twenty-one
    # stops: past that the number is wrong rather than large, so it falls
    # back to the same "I can't call the window" answer as no capacity at
    # all, instead of spinning on the thread the driver is waiting on.
    while earliest > latest and stops < _MAX_FUEL_STOPS:
        stops += 1
        earliest = first_stop_for(stops)
    if earliest > latest:
        logger.warning(
            "Fuel window needs more than %d stops (capacity %s, burn %s per lap)"
            " — not calling it", _MAX_FUEL_STOPS, capacity, per_lap)
        return window_unknown()

    return {
        "stops_required": stops,
        "earliest_lap": earliest,
        "latest_lap": latest,
        "laps_on_current_fuel": laps_on_current_fuel,
        "laps_of_fuel": laps_of_fuel,
        "tank_covers_remaining": laps_on_full >= laps,
        "window_open": earliest <= lap_now,
        "pit_now": latest <= lap_now,
    }


# ── The calculations ────────────────────────────────────────────────

def fuel_plan(car: CarTelemetry, track: TrackConfig,
              target_laps: Optional[float] = None,
              target_minutes: Optional[float] = None) -> Dict[str, Any]:
    """Can we finish on the fuel in the tank, and if not, by how much?

    `target_minutes` answers "how much fuel for the next twenty minutes",
    which is how a driver thinks in a timed race. It is converted to laps
    here rather than in the prompt, because the model must never do the
    division — and the answer says it is an estimate, since a lap time that
    drifts moves the lap count with it.
    """
    unit = getattr(car, "fuel_unit", "litres")
    have = car.fuel_level
    if have is None:
        return _no("I've got no fuel reading.")
    typical_burn, burn_low, burn_high, burn_samples, fuel_confidence = \
        _fuel_burn_bounds(car)
    if typical_burn is None or burn_low is None or burn_high is None:
        return _yes(
            f"You've got {have:.1f} {unit} on board. I haven't measured a racing-lap "
            f"burn yet, so I can't call the range or strategy.",
            fuel_in_tank=round(have, 2), unit=unit,
            strategy_available=False, burn_per_lap=None,
            laps_of_fuel=None, stop_required=None, stops_required=None,
            pit_now=False,
        )

    # Outside a race there is no finish to reach, so report range instead of
    # inventing a target and answering "you will make it".
    if target_laps is None and not _is_race(track):
        laps_of_fuel = have / typical_burn
        return _yes(
            f"You've got {have:.1f} {unit} on board, about {laps_of_fuel:.1f} laps "
            f"at {typical_burn:.2f} {unit} a lap.",
            fuel_in_tank=round(have, 2), unit=unit,
            burn_per_lap=round(typical_burn, 3),
            burn_per_lap_low=round(burn_low, 3),
            burn_per_lap_high=round(burn_high, 3),
            fuel_burn_samples=burn_samples,
            fuel_confidence=fuel_confidence,
            laps_of_fuel=round(laps_of_fuel, 1),
            burn_last_lap=car.fuel_last_lap,
            burn_last_lap_clean=car.fuel_last_lap_clean,
            burn_this_stint=car.fuel_used_stint,
            session="not a race — reporting range, not whether it reaches a finish",
        )

    estimated_from_clock = False
    if target_laps is not None:
        laps = float(target_laps)
    elif target_minutes is not None:
        lap_time = car.avg_lap_time or car.last_lap_time or car.best_lap_time
        if not lap_time or lap_time <= 0:
            return _no("I need a lap time before I can turn minutes into laps.")
        # Partial laps still have to be driven, so round up.
        laps = math.ceil(float(target_minutes) * 60.0 / lap_time)
        estimated_from_clock = True
    else:
        laps = _laps_to_go(car, track)
    if laps is None:
        return _no("I don't know how many laps are left, so I can't call the fuel.")

    needed_likely = laps * typical_burn
    margin_likely = have - needed_likely
    needed_best = laps * burn_low
    needed_conservative = laps * burn_high
    margin_best = have - needed_best
    margin_conservative = have - needed_conservative
    timed_estimate = bool(
        estimated_from_clock
        or getattr(car, "laps_to_go_is_estimate", False)
        or (target_laps is None and track.laps_remaining is None
            and track.time_remaining is not None)
    )
    extra_lap_margin = have - ((laps + 1.0) * burn_high) if timed_estimate else None

    # Strategy uses the highest burn actually measured on a clean racing lap.
    # The mean remains useful as the likely case, but it cannot be the number
    # that declares a tank safe.
    per_lap = burn_high
    needed = needed_conservative
    margin = margin_conservative
    laps_of_fuel = have / per_lap
    stop_window = (_fuel_stop_window(car, track, laps, per_lap, have)
                   if _is_race(track) else {})

    range_spans_finish = margin_best >= 0 > margin_conservative
    extra_lap_spans_finish = (extra_lap_margin is not None
                              and margin_conservative >= 0 > extra_lap_margin)
    if range_spans_finish or extra_lap_spans_finish:
        if range_spans_finish:
            spoken = (
                f"Fuel is marginal — measured clean laps used {burn_low:.1f} to "
                f"{burn_high:.1f} {unit}, putting the finish between "
                f"{margin_best:.1f} {unit} spare and "
                f"{abs(margin_conservative):.1f} {unit} short."
            )
        else:
            spoken = (
                f"Fuel covers the likely finish by {margin_conservative:.1f} {unit}, "
                f"but an extra timed lap leaves us {abs(extra_lap_margin):.1f} "
                f"{unit} short — it's marginal until the lap count settles."
            )
        return _yes(
            spoken,
            fuel_in_tank=round(have, 2), unit=unit,
            burn_per_lap=round(typical_burn, 3),
            burn_per_lap_low=round(burn_low, 3),
            burn_per_lap_high=round(burn_high, 3),
            fuel_burn_samples=burn_samples,
            fuel_confidence=fuel_confidence,
            laps_to_go=round(laps, 1),
            fuel_needed=round(needed_likely, 2),
            fuel_needed_conservative=round(needed_conservative, 2),
            margin=round(margin_likely, 2),
            fuel_margin_best=round(margin_best, 2),
            fuel_margin_conservative=round(margin_conservative, 2),
            extra_lap_margin=(None if extra_lap_margin is None
                              else round(extra_lap_margin, 2)),
            laps_of_fuel=round(laps_of_fuel, 1),
            strategy_available=True, stop_required=None, stops_required=None,
            pit_now=False, window_open=False, next_window_open=None,
            next_window_close=None, will_finish=None,
            save_per_lap_required=None,
            tank_capacity=(None if not car.fuel_capacity
                           else round(car.fuel_capacity, 2)),
        )

    # One stop is only enough if what is on board plus a full tank covers the
    # requirement — the best case being to run this tank out before filling.
    # Same criterion as pit_window's earliest > latest arm, so the two cannot
    # disagree about whether the race is a one-stopper.
    capacity = car.fuel_capacity
    one_stop_covers_it = None
    if capacity and capacity > 0:
        one_stop_covers_it = have + capacity >= needed

    if margin >= 0:
        spare_laps = margin / per_lap
        spoken = (f"Fuel is good — {have:.1f} {unit} for {laps:.0f} laps, "
                  f"{margin:.1f} {unit} spare, about {spare_laps:.1f} laps in hand.")
        save = None
        save_basis = None
        stop_required = False
    else:
        save = save_per_lap(margin, laps, per_lap)
        short_laps = abs(margin) / per_lap
        if one_stop_covers_it is False:
            # Saying "box for fuel" here sends the driver in for a stop that
            # cannot fix it. The shortfall is bigger than the tank, so the
            # answer is how many stops, not how much fuel. Dropping the saving
            # target with it: a figure that reaches the flag on one stop is
            # not one the driver can lift and coast to when two are needed,
            # and the words and the numbers must not disagree.
            save = None
            spoken = (f"We're short by {abs(margin):.1f} {unit}, about "
                      f"{short_laps:.1f} laps.")
            save_basis = ("no saving target exists — the shortfall is more than one "
                          "stop can put in the car. Do not work one out from these "
                          "numbers")
        elif save is not None:
            spoken = (f"Fuel is short by {abs(margin):.1f} {unit}, about "
                      f"{short_laps:.1f} laps — save {save:.2f} {unit} per lap or we stop.")
            save_basis = None
        else:
            spoken = (f"We're short on fuel — about {laps_of_fuel:.1f} laps in the "
                      f"tank at {per_lap:.2f} {unit} a lap, and {laps:.0f} laps to "
                      f"run. That's more than you can save.")
            save_basis = ("no saving target exists — the shortfall is more than a "
                          "driver can lift and coast back. Do not work one out from "
                          "these numbers")

        stop_required = save is None
        if stop_required:
            stops = stop_window.get("stops_required")
            earliest = stop_window.get("earliest_lap")
            latest = stop_window.get("latest_lap")
            pit_now = bool(stop_window.get("pit_now"))
            window_open = stop_window.get("window_open")
            if stops is None:
                spoken += " A fuel stop is required"
                if pit_now:
                    spoken += "; I'd stop this lap because this tank is at its limit."
                elif latest is not None:
                    spoken += f", but not now — stay out and stop by lap {latest}."
                else:
                    spoken += ", but I haven't got the tank size to time it."
            else:
                count = "One fuel stop is" if stops == 1 else f"{stops} fuel stops are"
                spoken += f" {count} required."
                if pit_now:
                    spoken += " I'd stop this lap; the first window closes now."
                elif window_open:
                    spoken += f" The first window is open and closes on lap {latest}."
                else:
                    spoken += (f" The first window is lap {earliest} to lap {latest}; "
                               f"stay out for now.")

    if estimated_from_clock:
        spoken += (f" That's {laps:.0f} laps at your current pace — an estimate "
                   f"from the clock, not a lap count.")
    if extra_lap_margin is not None and extra_lap_margin >= 0:
        spoken += f" Even an extra timed lap leaves {extra_lap_margin:.1f} {unit} spare."

    # If the last lap burnt far less than a racing lap, the driver is behind
    # a safety car or cruising, and the racing average is pessimistic. Saying
    # "you are short" while they cruise home is the wrong call.
    last = car.fuel_last_lap
    if last is not None and per_lap > 0 and car.fuel_last_lap_clean is True:
        ratio = last / per_lap
        if ratio < 0.7:
            spoken += (f" Last lap only used {last:.2f} {unit} though — if it stays "
                       f"this slow you will go further than that.")
        elif ratio > 1.3:
            spoken += (f" Last lap used {last:.2f} {unit}, well above your average — "
                       f"that pace will not reach the flag.")

    return _yes(
        spoken,
        fuel_in_tank=round(have, 2),
        unit=unit,
        burn_last_lap=last,
        burn_last_lap_clean=car.fuel_last_lap_clean,
        burn_this_stint=car.fuel_used_stint,
        burn_basis="clean racing laps; in-laps, out-laps and caution laps excluded "
                   "from the average; burn_last_lap is raw and only changes the "
                   "range call when burn_last_lap_clean is true",
        burn_per_lap=round(typical_burn, 3),
        burn_per_lap_low=round(burn_low, 3),
        burn_per_lap_high=round(burn_high, 3),
        fuel_burn_samples=burn_samples,
        fuel_confidence=fuel_confidence,
        laps_to_go=round(laps, 1),
        fuel_needed=round(needed_likely, 2),
        fuel_needed_conservative=round(needed_conservative, 2),
        margin=round(margin_likely, 2),
        fuel_margin_best=round(margin_best, 2),
        fuel_margin_conservative=round(margin_conservative, 2),
        extra_lap_margin=(None if extra_lap_margin is None
                          else round(extra_lap_margin, 2)),
        laps_of_fuel=round(laps_of_fuel, 1),
        save_per_lap_required=None if save is None else round(save, 3),
        save_basis=save_basis,
        strategy_available=True,
        stop_required=stop_required,
        stops_required=(stop_window.get("stops_required")
                        if stop_required else 0),
        pit_now=(bool(stop_window.get("pit_now")) if stop_required else False),
        window_open=(stop_window.get("window_open") if stop_required else False),
        next_window_open=(stop_window.get("earliest_lap")
                          if stop_required else None),
        next_window_close=(stop_window.get("latest_lap")
                           if stop_required else None),
        will_finish=margin_conservative >= 0,
        tank_capacity=None if not capacity or capacity <= 0 else round(capacity, 2),
        one_stop_covers_it=one_stop_covers_it,
    )


def pit_window(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """Earliest and latest lap we can stop and still reach the flag.

    Latest is set by the fuel in the tank; earliest by whether a full tank
    from that point still covers the remaining distance.
    """
    typical_burn, burn_low, burn_high, burn_samples, fuel_confidence = \
        _fuel_burn_bounds(car)
    if typical_burn is None or burn_low is None or burn_high is None:
        return _no("I need three green laps before I can call your pit window.")
    per_lap = burn_high
    have = car.fuel_level
    if have is None:
        return _no("I've got no fuel reading.")

    if not _is_race(track):
        laps_of_fuel = math.floor(have / per_lap)
        return _no(
            f"There's no pit window outside a race — you've got about "
            f"{laps_of_fuel} laps of fuel to play with.")

    laps = _laps_to_go(car, track)
    if laps is None:
        return _no("I don't know how many laps are left, so I can't give you a window.")

    best_margin = have - laps * burn_low
    conservative_margin = have - laps * burn_high
    if best_margin >= 0 > conservative_margin:
        return _yes(
            f"The fuel window is marginal — measured burn of {burn_low:.2f} to "
            f"{burn_high:.2f} {getattr(car, 'fuel_unit', 'litres')} spans the "
            f"finish, so I wouldn't call a stop yet.",
            stop_required=None, stops_required=None, pit_now=False,
            window_open=False, laps_to_go=round(laps, 1),
            fuel_burn_samples=burn_samples, fuel_confidence=fuel_confidence,
            fuel_margin_best=round(best_margin, 2),
            fuel_margin_conservative=round(conservative_margin, 2),
        )

    window = _fuel_stop_window(car, track, laps, per_lap, have)
    laps_of_fuel = window["laps_of_fuel"]
    laps_on_current_fuel = window["laps_on_current_fuel"]
    earliest = window["earliest_lap"]
    latest = window["latest_lap"]
    stops = window["stops_required"]
    can_run_to_end = window["tank_covers_remaining"]

    if laps_of_fuel >= laps:
        return _yes(
            f"No stop needed — {have:.1f} {getattr(car, 'fuel_unit', 'litres')} "
            f"covers the {laps:.0f} laps left.",
            stop_required=False, laps_to_go=round(laps, 1),
            stops_required=0, pit_now=False, window_open=False,
            laps_on_current_fuel=laps_on_current_fuel,
            latest_lap=latest, earliest_lap=earliest,
        )

    if stops is not None:
        count = "One stop is" if stops == 1 else f"{stops} stops are"
        if window["pit_now"]:
            spoken = (f"{count} required; I'd stop this lap because the first fuel "
                      f"window closes now.")
        elif window["window_open"]:
            spoken = (f"{count} required. The first window is open and closes on "
                      f"lap {latest}; about {laps_of_fuel:.1f} laps of fuel left.")
        else:
            spoken = (f"{count} required. The first window is lap {earliest} to "
                      f"lap {latest}; stay out for now.")
    else:
        if window["pit_now"]:
            spoken = ("I'd stop this lap because the current tank is at its limit; "
                      "I haven't got the tank capacity to count the remaining stops.")
        else:
            spoken = (f"Stay out for now; stop by lap {latest}. I haven't got the "
                      f"tank capacity to count the remaining stops.")

    return _yes(
        spoken,
        stop_required=True,
        stops_required=stops,
        pit_now=window["pit_now"],
        window_open=window["window_open"],
        laps_to_go=round(laps, 1),
        laps_on_current_fuel=laps_on_current_fuel,
        laps_of_fuel=round(laps_of_fuel, 1),
        earliest_lap=earliest,
        latest_lap=latest,
        tank_covers_remaining=can_run_to_end,
    )


def car_resources(car: CarTelemetry, track: TrackConfig, topic: str) -> Dict[str, Any]:
    """Series-dependent controls and allowances published by iRacing."""
    key = " ".join(str(topic or "").lower().replace("-", " ").split())
    if key in ("pits", "pit open", "pits open"):
        if car.pits_open is None:
            return _no("I haven't got pit-lane availability.")
        return _yes(
            f"The pits are {'open' if car.pits_open else 'closed'}.",
            topic="pits", pits_open=car.pits_open)

    if key in ("tyres", "tires", "tyre sets", "tire sets"):
        sets = dict(getattr(car, "tire_sets_available", {}) or {})
        if not sets:
            return _no("I haven't got a remaining tyre-set count.")
        total = sets.get("total")
        if total == "unlimited":
            spoken = "Tyre sets are unlimited in this session."
        elif isinstance(total, int):
            spoken = f"You've got {total} tyre set{'s' if total != 1 else ''} remaining."
        else:
            parts = [f"{name} {value}" for name, value in sets.items()]
            spoken = "Tyre sets remaining: " + ", ".join(parts) + "."
        return _yes(spoken, topic="tyres", tire_sets_available=sets)

    if key in ("push to pass", "p2p"):
        count = car.push_to_pass_count
        active = car.push_to_pass_active
        if count is None and active is None:
            return _no("I haven't got push-to-pass status for this car.")
        bits = []
        if active is not None:
            bits.append("Push-to-pass is active" if active else "Push-to-pass is not active")
        if count is not None:
            if _is_race(track):
                bits.append(f"{count} use{'s' if count != 1 else ''} remaining")
            else:
                bits.append(f"the session count is {count}")
        return _yes(", ".join(bits) + ".", topic="push_to_pass",
                    push_to_pass_count=count, push_to_pass_active=active)

    if key in ("joker", "joker lap", "joker laps"):
        count = car.joker_laps_remaining
        if count is None and car.on_joker_lap is None:
            return _no("I haven't got joker-lap status in this session.")
        if car.on_joker_lap:
            spoken = "You're on the joker lap now"
            if count is not None:
                spoken += f", with {count} still remaining after this one"
            spoken += "."
        elif count == 1:
            spoken = "You've got one joker lap remaining."
        elif count is not None:
            spoken = f"You've got {count} joker laps remaining."
        else:
            spoken = "You're not on the joker lap now."
        return _yes(spoken, topic="joker", joker_laps_remaining=count,
                    on_joker_lap=car.on_joker_lap)

    if key in ("pit service", "pit stop", "service"):
        requests = list(getattr(car, "pit_service_requests", []) or [])
        tyres = {"left front tyre", "right front tyre",
                 "left rear tyre", "right rear tyre"}
        if tyres.issubset(requests):
            requests = [item for item in requests if item not in tyres]
            requests.insert(0, "all four tyres")
        if car.pit_autofill_active and "fuel" not in requests:
            requests.append("fuel autofill")
        elif car.pit_autofill_active and "fuel" in requests:
            requests[requests.index("fuel")] = "fuel autofill"
        if not requests and car.pit_tire_compound is None:
            return _no("No pit service is selected right now.")
        spoken = "Pit service selected: " + ", ".join(requests or ["tyre compound change"]) + "."
        return _yes(
            spoken, topic="pit_service", requests=requests,
            autofill_active=car.pit_autofill_active,
            # This calculator receives the selected index, not the player's
            # session-YAML compound table.
            pit_tire_compound=car.pit_tire_compound,
        )

    return _no("Tell me whether you mean the pits, tyre sets, push-to-pass, joker laps or pit service.")


def race_control_report(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """Current flag and penalty, with race-control wording owned by code."""
    if car.penalty:
        spoken = car.penalty[0].upper() + car.penalty[1:] + "."
    elif car.flag:
        spoken = f"{car.flag}." if "flag" in car.flag.lower() else f"{car.flag} flag."
    else:
        return _no("I've got no race-control status right now.")
    return _yes(spoken, flag=car.flag or None, penalty=car.penalty)


def gap_to(car: CarTelemetry, track: TrackConfig, target: str) -> Dict[str, Any]:
    """Gap between you and any car in the field, by name, number or position."""
    if not track.field:
        return _no("I haven't got the order in front of me.")
    them = _find(track.field, target)
    if them is None:
        return _no(f"I can't find {target} out there.")
    me = _me(track)
    if me is None:
        return _no("I can't place you in the order right now.")
    if them.is_player:
        return _no("That's you.")
    context = _class_context(track, them)

    # Both gaps are time behind the leader, so the difference is the gap
    # between the two cars — but only while both are on the lead lap.
    # Position 1 is by definition zero behind the leader; older snapshots
    # left it as None.
    my_gap = 0.0 if me.position == 1 else me.gap
    their_gap = 0.0 if them.position == 1 else them.gap
    if my_gap is None or their_gap is None:
        return _no(f"I've got no time on {them.describe()}.")

    delta = their_gap - my_gap
    ahead = delta < 0
    laps_apart = them.laps_down - me.laps_down

    if laps_apart:
        word = "ahead of" if laps_apart < 0 else "behind"
        spoken = (f"{them.describe()} is P{them.position}, "
                  f"{_plural(abs(laps_apart), 'lap')} {word} you — "
                  f"the time gap doesn't mean anything across laps.")
        if context["traffic_description"]:
            spoken += f" That's {context['traffic_description']}, not a battle for this position."
        return _yes(spoken, position=them.position, laps_apart=laps_apart,
                    gap_seconds=None, driver=them.name, car_number=them.car_number,
                    same_lap=False, **context)

    # "Ahead" and "behind" mean position on track, which is what the gap is
    # in a race. In practice and qualifying the same subtraction is a
    # lap-time difference between two cars that may never have been near
    # each other, so it has to be said as pace, not as track position.
    if _is_race(track):
        spoken = (f"{them.describe()} is P{them.position}, {_spoken_delta(delta)} "
                  f"{'ahead' if ahead else 'behind'}.")
    else:
        spoken = (f"{them.describe()} is P{them.position}, {_spoken_delta(delta)} "
                  f"{'quicker' if ahead else 'slower'} than you on best lap.")
    if them.in_pits:
        spoken += " They're in the pits."
    if _is_race(track) and context["traffic_description"]:
        spoken += (f" That's {context['traffic_description']}, not a battle for "
                   "this position.")

    return _yes(
        spoken,
        driver=them.name,
        car_number=them.car_number,
        position=them.position,
        gap_seconds=round(abs(delta), 2),
        ahead=ahead,
        in_pits=them.in_pits,
        best_lap=them.best_lap,
        same_lap=True,
        laps_apart=0,
        **context,
    )


def _radio_rival_name(rival: Rival) -> str:
    """One compact identifier for a rival; never repeat name plus car number."""
    return rival.name or (f"car {rival.car_number}" if rival.car_number else "the car")


def position_report(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """Current position with the physical battle gaps, not gap to the leader."""
    if _race_not_started(track):
        position = getattr(track, "grid_position", None) or car.position
        class_position = (getattr(track, "grid_class_position", None)
                          or car.class_position)
        if not position:
            return _no("I haven't got your starting position yet.")
        return _yes(
            _starting_position_spoken(track, position, class_position),
            position=position, class_position=class_position,
            ahead=None, behind=None, race_started=False,
            basis="published starting position; no places gained or lost before green",
        )

    if not car.position:
        return _no("I haven't got your position right now.")

    if getattr(track, "finished", False):
        # You do not "finish" a qualifying session, you qualify in it — and
        # the driver still has the race to come.
        verb = "qualified" if session_kind(track.session_type) == "qualify" else "finished"
        return _yes(f"You {verb} P{car.position}.", position=car.position,
                    class_position=car.class_position, ahead=None, behind=None)

    bits = [f"You're P{car.position}"]
    if car.class_position and car.class_position != car.position:
        bits[0] += f", P{car.class_position} in class"

    adjacent = []
    if _is_race(track):
        if car.ahead is not None and car.ahead.gap is not None:
            adjacent.append(
                f"{_radio_rival_name(car.ahead)} {car.ahead.gap:.2f} seconds ahead")
        elif car.position == 1:
            bits[0] = "You're leading" + (
                f", P{car.class_position} in class"
                if car.class_position and car.class_position != 1 else "")
        if car.behind is not None and car.behind.gap is not None:
            adjacent.append(
                f"{_radio_rival_name(car.behind)} {car.behind.gap:.2f} seconds behind")

    spoken = bits[0]
    if adjacent:
        spoken += "; " + ", ".join(adjacent)
    spoken += "."
    return _yes(
        spoken,
        position=car.position,
        class_position=car.class_position,
        ahead=(None if car.ahead is None else {
            "driver": _radio_rival_name(car.ahead), "gap_seconds": car.ahead.gap}),
        behind=(None if car.behind is None else {
            "driver": _radio_rival_name(car.behind), "gap_seconds": car.behind.gap}),
        basis="live position plus physical adjacent-car gaps; not gap to leader",
    )


def adjacent_gap_trend(car: CarTelemetry, track: TrackConfig,
                       direction: str) -> Dict[str, Any]:
    """Is the physical car ahead/behind gaining, from live distance samples?

    This is deliberately separate from pace_compare: best laps describe
    potential over a lap, while this answers what the gap is doing now.
    Every phrase, including contact time, is calculated here so the model
    cannot mix a time gap with a metres-per-second rate.
    """
    direction = str(direction or "").strip().lower()
    if direction not in ("ahead", "behind"):
        return _no("I need to know whether you mean the car ahead or behind.")

    rival = car.ahead if direction == "ahead" else car.behind
    if rival is None or not rival.known:
        return _no(f"I haven't got a car {direction} right now.")

    status = (car.closing_ahead_status if direction == "ahead"
              else car.closing_behind_status)
    observed = (car.closing_ahead_seconds if direction == "ahead"
                else car.closing_behind_seconds)
    change = (car.closing_ahead_change if direction == "ahead"
              else car.closing_behind_change)
    segments = (car.closing_ahead_segments if direction == "ahead"
                else car.closing_behind_segments)
    gap_seconds = rival.gap
    gap_metres = rival.gap_metres
    if gap_seconds is not None:
        gap_words = f"{gap_seconds:.2f} seconds"
    elif gap_metres is not None:
        if getattr(car, "units", "metric") == "imperial":
            gap_words = f"{gap_metres * 3.28084:.0f} feet"
        else:
            gap_words = f"{gap_metres:.0f} metres"
    else:
        return _no(f"I've got the car {direction}, but no live gap to it.")

    who = _radio_rival_name(rival)
    where = "ahead" if direction == "ahead" else "behind"
    context = _class_context(track, rival)

    if status in ("unavailable", "waiting"):
        spoken = f"{who} is {gap_words} {where}; no closing trend yet."
        return _yes(spoken, direction=direction, confidence="waiting",
                    observed_seconds=round(observed, 1), rate_metres_per_second=None,
                    gap_seconds=gap_seconds, gap_metres=gap_metres,
                    contact_seconds=None, **context)

    if status == "fluctuating":
        spoken = f"The gap to {who} is fluctuating around {gap_words}; no clear trend."
        return _yes(spoken, direction=direction, confidence=status,
                    observed_seconds=round(observed, 1), rate_metres_per_second=None,
                    gap_seconds=gap_seconds, gap_metres=gap_metres,
                    contact_seconds=None, **context)

    if status == "steady":
        spoken = f"The gap to {who} is holding at {gap_words}."
        return _yes(spoken, direction=direction, confidence="steady",
                    observed_seconds=round(observed, 1), rate_metres_per_second=None,
                    gap_change_seconds=0.0, mini_sectors=segments,
                    gap_seconds=gap_seconds, gap_metres=gap_metres,
                    contact_seconds=None, **context)

    if change is None:
        spoken = f"The gap to {who} is fluctuating around {gap_words}; no clear trend."
        return _yes(spoken, direction=direction, confidence="fluctuating",
                    observed_seconds=round(observed, 1), rate_metres_per_second=None,
                    gap_change_seconds=None, mini_sectors=segments,
                    gap_seconds=gap_seconds, gap_metres=gap_metres,
                    contact_seconds=None, **context)

    closing = change > 0
    if status == "early":
        if direction == "ahead":
            movement = (f"You're gaining on {who} right now" if closing
                        else f"{who} is edging away right now")
        else:
            movement = (f"{who} is gaining right now" if closing
                        else f"You're edging away from {who} right now")
        spoken = f"{movement}; gap {gap_words}."
    else:
        delta_words = _spoken_delta(abs(change))
        if direction == "ahead":
            movement = (f"You're gaining on {who}" if closing
                        else f"{who} is pulling away")
        else:
            movement = (f"{who} is gaining" if closing
                        else f"You're pulling away from {who}")
        spoken = (f"{movement}, {delta_words} over the last {segments} "
                  f"mini-sectors; gap {gap_words}.")

    return _yes(
        spoken,
        direction=direction,
        confidence=status,
        observed_seconds=round(observed, 1),
        rate_metres_per_second=None,
        gap_change_seconds=round(change, 2),
        mini_sectors=segments,
        gap_seconds=gap_seconds,
        gap_metres=gap_metres,
        contact_seconds=None,
        basis="CarIdxEstTime gap sampled at fixed lap-progress mini-sectors",
        **context,
    )


def standings(car: CarTelemetry, track: TrackConfig,
              top: Optional[int] = None, around_me: Optional[int] = None) -> Dict[str, Any]:
    """A slice of the running order — the top N, or the cars either side of you."""
    field = track.field
    if not field:
        return _no("I haven't got the order in front of me.")

    if around_me:
        me = _me(track)
        if me is None:
            return _no("I can't place you in the order right now.")
        idx = field.index(me)
        n = int(around_me)
        rows = field[max(0, idx - n): idx + n + 1]
        label = f"around you (P{me.position})"
    else:
        n = int(top) if top else 5
        rows = field[:n]
        label = f"top {len(rows)}"

    entries = []
    spoken_bits = []
    for r in rows:
        context = (_class_context(track, r) if not r.is_player else {
            "same_class_as_you": True, "class_speed": None,
            "position_rival": None, "traffic_description": ""})
        who = r.name or (f"car {r.car_number}" if r.car_number else "unknown")
        entries.append({
            "position": r.position, "driver": r.name, "car_number": r.car_number,
            "class_position": r.class_position,
            "car_model": r.car_model or None,
            "car_model_short": r.car_model_short or None,
            "car_class": r.car_class_name or None,
            "car_class_id": r.car_class_id,
            "car_id": r.car_id,
            "gap_to_leader": r.gap, "best_lap": r.best_lap,
            "in_pits": r.in_pits, "laps_down": r.laps_down, "is_you": r.is_player,
            "same_class_as_you": context["same_class_as_you"],
            "class_speed": context["class_speed"],
            "position_rival": context["position_rival"],
        })
        bit = (f"P{r.position} {'you' if r.is_player else who}" if r.position
               else f"{'you' if r.is_player else who} (no time yet)")
        if r.class_position and r.class_position != r.position:
            bit += f", P{r.class_position} in class"
        if r.position != 1 and r.gap is not None:
            bit += f" +{r.gap:.1f}s"
        if r.in_pits:
            bit += " (pits)"
        if context["traffic_description"]:
            bit += f" ({context['traffic_description']}, not for position)"
        spoken_bits.append(bit)

    spoken = ", ".join(spoken_bits)
    if any("+" in b for b in spoken_bits):
        # The same quantity is not the same thing in every session — see
        # the reader, where the gap outside a race is a deficit to the best
        # lap rather than time behind the leader.
        spoken += (" (+ figures are seconds behind the leader)" if _is_race(track)
                   else " (+ figures are seconds off the best lap of the session,"
                        " not a gap on track)")
    return _yes(spoken, scope=label, field_size=track.field_size,
                gaps_are="time behind the leader" if _is_race(track)
                else "seconds off the session best lap",
                entries=entries)


def fastest_lap(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """Who holds the session's best lap, and how yours compares."""
    best = track.fastest_lap
    if best is None or not best.known or not best.best_lap:
        return _no("No fastest lap has been set yet.")

    mine = car.best_lap_time
    spoken = f"Fastest lap is {best.describe()} on {_laptime(best.best_lap)}"
    delta = None
    if mine:
        delta = mine - best.best_lap
        if delta <= 0.001:
            spoken += " — that's you."
        else:
            spoken += f"; you're {_spoken_delta(delta)} off on {_laptime(mine)}."
    else:
        spoken += "; you haven't set a clean lap yet."

    return _yes(spoken, driver=best.name, car_number=best.car_number,
                lap_time=best.best_lap, lap_time_spoken=_laptime(best.best_lap),
                your_best=mine, delta=None if delta is None else round(delta, 3),
                is_you=bool(mine and delta is not None and delta <= 0.001))


def pace_compare(car: CarTelemetry, track: TrackConfig, target: str) -> Dict[str, Any]:
    """Are we catching them, and how long until we're on their gearbox?

    Uses best-lap pace, which is what the SDK publishes per car — there is
    no per-rival rolling average, so this is a comparison of potential, not
    of the last three laps. Said plainly rather than dressed up.
    """
    if not track.field:
        return _no("I haven't got the order in front of me.")
    them = _find(track.field, target)
    if them is None:
        return _no(f"I can't find {target} out there.")
    if not them.best_lap:
        return _no(f"{them.describe()} hasn't set a time yet.")

    mine = car.best_lap_time
    if not mine:
        return _no("You haven't set a clean lap yet, so there's nothing to compare.")

    per_lap = them.best_lap - mine          # positive = you are faster
    context = _class_context(track, them)
    gap = gap_to(car, track, target)
    gap_seconds = gap.get("gap_seconds") if gap.get("available") else None

    ahead = gap.get("ahead")
    # "laps to contact" only means something when the quicker car is behind
    # the slower one. A faster car already ahead is simply escaping, and a
    # slower car behind is simply dropping away — quoting a lap count there
    # invites the model to narrate a catch that will never happen.
    closing = (per_lap > 0 and ahead is True) or (per_lap < 0 and ahead is False)
    laps_to_contact = None
    if closing and gap_seconds:
        laps_to_contact = gap_seconds / abs(per_lap)

    margin = _spoken_delta(per_lap)
    if abs(per_lap) < 0.02:
        spoken = f"You and {them.describe()} are matched on pace, within a couple of hundredths."
    elif per_lap > 0:
        spoken = f"You are {margin} a lap quicker than {them.describe()}"
        if laps_to_contact:
            spoken += f" — about {_plural(laps_to_contact, 'lap')} to catch them."
        elif ahead is False:
            spoken += ", and they are behind you, so the gap grows."
        else:
            spoken += "."
    else:
        spoken = f"{them.describe()} is {margin} a lap quicker than you"
        if laps_to_contact:
            spoken += f" — they are on you in about {_plural(laps_to_contact, 'lap')}."
        elif ahead is True:
            spoken += ", and they are ahead, so they are pulling away."
        else:
            spoken += "."

    if _is_race(track) and context["traffic_description"]:
        spoken += (f" That's {context['traffic_description']}, not a battle for "
                   "this position.")

    return _yes(
        spoken,
        driver=them.name, car_number=them.car_number, position=them.position,
        their_best=them.best_lap, your_best=mine,
        pace_delta_per_lap=round(per_lap, 3),
        gap_seconds=gap_seconds,
        they_are_ahead=ahead,
        closing=closing,
        laps_to_contact=None if laps_to_contact is None else round(laps_to_contact, 1),
        basis="best lap of the session for each car",
        **context,
    )


def driver_report(car: CarTelemetry, track: TrackConfig,
                  target: str = "") -> Dict[str, Any]:
    """Everything known about one car: pace, gap, rating, where they are.

    One tool rather than a family of them. CrewChief needs a separate
    grammar rule for each of "what's P4's last lap", "what's Rossi's
    iRating", "what class is the car ahead" — a closed grammar has to
    enumerate the phrasings. Here the model already understands the
    question and only needs the numbers, so the useful split is by *car*,
    not by field.

    Reports last lap alongside best: best says whether they were ever
    quick, last says whether they are quick now, and a rival two seconds
    off his own best is a rival in trouble.
    """
    if not track.field:
        return _no("I've got no timing data on the other cars.")

    query = str(target or "").strip().lower()
    them: Optional[Rival] = None
    if query in ("ahead", "in front", "the car ahead", "the car in front",
                 "the guy ahead", "the guy in front", "next", "car ahead"):
        them = _classified(track, car.ahead) or _neighbour(track, -1)
    elif query in ("behind", "the car behind", "the guy behind", "car behind"):
        them = _classified(track, car.behind) or _neighbour(track, +1)
    else:
        them = _find(track.field, query)
    if them is None:
        return _no("I can't find that car in the timing screen.")

    me = _me(track)
    bits = [f"{them.describe()} is P{them.position}" if them.position
            else f"{them.describe()} has no lap time in yet, so no place on the"
                 f" timing screen"]
    if them.class_position and me and me.class_position \
            and them.class_position != them.position:
        bits.append(f"P{them.class_position} in class")

    model = them.car_model_short or them.car_model
    if model:
        bits.append(f"driving {model}")
    if them.car_class_name:
        bits.append(f"in the {them.car_class_name} class")
    context = _class_context(track, them)
    same_class = context["same_class_as_you"]
    if same_class is True:
        bits.append("same class as you")
    elif same_class is False:
        relation = context["class_speed"]
        bits.append(f"a {relation} class than yours" if relation
                    else "a different class to you")

    if them.last_lap:
        bits.append(f"last lap {_laptime(them.last_lap)}")
    if them.best_lap:
        bits.append(f"best {_laptime(them.best_lap)}")
    # Off their own best by a margin is the tell that something is wrong —
    # a flat spot, a mistake, or traffic.
    if them.last_lap and them.best_lap and them.last_lap - them.best_lap > 1.0:
        bits.append(f"{_spoken_delta(them.last_lap - them.best_lap)} off their own best")

    # Same tyre or not is answerable from the indices. The player's mapping
    # cannot safely name a rival's compound in a mixed-car field.
    if them.tyre_compound is not None and car.tyre_compound is not None:
        if them.tyre_compound != car.tyre_compound:
            bits.append("on a different tyre compound to you")
        else:
            bits.append("on the same tyre as you")

    # A rival who has left still holds their classified place on a lap set
    # before they went — normal in practice, where people come and go all
    # session. Comparing against the time is fine; calling them a car you
    # are racing is not.
    if not getattr(them, "in_world", True):
        # Deliberately not "they left": in lone qualifying everyone else
        # runs their own session, so a car that was never on track with the
        # driver is not a car that quit.
        bits.append("not out on track now — that time was set earlier")
    elif them.in_pits:
        bits.append("in the pits")
    elif them.track_surface and them.track_surface != "on track":
        bits.append(them.track_surface)
    if them.laps_down:
        bits.append(f"{_plural(abs(them.laps_down), 'lap')} "
                    f"{'down' if them.laps_down > 0 else 'up'}")

    return _yes(
        "; ".join(bits) + ".",
        driver=them.name, car_number=them.car_number,
        car_model=them.car_model or None,
        car_model_short=them.car_model_short or None,
        car_class=them.car_class_name or None,
        car_class_id=them.car_class_id, car_id=them.car_id,
        same_class_as_you=same_class,
        class_speed=context["class_speed"],
        position_rival=context["position_rival"],
        position=them.position, class_position=them.class_position,
        last_lap=them.last_lap, best_lap=them.best_lap,
        gap_to_leader=them.gap, laps_down=them.laps_down,
        in_pits=them.in_pits, track_surface=them.track_surface,
        in_session=getattr(them, "in_world", True),
        push_to_pass_count=them.push_to_pass_count,
        push_to_pass_active=them.push_to_pass_active,
        # Index only — never name a compound, iRacing does not publish names.
        same_compound_as_you=(None if them.tyre_compound is None
                              or car.tyre_compound is None
                              else them.tyre_compound == car.tyre_compound),
        # iRating is a record of past results, not pace today. Handed over
        # as a number so the model can report it when asked, never as a
        # basis for predicting who is quicker.
        irating=them.irating, licence=them.licence, is_ai=them.is_ai,
    )


def sector_report(car: CarTelemetry, track: TrackConfig,
                  target: str = "") -> Dict[str, Any]:
    """Where the lap is being won or lost, by sector.

    A lap time says you were slow; a sector says where. That is the whole
    value here — it is the one answer the driver can act on before the next
    corner rather than after the session.

    With no target, compares the last lap against the driver's own best in
    each sector. With one, compares against that car's last lap.
    """
    mine = [t for t in (car.sector_times or [])]
    if not mine or all(t is None for t in mine):
        return _no("I haven't timed a full lap through the sectors yet.")

    if target:
        them = _find(track.field, str(target).strip().lower()) if track.field else None
        if str(target).strip().lower() in ("ahead", "in front", "the car ahead",
                                           "the car in front", "the guy ahead"):
            them = _classified(track, car.ahead) or _neighbour(track, -1)
        elif str(target).strip().lower() in ("behind", "the car behind",
                                             "the guy behind"):
            them = _classified(track, car.behind) or _neighbour(track, +1)
        if them is None:
            return _no("I can't find that car in the timing screen.")
        theirs = them.sector_times or []
        if not theirs or all(t is None for t in theirs):
            return _no(f"I haven't got a full lap of sectors for "
                       f"{them.describe()} yet.")
        return _compare_sectors(mine, theirs, them.describe(), car)

    bests = [t for t in (car.sector_bests or [])]
    if not bests or all(t is None for t in bests):
        return _no("I need a clean lap before I can tell you where you're losing it.")
    return _compare_sectors(mine, bests, "your best", car, own_best=True)


def _compare_sectors(mine, theirs, label, car, own_best=False):
    """Per-sector deltas, and the one that matters said out loud."""
    deltas = []
    for n in range(max(len(mine), len(theirs))):
        a = mine[n] if n < len(mine) else None
        b = theirs[n] if n < len(theirs) else None
        deltas.append(None if a is None or b is None else round(a - b, 3))

    usable = [(n, d) for n, d in enumerate(deltas) if d is not None]
    if not usable:
        return _no("I haven't got matching sectors to compare.")

    worst_n, worst_d = max(usable, key=lambda p: p[1])
    best_n, best_d = min(usable, key=lambda p: p[1])
    total = round(sum(d for _, d in usable), 3)

    if worst_d <= 0.02:
        spoken = (f"You're matching {label} everywhere — nothing in it."
                  if abs(total) < 0.05 else
                  f"You're up on {label} across the board, "
                  f"{_spoken_delta(abs(total))} in hand.")
    else:
        spoken = (f"Sector {worst_n + 1} is costing you "
                  f"{_spoken_delta(worst_d)} against {label}")
        if best_d < -0.02:
            spoken += f", but you're {_spoken_delta(abs(best_d))} up in sector {best_n + 1}."
        else:
            spoken += "."

    # The sum of personal bests is a lap nobody has driven, so it is only
    # ever offered as what is on the table - never as a lap time.
    on_the_table = None
    if own_best and all(t is not None for t in theirs):
        on_the_table = round(sum(theirs), 3)

    return _yes(
        spoken,
        sectors=len(deltas),
        your_sectors=mine,
        compared_with=label,
        their_sectors=theirs,
        deltas=deltas,
        worst_sector=worst_n + 1,
        worst_delta=worst_d,
        best_sector=best_n + 1,
        best_delta=best_d,
        total_delta=total,
        theoretical_best=on_the_table,
        basis="last completed lap per car; bests from clean laps only",
    )


def _classified(track: TrackConfig, neighbour: Optional[Rival]) -> Optional[Rival]:
    """Match an on-track neighbour to its row in the classification.

    car.ahead and car.behind identify the car the driver can actually see,
    but they are built from track order and carry no lap times — asking for
    "the car ahead's last lap" off that record answers with nothing. The
    classified row has the timing, so the identity comes from one and the
    numbers from the other.
    """
    if neighbour is None or not track.field:
        return None
    if neighbour.car_number:
        found = next((r for r in track.field
                      if r.car_number == neighbour.car_number), None)
        if found is not None:
            return found
    if neighbour.name:
        return next((r for r in track.field if r.name == neighbour.name), None)
    return None


def _neighbour(track: TrackConfig, offset: int) -> Optional[Rival]:
    """The classified car one place ahead (-1) or behind (+1) of the driver."""
    me = _me(track)
    if me is None:
        return None
    return next((r for r in track.field if r.position == me.position + offset), None)


def pace_trend(car: CarTelemetry, track: TrackConfig,
               laps: Optional[int] = None) -> Dict[str, Any]:
    """Are the lap times drifting, and which way?

    The question the engineer could not previously answer. Knowing you are
    slower than your best says nothing about direction; knowing you have lost
    three tenths a lap for three laps running is the difference between
    pushing and boxing.

    Only clean laps count. An in-lap, an out-lap or a lap with a spin in it
    is seconds slow for reasons that have nothing to do with the tyres, and
    including them would report a cliff that is not there.
    """
    history = list(getattr(track, "laps", []) or [])
    if not history:
        return _no("I haven't got any lap times logged yet.")

    clean = [l for l in history if l.clean]
    window = int(laps) if laps else 5
    recent = clean[-window:]
    if len(recent) < 3:
        skipped = len(history) - len(clean)
        detail = f" — {skipped} of the last few were in, out or scrappy laps" if skipped else ""
        return _no(f"I need three clean laps before I can call a trend{detail}.")

    times = [l.time for l in recent]
    # Least-squares slope over lap index: seconds gained or lost per lap.
    n = len(times)
    mean_x = (n - 1) / 2.0
    mean_y = sum(times) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (t - mean_y) for i, t in enumerate(times)) / denom if denom else 0.0

    first_last = times[-1] - times[0]
    spread = max(times) - min(times)

    # A flat slope across widely scattered laps is not stability, it is
    # inconsistency — reporting "pace is holding" over a seven-second spread
    # would be true of the trend line and useless to the driver.
    if abs(slope) < 0.05 and spread > 1.5:
        verdict, spoken = "inconsistent", (
            f"Lap times are all over the place — {_spoken_delta(spread)} between "
            f"your best and worst of the last {n}, with no clear trend.")
    # Below about five hundredths a lap is noise, not degradation.
    elif abs(slope) < 0.05:
        verdict, spoken = "stable", (
            f"Pace is holding — last {n} clean laps within "
            f"{_spoken_delta(spread)} of each other.")
    elif slope > 0:
        verdict, spoken = "degrading", (
            f"You're losing {_spoken_delta(slope)} a lap over the last {n} clean laps, "
            f"{_spoken_delta(first_last)} slower than {n} laps ago.")
    else:
        verdict, spoken = "improving", (
            f"You're finding {_spoken_delta(slope)} a lap over the last {n} clean laps.")

    return _yes(
        spoken,
        verdict=verdict,
        trend_per_lap=round(slope, 3),
        change_over_window=round(first_last, 3),
        laps_used=n,
        laps_skipped=len(history) - len(clean),
        fastest_in_window=round(min(times), 3),
        slowest_in_window=round(max(times), 3),
        spread=round(spread, 3),
        recent=[{"lap": l.lap, "time": round(l.time, 3), "position": l.position}
                for l in recent],
        basis="clean laps only; in-laps, out-laps, offs and outliers excluded",
    )


def _race_laps(track: TrackConfig) -> List[Any]:
    """The lap history for the segment running now, oldest first.

    The reader deliberately keeps laps across a session change — practice
    rolling into qualifying into the race — because the fuel and pace
    baselines are worth carrying. For a race story they are not: a lap set
    in practice would count toward "you gained three places". Laps recorded
    before the tag existed carry None and are kept, since dropping them
    would silently shorten the race.
    """
    history = list(getattr(track, "laps", []) or [])
    current = getattr(track, "session_num", None)
    if current is None:
        return history
    return [l for l in history
            if getattr(l, "session_num", None) in (current, None)]


def race_history(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """How the race has gone so far: where it started, where it stands.

    Every figure here is measured or published — the grid slot from the
    qualifying results, the position at each crossing, the laps led from the
    session results. Nothing infers who overtook whom, and nothing explains
    why a place changed. That distinction is the whole point: the engineer
    may invent warmth, never events.

    Answers mid-race and after the flag from the same numbers. What changes
    is only how the driver hears them, and the block already says which.
    """
    laps = _race_laps(track)
    grid = getattr(track, "grid_position", None)
    now = car.position or None

    if _race_not_started(track):
        position = grid or now
        class_position = (getattr(track, "grid_class_position", None)
                          or car.class_position)
        if not position:
            return _no("I haven't got your starting position yet.")
        return _yes(
            _starting_position_spoken(track, position, class_position),
            grid_position=grid,
            grid_class_position=getattr(track, "grid_class_position", None),
            position_now=None,
            places_gained=None,
            best_lap=None,
            best_lap_number=None,
            laps_recorded=0,
            laps_led=None,
            caution_flags=None,
            caution_laps=None,
            lead_changes=None,
            pit_laps=[],
            scrappy_laps=[],
            position_trace=[],
            history_partial=False,
            basis="published starting position; race history begins at green",
        )

    if not laps and grid is None:
        return _no("I haven't got anything logged for this one yet.")

    # Position at each crossing, reduced to the moments it actually moved.
    # One row per lap is sixty rows in a sixty-lap race — it costs tokens on
    # every call and reads worse than the three places it actually changed.
    trace: List[Dict[str, int]] = []
    for lap in laps:
        if lap.position is None:
            continue
        if not trace or trace[-1]["position"] != lap.position:
            trace.append({"lap": lap.lap, "position": lap.position})

    timed = [l for l in laps if l.time > 0]
    best = min(timed, key=lambda l: l.time) if timed else None
    pit_laps = [l.lap for l in laps if l.note == "pit lane"]
    scrappy = [{"lap": l.lap, "note": l.note} for l in laps
               if not l.clean and l.note and l.note != "pit lane"]

    # The reader keeps a bounded history, so a long race can outrun it. Say
    # so rather than describing the last hundred laps as the whole race.
    run = getattr(track, "race_laps", None)
    partial = bool(run and len(laps) < run - 1)

    bits: List[str] = []
    if grid is not None and now:
        moved = grid - now
        if moved > 0:
            bits.append(f"Started P{grid}, up to P{now} — {_plural(moved, 'place')} gained")
        elif moved < 0:
            bits.append(f"Started P{grid}, down to P{now} — {_plural(-moved, 'place')} lost")
        else:
            bits.append(f"Started P{grid} and you're still there")
    elif now:
        bits.append(f"You're P{now}")
        if grid is None:
            bits.append("I haven't got a grid position for this one")
    if best is not None:
        bits.append(f"best lap {_laptime(best.time)} on lap {best.lap}")
    laps_led = getattr(track, "laps_led", None)
    if laps_led:
        bits.append(f"you've led {_plural(laps_led, 'lap')}")
    if pit_laps:
        bits.append(f"stopped on lap {pit_laps[-1]}" if len(pit_laps) == 1
                    else f"{_plural(len(pit_laps), 'stop')}, last on lap {pit_laps[-1]}")
    if scrappy:
        bits.append(f"{_plural(len(scrappy), 'scrappy lap')} — {scrappy[-1]['note']} on lap {scrappy[-1]['lap']}")
    cautions = getattr(track, "caution_flags", None)
    if cautions:
        bits.append(f"{_plural(cautions, 'caution')}")

    if not bits:
        return _no("I haven't got anything logged for this one yet.")

    return _yes(
        ". ".join(bits) + ".",
        grid_position=grid,
        grid_class_position=getattr(track, "grid_class_position", None),
        position_now=now,
        places_gained=(grid - now) if (grid is not None and now) else None,
        best_lap=round(best.time, 3) if best else None,
        best_lap_number=best.lap if best else None,
        laps_recorded=len(laps),
        laps_led=laps_led,
        caution_flags=cautions,
        caution_laps=getattr(track, "caution_laps", None),
        lead_changes=getattr(track, "lead_changes", None),
        pit_laps=pit_laps,
        scrappy_laps=scrappy,
        position_trace=trace,
        history_partial=partial,
        basis=("position at each start/finish crossing; grid from the qualifying "
               "result; nothing here says who overtook whom"),
    )


def stint_projection(car: CarTelemetry, track: TrackConfig) -> Dict[str, Any]:
    """Where the fuel runs out relative to the flag."""
    if not _is_race(track):
        return _no("There's no race distance to compare against in this session.")

    laps = _laps_to_go(car, track)
    if car.fuel_level is not None and car.fuel_per_lap:
        range_laps = car.fuel_level / car.fuel_per_lap
    else:
        range_laps = car.fuel_remaining_laps
    if range_laps is None:
        return _no("I haven't got a fuel range yet.")
    if laps is None:
        return _no("I can't tell how many laps are left.")

    surplus = range_laps - laps
    tail = (f"{surplus:.1f} laps in hand." if surplus >= 0
            else f"{abs(surplus):.1f} laps short.")
    spoken = f"{range_laps:.1f} laps of fuel against {laps:.0f} to run — {tail}"
    return _yes(spoken, laps_of_fuel=round(range_laps, 1),
                laps_to_go=round(laps, 1), surplus_laps=round(surplus, 1),
                stint_laps_so_far=car.stint_laps)


# ── Tool registry ───────────────────────────────────────────────────
# One definition per calculation: the JSON schema advertised to the model
# and the function that answers it. Keeping both here means a new tool
# cannot be advertised without an implementation, or vice versa.

_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "fuel_plan",
        "description": (
            "Report current fuel immediately, and when a burn average exists work "
            "out finish margin, saving target, required stop count and whether the "
            "next fuel window actually requires pitting this lap. A required stop "
            "later in the race never means pit now."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_laps": {
                    "type": "number",
                    "description": "Optional: run the sum over this many laps instead of to the flag.",
                },
                "target_minutes": {
                    "type": "number",
                    "description": (
                        "Optional: run the sum over this many minutes instead. "
                        "Converted to laps here — never do that division yourself."
                    ),
                }
            },
        },
        "fn": fuel_plan,
    },
    {
        "name": "pit_window",
        "description": (
            "The first valid stop window and number of stops required, based on "
            "current fuel, burn and tank capacity. This is the authority on whether "
            "to pit now."
        ),
        "parameters": {"type": "object", "properties": {}},
        "fn": pit_window,
    },
    {
        "name": "car_resources",
        "description": (
            "Published series-dependent status: whether pits are open, remaining "
            "tyre sets, push-to-pass, joker laps, or selected pit service. Use this "
            "instead of guessing whether a car or series supports one of them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["pits", "tyres", "push to pass", "joker", "pit service"],
                }
            },
            "required": ["topic"],
        },
        "fn": car_resources,
    },
    {
        "name": "gap_to",
        "description": (
            "The gap in seconds between the driver and any other car. Use for "
            "questions about a specific rival, the leader, or a position."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Driver name, car number (\"#44\"), position (\"P3\"), or \"leader\".",
                }
            },
            "required": ["target"],
        },
        "fn": gap_to,
    },
    {
        "name": "position_report",
        "description": (
            "The driver's current position plus gaps to the physical cars directly "
            "ahead and behind. Use for position or position-status questions; do not "
            "substitute the gap to the leader."
        ),
        "parameters": {"type": "object", "properties": {}},
        "fn": position_report,
    },
    {
        "name": "adjacent_gap_trend",
        "description": (
            "The live, telemetry-measured closing or opening trend to the physical "
            "car immediately ahead or behind. Use for 'am I gaining/catching', "
            "'is the gap closing', and 'is the car behind catching me'. This is the "
            "only calculator for current relative movement; do not substitute best laps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["ahead", "behind"],
                    "description": "Which physically adjacent car to measure.",
                }
            },
            "required": ["direction"],
        },
        "fn": adjacent_gap_trend,
    },
    {
        "name": "standings",
        "description": "A slice of the classified running order: the top N, or the cars around the driver.",
        "parameters": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Return the leading N cars."},
                "around_me": {"type": "integer", "description": "Return N cars either side of the driver."},
            },
        },
        "fn": standings,
    },
    {
        "name": "fastest_lap",
        "description": "Who holds the session's fastest lap and how the driver's best compares.",
        "parameters": {"type": "object", "properties": {}},
        "fn": fastest_lap,
    },
    {
        "name": "pace_compare",
        "description": (
            "Compare best-lap potential with another named car and estimate how many "
            "laps until they catch, or are caught. This is not the live gap trend; "
            "use adjacent_gap_trend for the physical car ahead or behind."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Driver name, car number, position, or \"leader\".",
                }
            },
            "required": ["target"],
        },
        "fn": pace_compare,
    },
    {
        "name": "pace_trend",
        "description": (
            "Whether the driver's lap times are getting slower, faster or holding "
            "steady, over recent clean laps. Use for tyre degradation, consistency, "
            "and whether to push or manage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "laps": {
                    "type": "integer",
                    "description": "How many recent clean laps to look at (default 5).",
                }
            },
        },
        "fn": pace_trend,
    },
    {
        "name": "driver_report",
        "description": (
            "Everything known about one other car: last lap, best lap, race and "
            "class position, exact car model and class, gap, whether they are in "
            "the pits or off track, and their iRating and licence. Use for any "
            "question about a specific "
            "rival, including 'the car ahead' and 'the car behind'. Report "
            "iRating only if asked for it — it is a record of past results, not "
            "pace today."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Who to look up: a driver name or surname, a position "
                        "like 'P4', a car number like '#44', 'leader', 'ahead' "
                        "or 'behind'."
                    ),
                }
            },
            "required": ["target"],
        },
        "fn": driver_report,
    },
    {
        "name": "sector_report",
        "description": (
            "Which sector the driver is losing or gaining time in. With no "
            "target, compares their last lap to their own best in each "
            "sector; with one, to that car's last lap. Use for 'where am I "
            "losing it', 'which is my worst sector', and any sector "
            "comparison against a rival."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Optional. A driver name, a position like 'P4', a car "
                        "number, 'leader', 'ahead' or 'behind'. Omit to "
                        "compare against the driver's own best sectors."
                    ),
                }
            },
        },
        "fn": sector_report,
    },
    {
        "name": "stint_projection",
        "description": "Compare laps of fuel remaining against laps left in the race.",
        "parameters": {"type": "object", "properties": {}},
        "fn": stint_projection,
    },
    {
        "name": "session_status",
        "description": (
            "How much running is left in this session and what it is that "
            "runs out — a lap count, the clock, or whichever comes first. "
            "Use for 'how long have I got', 'how many laps left', 'how much "
            "time is left' and anything about how much of the session "
            "remains. In qualifying it also gives the lap allowance and the "
            "scoring format, because the session clock there is usually the "
            "window the driver may run in and NOT how long they may keep "
            "trying: never read time remaining as a qualifying budget "
            "yourself, ask this."
        ),
        "parameters": {"type": "object", "properties": {}},
        "fn": session_status,
    },
    {
        "name": "race_history",
        "description": (
            "How the race has gone: the grid position, the position now and "
            "how many places that is, the best lap and which lap it was, laps "
            "led, which laps were pit or scrappy laps, and how many cautions "
            "there have been. Use for 'how's my race going', 'how did I get "
            "here', 'where did I start', 'how has it gone' and for the "
            "post-race debrief. It does NOT know who overtook whom or why any "
            "position changed — do not ask it to explain a place, and never "
            "fill that in yourself."
        ),
        "parameters": {"type": "object", "properties": {}},
        "fn": race_history,
    },
]

TOOL_NAMES = [t["name"] for t in _TOOLS]


def tool_schemas() -> List[Dict[str, Any]]:
    """OpenAI-style tool definitions for the chat completions request."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in _TOOLS
    ]


def dispatch(name: str, arguments: Dict[str, Any],
             telemetry: Optional[TelemetrySnapshot]) -> Dict[str, Any]:
    """Run one tool call against the current snapshot.

    Never raises. A bad name, bad arguments or missing telemetry all come
    back as an unavailable result, because the model is free to call these
    with anything and a crash here would take the radio down mid-corner.
    """
    entry = next((t for t in _TOOLS if t["name"] == name), None)
    if entry is None:
        logger.warning("Model asked for an unknown calculation: %r", name)
        return _no("I can't work that one out.")
    if telemetry is None:
        return _no("I've lost data on the car — nothing I can tell you.")

    schema = entry["parameters"]
    args = {k: v for k, v in (arguments or {}).items()
            if k in schema.get("properties", {})}
    missing = [k for k in schema.get("required", []) if not args.get(k)]
    if missing:
        logger.info("%s called without %s", name, ", ".join(missing))
        return _no("I need to know which car you mean before I can answer that.")
    try:
        return entry["fn"](telemetry.car, telemetry.track, **args)
    except Exception:                             # noqa: BLE001 — see docstring
        # The driver gets a clean refusal; the traceback goes to the log,
        # because a silently swallowed bug here is one nobody ever finds.
        logger.exception("Calculation %s(%s) failed", name, args)
        return _no("I can't work that one out.")


def summary_lines(telemetry: Optional[TelemetrySnapshot]) -> List[str]:
    """Pre-computed strategy lines for the prompt block.

    Used when the model has no tool support: the same arithmetic, folded
    into the context instead of fetched on demand. Only the cheap, always
    relevant sums — the per-rival ones need an argument.
    """
    if telemetry is None:
        return []
    lines = []
    for label, result in (
        ("Fuel plan", fuel_plan(telemetry.car, telemetry.track)),
        ("Pit window", pit_window(telemetry.car, telemetry.track)),
    ):
        if result["available"]:
            lines.append(f"- {label}: {result['spoken']}")
    return lines
