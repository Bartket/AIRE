"""Prompt builder: assemble the LLM message list from question + telemetry.

This is the single place that decides how telemetry is described to the
model, so the wording stays consistent with the system prompt's "never
invent a number" rule.
"""

from typing import Dict, Iterable, List, Optional, Tuple

from . import race_math
from .telemetry import CarTelemetry, TelemetrySnapshot, TrackConfig, session_kind
from .text import question_words

# Appended after the (user-editable) system prompt. This is the output contract
# that must survive someone rewriting the persona in the UI, so it states only
# what the pipeline depends on — anything more duplicates the system prompt and
# dilutes it. The previous version repeated three rules verbatim and contradicted
# the system prompt on number formatting.
VALIDATION_PROMPT = (
    "Your reply is spoken aloud over team radio: one sentence, no markdown, "
    "no lists, no headings."
)

# Also appended after the editable persona, for the same reason: a call the
# driver hears as an order is one they act on without weighing it, and the
# engineer is the half of that conversation that cannot see the car. It got
# a driver into the pits for cosmetic damage with two laps to go and fuel to
# finish. Judgement is still wanted — stated as judgement.
ADVICE_PROMPT = (
    "Strategy is a recommendation, never an instruction. Say what you would do "
    "and why, in the same sentence — \"I'd stay out, that repair costs more than "
    "the damage\" — and leave the call with the driver. Never phrase it as an "
    "order and never imply there is only one option; they can see the car and "
    "you cannot.\n"
    "\n"
    "Race control is the exception. A black flag, a meatball, a drive-through or "
    "a disqualification is not your opinion — pass it on straight and plainly.\n"
    "\n"
    "Do not bring up pitting unless they asked or the numbers say it is needed. "
    "Damage the car is still running with is not a reason to stop on its own: "
    "late in a race the stop usually costs more than carrying the damage does."
)

# Once the flag is out the driver is on the slow-down lap with nothing to do
# but talk, and clipping the engineer to a single terse sentence there is what
# makes the whole thing feel like a database with a voice.
DEBRIEF_PROMPT = (
    "The session has finished. You are talking to your driver on the slow-down "
    "lap, so the one-sentence limit is lifted — but lifted is not a target. Two "
    "sentences is usually right. Three is the most that has ever been needed.\n"
    "\n"
    "The position in the telemetry is the FINAL position. Never report it as "
    "though the session were still running, and never mention gaps closing, cars "
    "ahead, or strategy to come. The telemetry block says what that position "
    "actually is — a finishing position, a grid slot, or nearly nothing after "
    "practice. Use its wording rather than guessing which session this was; "
    "calling a race result a grid slot is the kind of slip that tells a driver "
    "you are not really following.\n"
    "\n"
    "You still did not watch the race. You have the finishing numbers and "
    "nothing else — no replay, no memory of any lap. So react to the result, do "
    "not explain it. Say the result, say what you think of it, and stop.\n"
    "\n"
    "Do not pad. If there is little to say, say little: \"Good drive, that one. "
    "How did the car feel?\" is a complete answer. Never invent a reason for the "
    "result, never string together hedged guesses about what might have "
    "happened, and never justify your own opinion with made-up detail — that "
    "padding is what makes you sound like a machine rather than someone pleased "
    "for them.\n"
    "\n"
    "Warmth is yours to invent. Events are not. Be genuinely pleased about a "
    "good result, commiserate a bad one, and use their name."
)

# Added only when the model is actually offered the calculators. The rule is
# blunt because the failure it prevents is silent: a model that does the fuel
# sum in its head produces a confident, plausible, wrong number.
TOOLS_PROMPT = (
    "You have calculation tools for fuel, pit windows, gaps to a named car, "
    "the live trend to the adjacent cars, the running order, the fastest lap "
    "and pace comparisons, plus series-dependent pit, tyre-set, push-to-pass "
    "and joker-lap status. Call the tool "
    "instead of working any of that out yourself — never do arithmetic in your "
    "head.\n"
    "\n"
    "Call the tool BEFORE saying you do not have something. A refusal you could "
    "have answered is worse than no answer at all: the driver stops asking. If a "
    "question is about fuel, pit strategy, a rival, the order, lap times or how "
    "your pace is trending, the tool is the way to find out whether you know.\n"
    "\n"
    "For 'am I gaining', 'is the gap closing', or whether the car behind is "
    "catching, use adjacent_gap_trend. Never infer a live trend from earlier "
    "conversation gaps or from best laps, and never calculate a contact time.\n"
    "\n"
    "If a tool does come back unavailable, tell the driver you haven't got that "
    "one, in your own words and in character; never substitute an estimate, and "
    "never mention tools, calculations or missing data channels."
)


class PromptBuilder:
    """Build chat messages for the LLM."""

    def __init__(self, config):
        self.config = config

    def build_messages(
        self,
        question: str,
        telemetry: Optional[TelemetrySnapshot] = None,
        history: Iterable[Tuple[str, str]] = (),
        tools_enabled: bool = False,
    ) -> List[Dict[str, str]]:
        """System + recent turns + the current question with telemetry."""
        system = f"{self.config.system_prompt}\n\n{VALIDATION_PROMPT}\n\n{ADVICE_PROMPT}"
        if tools_enabled:
            system += f"\n\n{TOOLS_PROMPT}"
        if telemetry is not None and getattr(telemetry.track, "finished", False):
            system += f"\n\n{DEBRIEF_PROMPT}"
        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]

        for prev_q, prev_a in history:
            messages.append({"role": "user", "content": prev_q})
            messages.append({"role": "assistant", "content": prev_a})

        messages.append({
            "role": "user",
            "content": self.build_user_turn(question, telemetry, tools_enabled),
        })
        return messages

    def build_user_turn(self, question: str, telemetry: Optional[TelemetrySnapshot],
                        tools_enabled: bool = False) -> str:
        if telemetry is None:
            return f"No telemetry is available right now.\n\nQuestion: {question}"
        block = self.format_telemetry(telemetry.car, telemetry.track,
                                      self._callsign())
        focus = self._focus_prefixes(question)
        if focus is not None:
            block = self._focused_block(block, focus)
        # Without tools the model would have to do the strategy sums itself,
        # so hand it the answers already worked out.
        if not tools_enabled:
            precomputed = (race_math.summary_lines(telemetry)
                           if focus is None or any(word in question.lower()
                                                   for word in ("fuel", "pit", "strategy"))
                           else [])
            if precomputed:
                block += "\n" + "\n".join(precomputed)
        return f"Current telemetry:\n{block}\n\nQuestion: {question}"

    @staticmethod
    def _focus_prefixes(question: str) -> Optional[Tuple[str, ...]]:
        """A conservative compact view for unambiguous single-topic asks.

        Anything not recognised keeps the complete block. A missing line is
        more dangerous than a few extra tokens, so these groups are deliberately
        narrow and carry the session and car identity alongside the reading.
        """
        words = question_words(question)
        common = ("- You are talking", "- Your car:", "- Session:")
        if words & {"weather", "conditions", "rain", "wet", "temperature"} \
                and not words & {"engine", "oil", "water", "brake", "brakes",
                                 "tyre", "tyres", "tire", "tires"}:
            return common + ("- Track conditions:",)
        if words & {"tyre", "tyres", "tire", "tires"}:
            return common + ("- Tyre", "- Stint:", "- Setup:",
                             "- Track conditions:")
        if words & {"damage", "repair", "engine", "oil", "water", "brake",
                    "brakes", "warning", "health"}:
            return common + ("- Damage:", "- Car/pit state:", "- WARNING LIGHTS:",
                             "- Car location:", "- Pit box:", "- Fluid loss:",
                             "- Engine:", "- Brake")
        if words & {"lap", "laps", "pace", "sector", "sectors", "delta",
                    "faster", "slower"}:
            return common + ("- Remaining:", "- Fastest lap", "- Stint:",
                             "- Pace:", "- Best lap:")
        return None

    @staticmethod
    def _focused_block(block: str, prefixes: Tuple[str, ...]) -> str:
        chunks: List[List[str]] = []
        current: List[str] = []
        for line in block.splitlines():
            if line.startswith("- "):
                if current:
                    chunks.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            chunks.append(current)
        kept = ["\n".join(chunk) for chunk in chunks
                if any(chunk[0].startswith(prefix) for prefix in prefixes)]
        return "\n".join(kept)

    def build_prompt(self, question: str, telemetry: Optional[TelemetrySnapshot] = None) -> str:
        """Flat single-string prompt (kept for completion-style endpoints)."""
        parts = [self.config.system_prompt, "\n\n", VALIDATION_PROMPT, "\n\n",
                 ADVICE_PROMPT, "\n\n"]
        if telemetry is not None:
            parts.append("## Telemetry Data\n"
                         + self.format_telemetry(telemetry.car, telemetry.track,
                                                 self._callsign())
                         + "\n\n")
        parts.append(f"## Question\n{question}\n")
        return "".join(parts)

    def _callsign(self) -> str:
        """What the driver wants to be called, or "" to use the sim's name.

        Read through getattr with a default: a config.json written before
        this setting existed has no such key, and AppConfig raises
        AttributeError rather than returning None for a missing one.
        """
        return str(getattr(self.config, "driver_callsign", "") or "").strip()

    @staticmethod
    def format_telemetry(car: CarTelemetry, track: TrackConfig,
                         callsign: str = "") -> str:
        """Compact one-line-per-group summary of the car's current state.

        Anything the sim did not supply is written as "unknown" rather than
        as a number, so the engineer cannot read a missing channel back as
        zero laps of fuel or a negative lap time.
        """
        callsign = str(callsign or "").strip()
        header = [_driver(track, callsign)] if _driver(track, callsign) else []
        identity = _car_identity(track)
        if identity:
            header.append(identity)
        focus = _session_focus(track)
        if focus:
            header.append(focus)
        fmt = _qualifying_format(track)
        if fmt:
            header.append(fmt)
        if getattr(track, "finished", False):
            header.append(_result(car, track, callsign))

        return "\n".join(
            header
            + [
                f"- Session: {track.session_type or 'unknown'}{_state(track)}"
                f" | Track: {track.track_name or 'unknown'}"
                f"{_lap_counter(track)}"
                f" | Flag: {car.flag}",
                _remaining_line(car, track),
                _position_line(car, track)
                + f" | Speed: {_speed(car, car.speed)}"
                + f" | Gear: {car.gear or 'unknown'} | RPM: {car.rpm}",
                f"- Tyre tread remaining (%): FL {_num(car.tire_fl_wear, 0)}"
                f" | FR {_num(car.tire_fr_wear, 0)} | RL {_num(car.tire_rl_wear, 0)}"
                f" | RR {_num(car.tire_rr_wear, 0)}",
                # These are CARCASS temperatures — the only ones the SDK
                # publishes. They run far cooler than the surface temps the
                # in-game tyre box shows, so a bare number invited the model
                # to call 38C cold when that is normal for a carcass.
                # Carcass, not surface — the only temperature the SDK
                # publishes, and much cooler than the tyre box shows. The
                # normal range depends on car and compound and is not
                # published anywhere, so the honest position is that we
                # cannot say hot or cold at all. Saying "do not judge these
                # without a reference" was not enough: it still called 38C
                # cool. Forbid the words instead.
                f"- Tyre carcass temps: FL {_temp(car, car.tire_fl_temp)}"
                f" | FR {_temp(car, car.tire_fr_temp)} | RL {_temp(car, car.tire_rl_temp)}"
                f" | RR {_temp(car, car.tire_rr_temp)}",
                _tyre_caveat(car),
                f"- Tyre age (laps): FL {car.tire_fl_age} | FR {car.tire_fr_age}"
                f" | RL {car.tire_rl_age} | RR {car.tire_rr_age}",
                _brakes(car),
                _fuel_line(car),
                f"- {_neighbour_label(track, 'Ahead', car.ahead)}: {_rival(car.ahead, car)}",
                f"- {_neighbour_label(track, 'Behind', car.behind)}: {_rival(car.behind, car)}",
                f"- Fastest lap of the session: {_fastest(track.fastest_lap)}",
                _field_strength(track),
                f"- Running order ({track.field_size} cars, {_gap_meaning(track)}):"
                f" {_field(track.field)}",
                f"- Stint: {car.stint_laps} laps | Lap progress: {car.lap_progress * 100:.0f}%",
                f"- Pace: {_delta_line(car, track)}",
                f"- Strategy: {_strategy_line(car)}",
                f"- Best lap: {_laptime(car.best_lap_time)}"
                f" | Last lap: {_laptime(car.last_lap_time)}"
                f" | This lap so far: {_laptime(car.current_lap_time)}",
                _weather(car),
            ]
            + _fixed_setup(track)
            + _tyre_profile(car, track)
            # Car state lines are appended only when the sim actually supplies
            # them. Rendering "unknown" for every channel this build does not
            # publish would bury the readings that matter.
            + _car_state(car, track)
        )


UNKNOWN = "unknown"

# Every number that carries a unit says so. The model reads this block
# aloud, and an unlabelled figure is an invitation to pick a unit — air
# pressure came through as a bare "101325" before the SDK's Pascals were
# converted to hectopascals.
#
# The snapshot is metric motorsport throughout; imperial is applied here,
# where numbers become words. No calculator touches a temperature, a speed
# or a gap in metres, so converting at the edge keeps the arithmetic in one
# system permanently rather than hoping two agree.
#   metric   speed km/h · distance m · temperature C · wind km/h · hPa
#   imperial speed mph  · distance ft · temperature F · wind mph  · inHg
#   always   fuel in its own unit · time s · brake line pressure bar
#   sub-second differences in tenths and hundredths


def _is_imperial(car) -> bool:
    return str(getattr(car, "units", "metric") or "metric").lower() == "imperial"


def _temp(car, celsius, decimals=0) -> str:
    """A temperature with its unit, in whichever system the driver asked for."""
    if celsius is None:
        return "unknown"
    if _is_imperial(car):
        return f"{celsius * 9 / 5 + 32:.{decimals}f} F"
    return f"{celsius:.{decimals}f} C"


def _speed(car, kmh, decimals=0) -> str:
    if kmh is None:
        return "unknown"
    if _is_imperial(car):
        return f"{kmh * 0.621371:.{decimals}f} mph"
    return f"{kmh:.{decimals}f} km/h"


def _distance(car, metres, decimals=0) -> str:
    if metres is None:
        return "unknown"
    if _is_imperial(car):
        return f"{metres * 3.28084:.{decimals}f} feet"
    return f"{metres:.{decimals}f} metres"


def _pressure(car, hpa, decimals=0) -> str:
    """Air pressure. inHg wants two decimals — 29.92 is meaningful, 30 is not."""
    if hpa is None:
        return "unknown"
    if _is_imperial(car):
        return f"{hpa * 0.02952998:.2f} inHg"
    return f"{hpa:.{decimals}f} hPa"


def _rival(rival, car=None) -> str:
    """Name the car ahead/behind so the engineer can say who it is."""
    if rival is None or not rival.known:
        return "no car close"
    who = rival.describe()
    identity = []
    model = (getattr(rival, "car_model_short", "")
             or getattr(rival, "car_model", ""))
    if model:
        identity.append(model)
    car_class = getattr(rival, "car_class_name", "") or ""
    if car_class:
        identity.append(f"{car_class} class")
    if identity:
        who += f" [{', '.join(identity)}]"
    parts = []
    if rival.gap is not None:
        parts.append(f"{rival.gap:.2f} seconds")
    if rival.gap_metres is not None:
        parts.append(_distance(car, rival.gap_metres))
    where = getattr(rival, "track_surface", "")
    if where and where != "on track":
        parts.append(where)
    return f"{who} — {' / '.join(parts) if parts else 'gap unknown'}"


def _signed(value, unit: str = "s") -> str:
    """Deltas read wrong without an explicit sign — +0.3 down, -0.3 up."""
    if value is None:
        return UNKNOWN
    return f"{value:+.3f}{unit}"


def _delta_line(car, track) -> str:
    bits = []
    if car.delta_to_best is not None:
        bits.append(f"{_signed(car.delta_to_best)} vs your best (minus = faster)")
    if car.avg_lap_time is not None:
        bits.append(f"rolling average lap {_laptime(car.avg_lap_time)}")
    if car.delta_to_session_best is not None:
        bits.append(f"{_signed(car.delta_to_session_best)} vs session best")
    ahead_status = getattr(car, "closing_ahead_status", "waiting")
    ahead_change = getattr(car, "closing_ahead_change", None)
    ahead_segments = getattr(car, "closing_ahead_segments", 0)
    if ahead_status in ("early", "confirmed") and ahead_change is not None:
        verb = "closing on" if ahead_change > 0 else "losing to"
        subject = _traffic_subject(track, car.ahead, "the car ahead")
        if ahead_status == "early":
            bits.append(f"{verb} {subject} right now across {ahead_segments} "
                        f"mini-sector — QUALITATIVE EARLY READ, no numeric rate")
        else:
            bits.append(f"{verb} {subject}: timing gap changed "
                        f"{abs(ahead_change):.2f} seconds over {ahead_segments} "
                        f"mini-sectors")
    elif ahead_status == "steady" and car.ahead is not None:
        bits.append("gap to the car ahead holding roughly steady")
    elif ahead_status == "fluctuating" and car.ahead is not None:
        bits.append("gap to the car ahead fluctuating — no sustained direction")
    elif car.ahead is not None and car.ahead.gap is not None:
        # There is a car ahead and a gap to it, but not yet enough history
        # to say which way it is moving. Saying so is the difference between
        # "give me a few seconds" and the model inventing a reason — it
        # offered that the rival might be in the pits, which was not true
        # and was not something it had been told.
        bits.append("trend to the car ahead: still measuring comparable "
                    "mini-sectors. This needs three mini-sectors, NEVER laps")
    behind_status = getattr(car, "closing_behind_status", "waiting")
    behind_change = getattr(car, "closing_behind_change", None)
    behind_segments = getattr(car, "closing_behind_segments", 0)
    if behind_status in ("early", "confirmed") and behind_change is not None:
        verb = "catching you" if behind_change > 0 else "dropping back"
        subject = _traffic_subject(track, car.behind, "car behind")
        if behind_status == "early":
            bits.append(f"{subject} {verb} right now across {behind_segments} "
                        f"mini-sector — QUALITATIVE EARLY READ, no numeric rate")
        else:
            bits.append(f"{subject} {verb}: timing gap changed "
                        f"{abs(behind_change):.2f} seconds over {behind_segments} "
                        f"mini-sectors")
    elif behind_status == "steady" and car.behind is not None:
        bits.append("gap to the car behind holding roughly steady")
    elif behind_status == "fluctuating" and car.behind is not None:
        bits.append("gap to the car behind fluctuating — no sustained direction")
    return " | ".join(bits) if bits else UNKNOWN


def _strategy_line(car) -> str:
    bits = []
    if car.laps_to_go is not None:
        if getattr(car, "laps_to_go_is_estimate", False):
            # A timed race has no fixed lap count: iRacing recalculates it as
            # pace changes, so it moves by a lap either way. Saying "three
            # laps left" as a fact was wrong on track when it turned out to
            # be four.
            bits.append(f"about {car.laps_to_go:.0f} laps still to run — this is an "
                        f"ESTIMATE from the clock, not a fixed count, and can move by "
                        f"a lap; say so rather than stating it flatly")
        else:
            bits.append(f"{car.laps_to_go:.0f} laps still to run")
    needed = (getattr(car, "fuel_needed_conservative", None)
              if getattr(car, "fuel_needed_conservative", None) is not None
              else car.fuel_needed)
    margin = (getattr(car, "fuel_margin_conservative", None)
              if getattr(car, "fuel_margin_conservative", None) is not None
              else car.fuel_margin)
    best_margin = getattr(car, "fuel_margin_best", None)
    if needed is not None:
        bits.append(f"needs up to {needed:.1f} {car.fuel_unit} to finish from the "
                    f"highest recent clean-lap burn")
    if best_margin is not None and margin is not None and best_margin >= 0 > margin:
        bits.append(f"FUEL MARGINAL: observed burn range puts the finish between "
                    f"{best_margin:.1f} {car.fuel_unit} spare and "
                    f"{abs(margin):.1f} short — do not call it safe or require a stop")
    elif margin is not None:
        if margin >= 0:
            spare = f"{margin:.1f} {car.fuel_unit} spare in the high-burn case"
            # State the conclusion, not just the number. Given a bare margin
            # the engineer kept framing fuel as something to worry about even
            # with laps of it in hand — asked for advice it warned about
            # running out on a tank that finishes the race.
            burn = car.fuel_per_lap or 0
            if burn > 0 and margin >= burn:
                spare += (f" — {margin / burn:.1f} laps in hand, so fuel is"
                          f" NOT a concern and must not be raised as one")
            bits.append(spare)
        else:
            short = f"SHORT by {abs(margin):.1f} {car.fuel_unit} in the high-burn case"
            # The block hands over the margin, the burn and the laps to go, so
            # a save-per-lap figure is one division away — and the engineer
            # did it, telling a driver burning 3.5 a lap to save 3.1 of it.
            # Where the shortfall is past lifting and coasting there is no
            # saving figure to give, so say that here rather than leaving the
            # sum sitting there to be done.
            if race_math.save_per_lap(margin, car.laps_to_go or 0,
                                      car.fuel_per_lap or 0) is None:
                short += (". More than can be saved by lifting and coasting: do NOT"
                          " give a save-per-lap figure or work one out. Say the fuel"
                          " will not reach the flag, give the laps of fuel left against"
                          " the laps to run, and say a stop will be required. This does"
                          " NOT mean pit now — only the pit-window calculation decides"
                          " when to stop")
            bits.append(short)
    extra_margin = getattr(car, "fuel_extra_lap_margin", None)
    if extra_margin is not None:
        bits.append((f"an extra timed lap still leaves {extra_margin:.1f} "
                     f"{car.fuel_unit} spare") if extra_margin >= 0 else
                    (f"an extra timed lap is {abs(extra_margin):.1f} "
                     f"{car.fuel_unit} short"))
    return " | ".join(bits) if bits else UNKNOWN


def _brakes(car) -> str:
    """iRacing has no brake temperature channel — only line pressure.

    Worded without naming the sim: this text reaches the model, and the
    engineer was repeating it back to the driver on air.
    """
    if car.brake_pressure_fl is not None or car.brake_pressure_fr is not None:
        return (f"- Brake line pressure: front left {_num(car.brake_pressure_fl, 1)} bar"
                f" | front right {_num(car.brake_pressure_fr, 1)} bar"
                f" (no brake temperature reading available)")
    if car.brake_fl_temp is not None:
        return (f"- Brake temps: FL {_temp(car, car.brake_fl_temp)}"
                f" | FR {_num(car.brake_fr_temp, 0)} | RL {_num(car.brake_rl_temp, 0)}"
                f" | RR {_num(car.brake_rr_temp, 0)}")
    return "- Brake temperatures: no reading"


def _weather(car) -> str:
    """One conditions line, carrying only what the sim actually publishes.

    Labelled "Track conditions" rather than "Weather" because that is the
    question the driver actually asks, and asked it the engineer was
    refusing — it read the anti-forecast rule as covering conditions
    altogether and said it could not see them, while holding track
    temperature, air temperature and surface state.
    """
    bits = [f"track {_temp(car, car.track_surface_temp)}",
            f"air {_temp(car, car.air_temp)}"]
    if car.track_wetness:
        bits.append(f"surface {car.track_wetness}")
    if car.rain_intensity:
        bits.append(f"rain {car.rain_intensity:.2f}")
    if car.declared_wet:
        bits.append("DECLARED WET — wet tyres allowed")
    if car.skies:
        bits.append(car.skies)
    if car.wind_speed is not None:
        gust = f" from the {car.wind_direction}" if car.wind_direction else ""
        bits.append(f"wind {_speed(car, car.wind_speed)}{gust}")
    if car.humidity is not None:
        bits.append(f"humidity {car.humidity:.0f}%")
    if car.fog:
        bits.append(f"fog {car.fog:.0f}%")
    if car.air_pressure is not None:
        bits.append(f"air pressure {_pressure(car, car.air_pressure)}")
    if car.time_of_day:
        bits.append(f"track time {car.time_of_day}")
    return ("- Track conditions: " + " | ".join(bits)
            + " (these ARE the conditions — answer from them rather than saying"
              " you cannot see them; the only thing missing is a forecast, so"
              " what it will do later is the one part you have not got)")


def _driver(track, callsign: str = "") -> str:
    """Name the driver, so the engineer can talk to a person.

    A callsign, when set, is the only thing spoken. iRacing supplies a full
    legal name and the voice mangles some of them audibly — a name
    mispronounced on every answer is worse than no name at all. The real
    name stays in the block so the engineer can still confirm it if the
    driver actually asks, but it is never volunteered.
    """
    name = getattr(track, "driver_name", "") or ""
    if callsign:
        line = f"- You are talking to {callsign}"
        if name and name.lower() != callsign.lower():
            line += (f". Call them {callsign} and nothing else. Their full name "
                     f"is {name} — say it only if they ask for it directly, "
                     f"never in passing")
        return line + "."
    if not name:
        return ""
    bits = [f"- You are talking to {name}"]
    short = getattr(track, "driver_short_name", "") or ""
    if short and short != name:
        bits.append(f"known on timing screens as {short}")
    number = getattr(track, "driver_car_number", "") or ""
    if number:
        bits.append(f"driving car {number}")
    team = getattr(track, "team_name", "") or ""
    if team and team != name:
        bits.append(f"for {team}")
    return ", ".join(bits) + " — use their name."


def _car_identity(track) -> str:
    """The sim's own readable car model and class, never a local mapping."""
    model = (getattr(track, "car_model_short", "")
             or getattr(track, "car_model", ""))
    car_class = getattr(track, "car_class_name", "") or ""
    if not model and not car_class:
        return ""
    return (f"- Your car: {model or 'model unknown'}"
            f" | Class: {car_class or 'unknown'}")


def _result(car, track, callsign: str = "") -> str:
    """The finished session, stated as a result rather than a running commentary.

    Without this the engineer keeps calling positions after the flag, because
    every other line in the block reads like a live update.

    Which session finished is part of the result. Qualifying ending announced
    itself as "RACE OVER — finished P2" for a driver with the race still to
    come, and told the model the lap it was comparing against was the fastest
    lap of a race nobody had started.
    """
    kind = session_kind(getattr(track, "session_type", ""))
    what = {"race": "RACE OVER", "qualify": "QUALIFYING OVER"}.get(kind, "SESSION OVER")
    verb = "qualified" if kind == "qualify" else "finished"
    who = callsign or track.driver_name or "your driver"
    if not car.position:
        # Same rule as the live line: no classification is not a place.
        return (f"- {what} — iRacing published no position for {who}. Say you "
                f"haven't got the result, and do not work one out.")
    bits = [f"{what} — {who} {verb} P{car.position}"]
    if car.class_position and car.class_position != car.position:
        bits.append(f"P{car.class_position} in class")
    if track.field_size:
        bits.append(f"out of {track.field_size} cars")
    if car.best_lap_time:
        bits.append(f"best lap {_laptime(car.best_lap_time)}")
        fastest = track.fastest_lap
        if fastest is not None and fastest.known and fastest.best_lap:
            delta = car.best_lap_time - fastest.best_lap
            whose = "race" if kind == "race" else "session"
            if delta <= 0.001:
                bits.append(f"which was the fastest lap of the {whose}")
            else:
                holder = fastest.name or f"car {fastest.car_number}"
                bits.append(
                    f"{race_math._spoken_delta(delta)} off the fastest lap of the {whose} "
                    f"({holder}, {_laptime(fastest.best_lap)}) — use this figure, do not "
                    f"work the difference out yourself"
                )
    if car.incidents is not None:
        bits.append(f"{car.incidents} incident points")
    return "- " + ", ".join(bits) + "."


def _position_line(car, track) -> str:
    """The driver's place, or words where the sim has not given them one.

    iRacing publishes 0 for a car it has not classified — before a first
    timed lap, or when the server is not transmitting that car — and the
    old version rendered it as "P0". That put a place-shaped hole three
    lines above a running order that listed the driver second, and asked
    where they were the engineer answered pole: the same failure as the
    fuel line, where a blank in a number's slot got filled from whatever
    number was nearest.

    "unknown" is safe for a reading. It is not safe in a slot the block
    itself supplies a plausible answer for, so this says outright that the
    place is missing and that the standings below are not a substitute.
    """
    label = "Finishing position" if getattr(track, "finished", False) else "Position"
    if not car.position:
        return (f"- {label}: {UNKNOWN} — iRacing has not classified this car, so "
                f"there is no position to give. Say you haven't got it. Do NOT "
                f"count the running order below to work one out")
    line = f"- {label}: P{car.position}"
    if car.class_position and car.class_position != car.position:
        line += f" (P{car.class_position} in class)"
    return line


def _fuel_line(car) -> str:
    """Fuel, with no number-shaped hole where a measurement is missing.

    The burn average needs a couple of completed laps before it exists, and
    until it does there is no laps-of-fuel figure. Writing that as "~unknown
    laps of fuel left" put a lap-shaped blank three lines under "Remaining: 2
    laps", and asked how much fuel was left the engineer filled the blank with
    the lap count and reported laps to the flag as fuel range — twice the
    driver's actual reserve, spoken with no hedge. "unknown" is safe for a
    reading; it is not safe in a slot the block itself supplies a plausible
    number for.

    So a clause with nothing behind it is not written at all, and the absence
    is stated as the fact it is.
    """
    bits = [f"- Fuel: {car.fuel_level_pct:.0f}% of tank"
            + (f" ({_num(car.fuel_level, 1)} {car.fuel_unit})"
               if car.fuel_level is not None else "")]
    if car.fuel_per_lap is not None:
        bits.append(f"burn {_num(car.fuel_per_lap, 2)} {car.fuel_unit} per racing lap"
                    + (f" (last lap actually {_num(car.fuel_last_lap, 2)}"
                       + ("; NOT a clean racing lap, so do not use it to predict range"
                          if getattr(car, "fuel_last_lap_clean", None) is False else "")
                       + ")"
                       if car.fuel_last_lap is not None else ""))
        burn_low = getattr(car, "fuel_burn_min", None)
        burn_high = getattr(car, "fuel_burn_max", None)
        burn_samples = getattr(car, "fuel_burn_samples", 0)
        if burn_low is not None and burn_high is not None:
            bits.append(f"{burn_samples} clean-lap samples span "
                        f"{_num(burn_low, 2)} to {_num(burn_high, 2)} {car.fuel_unit}")
    if car.fuel_used_stint:
        bits.append(f"{_num(car.fuel_used_stint, 1)} used this stint")
    if car.fuel_remaining_laps is not None:
        bits.append(f"~{_num(car.fuel_remaining_laps, 1)} laps of fuel left")
    else:
        bits.append("LAPS OF FUEL LEFT: NOT MEASURED YET — no such figure exists."
                    " Say you cannot call the fuel yet. Do not estimate one, and"
                    " do not read the laps remaining in this block as fuel range")
    return " | ".join(bits)


def _tyre_caveat(car) -> str:
    """Say what the tyre numbers actually are before the model reads them.

    Two separate traps. They are carcass temperatures, not the surface temps
    the in-car tyre box shows, and much cooler — a bare 38 got called "cold"
    on air. And iRacing restricts live tyre telemetry to in-car dashboards,
    so for a third-party reader they can stop updating once the car leaves
    the pits, leaving last-stop values that look current.
    """
    if not getattr(car, "tyre_data_live", True):
        return ("  (STALE: these tyre readings have stopped updating. iRacing only"
                " refreshes tyre telemetry for outside apps in the pits, so these are"
                " the values from the last stop, not now. Say so if asked about tyres,"
                " and do not describe them as current.)")
    return ("  (CARCASS temperatures, the only ones the sim publishes, and far cooler"
            " than the surface temps in the in-car tyre box. What counts as normal"
            " depends on car and compound and is not published, so you CANNOT tell"
            " whether these are hot or cold — never call them cold, cool, warm, hot or"
            " up to temperature. Report the numbers and the spread, nothing more.)")


def _fixed_setup(track) -> list:
    """Say when the car cannot be changed, because plenty of series say so.

    Suppressing the camber and pressure calls is not enough on its own: the
    driver can still ask "what should I change?", and without this the
    engineer answers as though the garage were open.
    """
    if not getattr(track, "fixed_setup", False):
        return []
    return ["- Setup: FIXED by the series. Nothing on the car can be changed —"
            " not camber, not pressures, not wing, not dampers. Never suggest a"
            " setup change; if asked what to change, say the setup is fixed and"
            " talk about driving instead."]


def _tyre_profile(car, track=None) -> list:
    """Across-the-tread temperatures, and what an uneven one means.

    Only emitted for corners actually running uneven. A tyre with an even
    profile needs no comment, and listing all four every lap would bury the
    one that has a problem.

    Emitted at all only when the tyre feed is still updating. iRacing stops
    refreshing tyre telemetry for outside apps once the car leaves the pits,
    so what is left describes the last stop — and a camber call read off a
    profile taken on pit exit is a setup change argued from the out-lap.
    That happened in a live practice session.
    """
    if not getattr(car, "tyre_data_live", True):
        return []

    # A fixed setup makes the cause true and the remedy unavailable. Naming
    # camber then sends the driver hunting for a control that is locked.
    fixed = bool(getattr(track, "fixed_setup", False))
    corners = (("front left", car.tyre_fl), ("front right", car.tyre_fr),
               ("rear left", car.tyre_rl), ("rear right", car.tyre_rr))
    lines = []
    for label, tyre in corners:
        if tyre is None:
            continue
        problem = tyre.diagnosis(setup_advice=not fixed)
        if not problem:
            continue
        lines.append(
            f"- Tyre profile {label}: inner {_temp(car, tyre.inner_temp)}"
            f" | middle {_num(tyre.middle_temp, 0)}"
            f" | outer {_num(tyre.outer_temp, 0)} — {problem}"
        )
    return lines


def _incidents(car) -> list:
    """The count, and how close to the limit only when there is a limit.

    The cap is a per-session setting — 17 in one series, 25 in another,
    "unlimited" in plenty of sessions, where the reader gives None. Stated
    as a bare count it left the model to supply the limit from somewhere
    else, and a driver kept hearing "one more and you're out" against a
    number nobody had published. The margin is subtracted here too: two
    numbers side by side is an invitation to do arithmetic.
    """
    if car.incidents is None:
        return []

    team = getattr(car, "team_incidents", None)
    is_team = team is not None and team != car.incidents
    # In a team race the team total is what runs against the limit, so the
    # driver's own count answers a different question from the one that
    # ends the race.
    counted = team if is_team else car.incidents
    who = (f"you have {car.incidents} and the TEAM has {team} — the TEAM total is "
           f"what runs against the limit, their own is what they have picked up"
           if is_team else f"{car.incidents}")

    limit = car.incident_limit
    if not limit:
        margin = ("No limit is published for this session, so you do NOT know the "
                  "cap: never name one, never say how many they have left, and "
                  "never tell them one more puts them out. Telling them to keep it "
                  "clean is as far as you can go.")
    elif counted >= limit:
        margin = (f"The limit is {limit} and they are at or past it — that is the "
                  f"session gone; say so straight.")
    else:
        left = limit - counted
        margin = (f"The limit is {limit}, which leaves "
                  f"{race_math._plural(left, 'point')} in hand — use that figure, "
                  f"never work it out yourself.")

    return [f"- Incident points: {who}. {margin}"]


def _repairs(car, track) -> list:
    """Damage put as a cost to weigh, not as an instruction to pit.

    `PitRepairLeft` is the repair time iRacing will force on you once you
    stop; it is not an order to stop. Written out as "mandatory repairs
    outstanding" it was read as one, and the engineer sent a driver in with
    two laps left, fuel in the tank and damage costing less than the stop.
    Only a meatball flag actually compels a stop, and that arrives on the
    penalty channel — so both sides of the trade are stated here, with the
    race that is left to win it back measured rather than left implied.
    """
    if not car.repair_required and not car.repair_optional:
        return []

    cost = []
    if car.repair_required:
        cost.append(f"{car.repair_required:.0f}s of repairs iRacing will force on "
                    f"you the moment you stop")
    if car.repair_optional:
        cost.append(f"{car.repair_optional:.0f}s of optional repairs on top")
    line = "- Damage: " + ", ".join(cost) + "."

    if car.penalty and "repair" in car.penalty:
        # A meatball is race control ordering the car in. Nothing to weigh.
        return [line + " Race control has ORDERED you in — that stop is not optional,"
                       " tell them to come in."]

    if not race_math._is_race(track):
        # Outside a race there is no finish to reach and no position to give
        # up, so there is nothing to weigh — the stop costs only its own time.
        return [line + " Nothing is ordering you in, and outside a race a stop"
                       " costs only the time it takes."]

    laps = race_math._laps_to_go(car, track)
    lap = car.avg_lap_time or car.last_lap_time or car.best_lap_time
    weigh = ("Weigh that repair time, plus the pit lane and the places a stop gives"
             " away, against ")
    if laps is not None and lap:
        weigh += (f"about {race_math._plural(laps, 'lap')} left, roughly "
                  f"{_clock(laps * lap)} of racing.")
    elif laps is not None:
        weigh += f"about {race_math._plural(laps, 'lap')} left."
    else:
        weigh = ("How much of the race is left is not something you have, so you"
                 " cannot say whether that stop pays for itself — say so.")

    return [line + f" No meatball flag, so nothing is ordering you in and the car is"
                   f" still running. {weigh} Damage on its own is NOT a reason to pit."]


def _car_state(car, track) -> list:
    """Incidents, damage and engine health — emitted only when present."""
    lines = _incidents(car) + _repairs(car, track)

    damage = []
    # Before damage: a stop-and-go and a slow-down need different driving,
    # and the flag line alone cannot tell them apart.
    if car.penalty:
        damage.append(f"PENALTY — {car.penalty}")
    if car.tow_time:
        damage.append(f"BEING TOWED, {car.tow_time:.0f}s — the car is out")
    if car.fast_repair_available is not None:
        used = f", {car.fast_repair_used} used" if car.fast_repair_used is not None else ""
        damage.append(f"fast repairs available: {car.fast_repair_available}{used}")
    if car.in_pit_stall:
        damage.append("stopped in the pit box")
    elif car.on_pit_road:
        damage.append("on pit road")
    if car.pit_service_pending:
        damage.append("pit service is armed")
    if damage:
        lines.append("- Car/pit state: " + " | ".join(damage))

    if car.warnings:
        lines.append("- WARNING LIGHTS: " + ", ".join(car.warnings))
    if car.track_surface and car.track_surface not in ("on track",):
        lines.append(f"- Car location: {car.track_surface}")
    if car.pit_service_status and car.pit_service_status != "none":
        lines.append(f"- Pit box: {car.pit_service_status}")

    # A level that falls is a leak. A temperature that climbs only says
    # something is wrong.
    fluids = []
    if car.oil_level_drop:
        fluids.append(f"OIL DOWN {car.oil_level_drop:.2f} litres since the start")
    if car.water_level_drop:
        fluids.append(f"COOLANT DOWN {car.water_level_drop:.2f} litres since the start")
    if fluids:
        lines.append("- Fluid loss: " + " | ".join(fluids)
                     + " — that is a leak, not consumption")

    engine = []
    # Temperatures are rendered through _temp so they follow the driver's
    # choice; the rest carry units that do not change between systems.
    if car.water_temp is not None:
        engine_temps = [f"water temp {_temp(car, car.water_temp)}"]
    else:
        engine_temps = []
    if car.oil_temp is not None:
        engine_temps.append(f"oil temp {_temp(car, car.oil_temp)}")
    for label, value, unit in (
        ("oil pressure", car.oil_pressure, " bar"), ("fuel pressure", car.fuel_pressure, " bar"),
        ("voltage", car.voltage, " volts"),
        ("oil level", car.oil_level, " litres"),
        ("coolant level", car.water_level, " litres"),
    ):
        if value is not None:
            engine.append(f"{label} {value:.1f}{unit}".rstrip())
    # Temperatures first — they are what the driver asks about.
    engine = engine_temps + engine
    if engine:
        lines.append("- Engine: " + " | ".join(engine))

    return lines


def _field_strength(track) -> str:
    """Where the driver sits in the field on paper, with the caveats.

    Three ways this misleads if stated bare. iRating is a result of past
    races, not pace today — the engineer must not turn it into "he is
    faster than you". AI opponents have no real rating, so a field of them
    is no data rather than a weak field. And a field where nobody has a
    rating yet is not a field of beginners.
    """
    field = getattr(track, "field", None) or []
    rated = [r for r in field if getattr(r, "irating", None)]
    ai = [r for r in field if getattr(r, "is_ai", False)]

    if ai and not rated:
        return ("- Field strength: these are AI opponents, so there is no iRating "
                "for anyone including comparisons — say you have no rating data "
                "rather than guessing at the field's strength.")
    if len(rated) < 3:
        return ""

    me = next((r for r in field if r.is_player), None)
    mine = getattr(me, "irating", None) if me else None
    ratings = sorted((r.irating for r in rated), reverse=True)
    average = round(sum(ratings) / len(ratings))

    bits = [f"{len(rated)} of {len(field)} cars rated",
            f"field average {average}"]
    if mine:
        rank = sum(1 for r in ratings if r > mine) + 1
        bits.append(f"you are {mine}, {rank} of {len(ratings)} on rating")
    if ai:
        bits.append(f"{len(ai)} AI cars excluded, they have no real rating")

    return ("- Field strength (iRating): " + " | ".join(bits)
            + ". This is a record of past results, NOT pace today — never say a "
              "driver is faster or slower than the driver because of it, and "
              "never predict a race result from it.")


def _fastest(rival) -> str:
    """Whoever holds the session's best lap."""
    if rival is None or not rival.known:
        return UNKNOWN
    return f"{rival.describe()} on {_laptime(rival.best_lap)}"


def _field(field) -> str:
    """Compact classified order, so the engineer can talk about the whole
    field rather than only the two cars either side."""
    if not field:
        return UNKNOWN
    parts = []
    class_ids = {getattr(r, "car_class_id", None) for r in field
                 if getattr(r, "car_class_id", None) is not None}
    multiclass = len(class_ids) > 1
    for r in field[:10]:
        who = r.name or (f"car {r.car_number}" if r.car_number else "?")
        # position is None for somebody in the session who has not been
        # classified yet — normal in practice, where a driver who has set
        # no lap has no slot on the timing screen.
        bits = [f"P{r.position} {who}" if r.position else f"{who} (no time yet)"]
        if multiclass and getattr(r, "car_class_name", ""):
            bits.append(f"[{r.car_class_name} class]")
        if r.position != 1 and r.gap:
            bits.append(f"+{r.gap:.1f}s")
        if r.laps_down < 0:
            bits.append(f"{-r.laps_down} lap(s) up")
        elif r.laps_down > 0:
            bits.append(f"{r.laps_down} lap(s) down")
        # In practice people join and leave constantly and iRacing keeps
        # their times, and in lone qualifying everyone runs separately, so
        # the order is full of cars that are not out there. Left unmarked
        # the engineer talks about them as rivals on track.
        if not getattr(r, "in_world", True):
            bits.append("(not on track, time from earlier)")
        elif r.in_pits:
            bits.append("in pits")
        parts.append(" ".join(bits))
    tail = f"; +{len(field) - 10} more" if len(field) > 10 else ""
    return "; ".join(parts) + tail


def _gap_meaning(track) -> str:
    """What the + figures in the running order actually are.

    They are not the same quantity in every session. In a race they are
    time behind the leader; in practice and qualifying nobody is chasing
    anybody, and the number that matters is how far off the best lap you
    are. Saying which one it is costs a few words and stops the engineer
    turning a lap-time deficit into a race gap.
    """
    if session_kind(getattr(track, "session_type", "")) == "race":
        return "+ figures are seconds behind the leader"
    return ("+ figures are seconds off the SESSION BEST LAP, not a gap on track"
            " — nobody is chasing anybody in this session")


def _neighbour_label(track, word: str, rival=None) -> str:
    """Name the cars either side for what they are in this session.

    In a race the car ahead is the one to catch. In practice and
    qualifying it is traffic — worth knowing about, because it decides
    whether the next lap is a clear one, but it is not a rival for
    position and must not be talked about as one.
    """
    if session_kind(getattr(track, "session_type", "")) == "race":
        context = _traffic_context(track, rival)
        return f"{word} on track ({context}, not a rival for position)" if context else word
    return f"{word} on track (traffic, not a rival for position)"


def _traffic_context(track, rival) -> str:
    if rival is None:
        return ""
    my_class = getattr(track, "car_class_id", None)
    their_class = getattr(rival, "car_class_id", None)
    if my_class is not None and their_class is not None and my_class != their_class:
        mine = getattr(track, "car_class_est_lap_time", None)
        theirs = getattr(rival, "class_est_lap_time", None)
        if mine and theirs:
            if theirs < mine:
                return "faster-class traffic"
            if theirs > mine:
                return "slower-class traffic"
        return "different-class traffic"
    if my_class is not None and their_class == my_class:
        if getattr(rival, "laps_down", 0) > 0:
            return "lapped car"
        if getattr(rival, "laps_down", 0) < 0:
            return "car lapping you"
    return ""


def _traffic_subject(track, rival, fallback: str) -> str:
    context = _traffic_context(track, rival)
    return f"{context} ({fallback}, not for position)" if context else fallback


def _session_focus(track) -> str:
    """What this session is actually for.

    The block otherwise reads identically in practice and in a race, and the
    engineer answered a qualifying question about pit strategy as though
    there were a finish to reach. What the driver needs is different in each.
    """
    kind = session_kind(getattr(track, "session_type", ""))
    if kind == "qualify":
        return ("- THIS IS QUALIFYING: one lap time is all that matters. Talk about the "
                "driver's best lap against the session best, where they sit in the "
                "order, and tyre temperature for the next flying lap. How much running "
                "they have left is the session_status calculation, never the session "
                "clock read off this block. There is no race to finish, so no strategy, no fuel to "
                "the end, and no pit window. Nobody is chasing anybody: every gap here "
                "is a difference in lap time, never a car to catch, and qualifying is "
                "often run alone, so most of the order will not be on track with the "
                "driver at all. Asked about the field, give awareness — the time to "
                "beat and where they stand — not a rival to hunt down.")
    if kind == "practice":
        return ("- THIS IS PRACTICE: nothing is at stake, so help them go faster. Talk "
                "about lap time consistency, whether the tyres are coming to them or "
                "going away, what the temperatures suggest about the setup, and how "
                "much session time is left. Position is by lap time, not by racing, "
                "and there is no finish to reach.")
    return ""


def _qualifying_format(track) -> str:
    """How this qualifying session is run, from what iRacing published.

    The engineer took a qualifying session for a stretch of time to keep
    trying in, because a session clock is what the block looked like it was
    offering. It is normally an allowance of a lap or two inside a window,
    and the shape of it differs by series — so this states the shape rather
    than describing the common case and hoping.

    Every part is published per session: Lone against Open Qualify,
    SessionLaps for the allowance, and the scoring format. Where iRacing
    published nothing, this says nothing. An assumed format spoken with
    confidence is the failure being fixed, not a smaller version of it.
    """
    if session_kind(getattr(track, "session_type", "")) != "qualify":
        return ""
    if getattr(track, "finished", False):
        # The allowance is spent and the result is in. Still describing the
        # budget here would have the engineer telling a driver what they may
        # do with laps they no longer have.
        return ""

    bits = []
    solo = getattr(track, "solo_qualifying", None)
    if solo is True:
        bits.append("the driver is out alone — no tow, and nobody to follow")
    elif solo is False:
        bits.append("the whole field is out together")

    allowance = track.laps_total
    if allowance:
        bits.append(
            f"iRacing has given them {race_math._plural(allowance, 'lap')} in this "
            f"session. That allowance is the budget. The session clock is only the "
            f"window it has to be spent in, and it is NOT how long they may keep "
            f"trying — typically it is an out lap and a flier or two, then they are "
            f"done however much time is showing")
    else:
        bits.append(
            "iRacing published no lap allowance, so the clock is what limits the "
            "running here and they may keep going until it expires")

    scoring = race_math._scoring_phrase(track)
    if scoring:
        bits.append(f"the result is {scoring}")

    return "- QUALIFYING FORMAT: " + "; ".join(bits) + "."


def _lap_counter(track) -> str:
    """Lap N of M, where M is a race distance and not something else.

    SessionLaps in a race is the distance to cover. In qualifying it is a
    lap allowance, and rendering the two identically produced "Lap 3 of 2"
    for a driver on their out lap plus two fliers — which reads as having
    overrun a race. The allowance belongs on the remaining line, where it
    is labelled as one.
    """
    if session_kind(getattr(track, "session_type", "")) == "race":
        return f" | Lap {track.current_lap}{_of(track.laps_total)}"
    return f" | Lap {track.current_lap}"


def _remaining_line(car, track) -> str:
    """Laps and clock, plus which of the two actually ends the running.

    Both were printed with nothing said about their relationship, and the
    engineer picked whichever it read first — which in qualifying meant
    offering eight more minutes to a driver who had two laps. Deciding
    between them is arithmetic over a lap time, so race_math.session_limit()
    does it once, in Python, and both this line and the session_status
    calculation report the same verdict.
    """
    line = (f"- Remaining: {_laps(track.laps_remaining)}"
            f" | Time left: {_clock(track.time_remaining)}")
    verdict = {
        "laps": "LAPS are what run out first here — the clock is not the limit",
        "clock": "the CLOCK is what runs out first here",
        "both": "laps and clock are close; either could end it first",
    }.get(race_math.session_limit(car, track)["binds"])
    return line if verdict is None else f"{line} | {verdict}"


def _state(track) -> str:
    """Say when the session has actually ended.

    Without this the engineer had no way to know the race was over: the flag
    goes checkered for one lap and the clock simply stops.
    """
    state = (getattr(track, "session_state", "") or "").lower()
    kind = session_kind(getattr(track, "session_type", ""))
    if state in ("checkered", "cooldown"):
        what = {"race": "race", "qualify": "qualifying"}.get(kind, "session")
        return f" — SESSION FINISHED, the {what} is over"
    if state == "paradelaps":
        return " — formation lap, race has not started"
    if state == "warmup":
        return " — warmup, race has not started"
    return ""


def _num(value, places: int = 1) -> str:
    return UNKNOWN if value is None else f"{value:.{places}f}"


def _laptime(value) -> str:
    """Lap times as minutes:seconds.

    iRacing sends raw seconds (112.188). Passing that through made the model
    do the mm:ss conversion itself, and it got it wrong — 112.188 came back
    as "1:12.188" instead of 1:52.188. Doing the arithmetic here removes the
    chance to fumble it.
    """
    if value is None:
        return UNKNOWN
    if value < 60:
        return f"{value:.3f}s"
    minutes, seconds = divmod(float(value), 60)
    return f"{int(minutes)}:{seconds:06.3f}"


def _of(total) -> str:
    return "" if total is None else f" of {total}"


def _laps(remaining) -> str:
    """Laps left, or an explicit statement that the session is timed."""
    if remaining is None:
        return "not lap-limited (timed session)"
    return f"{remaining} laps"


def _clock(seconds) -> str:
    """Human-readable session time remaining."""
    if seconds is None:
        return "no time limit"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
