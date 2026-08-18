"""Telemetry abstraction layer.

V1: iRacing via irsdk.
Future: swap in SimHub Property Server or other backend.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("race-engineer")


# ── Data classes (interface types) ──────────────────────────────────

@dataclass
class TrackConfig:
    current_lap: int = 0
    # Laps completed in the race specifically. current_lap counts from the
    # session start, which is not the same thing after a practice start.
    race_laps: Optional[int] = None
    # None means "not lap-limited or not known yet" — distinct from 0, which
    # would read as "the race is over".
    laps_remaining: Optional[int] = None
    laps_total: Optional[int] = None
    track_name: str = ""
    session_type: str = ""                    # Race / Practice / Qualify
    # How this qualifying session is actually run, straight from the session
    # YAML rather than assumed. iRacing publishes "Lone Qualify" (out alone,
    # a fixed lap allowance) and "Open Qualify" (everyone out together, the
    # clock is the budget) as distinct session types, and session_kind()
    # deliberately collapses both to "qualify" — these keep the difference
    # the engineer needs, which is what ends the driver's run.
    #
    # None / "" where iRacing published nothing, which is a refusal: the
    # format is then unknown, not assumed to be the common one.
    solo_qualifying: Optional[bool] = None
    qualify_scoring: str = ""                 # "best lap", or an averaged format
    laps_to_average: Optional[int] = None     # SessionNumLapsToAvg
    # Which segment of the weekend is running. Practice, qualifying and the
    # race are separate sessions under one SubSessionID, and the lap history
    # spans all of them — this is what tells them apart.
    session_num: Optional[int] = None
    # Weekend identity for scoping conversational memory. The reader's
    # generation also changes when an offline run restarts with zero IDs.
    session_id: Optional[int] = None
    subsession_id: Optional[int] = None
    session_generation: int = 0
    # Racing / Checkered / CoolDown / ParadeLaps / Warmup — the only way to
    # know the session has actually ended.
    session_state: str = ""
    time_remaining: Optional[float] = None    # seconds; None if untimed
    time_total: Optional[float] = None
    # Completed laps this session, oldest first. Trend work lives in
    # race_math; this is only the record.
    laps: List["LapRecord"] = field(default_factory=list)
    # Classified running order, leader first. Lets the engineer answer about
    # the field, not just the two cars either side.
    # NOTE: this attribute shadows dataclasses.field for the rest of the class
    # body, so any later default_factory must be declared above this line.
    field: List["Rival"] = field(default_factory=list)
    field_size: int = 0
    fastest_lap: Optional["Rival"] = None     # whoever holds the session best
    # Who is driving. An engineer who cannot use the driver's name never
    # sounds like a real one.
    driver_name: str = ""
    driver_short_name: str = ""
    driver_car_number: str = ""
    team_name: str = ""
    # Published by iRacing for the car, not inferred from its name. The IDs
    # are the stable association; the strings are what may be spoken.
    car_class_id: Optional[int] = None
    car_class_name: str = ""
    # iRacing's estimated lap time for this class. This is the authoritative
    # way to say whether different-class traffic is from a faster or slower
    # class; observed driver lap times are not a class definition.
    car_class_est_lap_time: Optional[float] = None
    car_id: Optional[int] = None
    car_model: str = ""
    car_model_short: str = ""
    car_path: str = ""
    # Where the race started from. Not derivable from the lap history: the
    # earliest LapRecord holds the position at the END of lap one, which
    # after a first-lap scrap is several places from the grid slot — and the
    # grid slot is what the driver is asking about. None means no qualifying
    # result was published and nothing was latched at green; that is a
    # refusal, never a reason to substitute lap one.
    grid_position: Optional[int] = None
    grid_class_position: Optional[int] = None
    # Published session results, for the segment currently running. None
    # means iRacing has not filled them in — distinct from 0, which for
    # laps led is the fact that the driver led none.
    laps_led: Optional[int] = None
    caution_flags: Optional[int] = None
    caution_laps: Optional[int] = None
    lead_changes: Optional[int] = None
    # True once the flag is out. Position then means finishing position, and
    # the engineer should be debriefing rather than calling the race.
    finished: bool = False
    # The series has locked the setup, so nothing about the car can be
    # changed. Advice to take camber out or drop a pressure is not advice
    # there — it is an instruction to go looking for a setting that is
    # greyed out, at 200 km/h.
    fixed_setup: bool = False


@dataclass
class Rival:
    """A car racing near you, with enough identity to name on the radio."""

    name: str = ""            # driver name
    short_name: str = ""      # abbreviated, nicer for TTS
    car_number: str = ""
    car_class_id: Optional[int] = None
    car_class_name: str = ""
    class_est_lap_time: Optional[float] = None
    car_id: Optional[int] = None
    car_model: str = ""
    car_model_short: str = ""
    car_path: str = ""
    gap: Optional[float] = None   # seconds; None when not measurable
    gap_metres: Optional[float] = None
    # Separate timing gap used for movement trends.  The displayed gap may
    # come from CarDistAhead divided by current speed, which is a good
    # snapshot but changes under braking even when neither car gains.  This
    # value comes from CarIdxEstTime and is sampled only at comparable points
    # around the lap.
    trend_gap: Optional[float] = None
    position: Optional[int] = None
    laps_down: int = 0        # negative = they are a lap ahead
    best_lap: Optional[float] = None
    # Their most recent lap, which answers "is he quick right now?" where
    # best_lap answers "was he ever quick?". CrewChief exposes both because
    # a rival two seconds off his own best is a rival in trouble.
    last_lap: Optional[float] = None
    tyre_compound: Optional[int] = None
    # Their last completed lap, split by sector.
    sector_times: List[Optional[float]] = field(default_factory=list)
    in_pits: bool = False
    # Richer than in_pits: on track, off track, approaching pits, in the
    # pit stall. And their position within their own class.
    track_surface: str = ""
    class_position: Optional[int] = None
    # iRating measures results over time, not pace today, and is None for AI
    # opponents because theirs describes a difficulty setting rather than a
    # driver.
    irating: Optional[int] = None
    licence: str = ""
    is_ai: bool = False
    push_to_pass_count: Optional[int] = None
    push_to_pass_active: Optional[bool] = None
    # Whether they are actually in the session right now. In practice people
    # join and leave constantly, and iRacing keeps their lap times in the
    # classification after they have gone — so a rival can hold P3 on a time
    # set an hour ago from a garage they have already left. Comparing
    # against them is not wrong, but calling them a car you are racing is.
    in_world: bool = True
    # Marks your own entry in the running order, so gaps can be measured
    # against it without guessing which row is you.
    is_player: bool = False

    @property
    def known(self) -> bool:
        return bool(self.name or self.car_number)

    def describe(self) -> str:
        who = self.name or (f"car {self.car_number}" if self.car_number else "unknown car")
        if self.car_number and self.name:
            who = f"{self.name} (car {self.car_number})"
        if self.laps_down > 0:
            who += f", {self.laps_down} lap{'s' if self.laps_down > 1 else ''} down"
        elif self.laps_down < 0:
            who += f", {-self.laps_down} lap{'s' if self.laps_down < -1 else ''} ahead"
        return who


@dataclass
class LapRecord:
    """One completed lap, with enough context to know whether to trust it.

    A degradation call is only as good as the laps behind it: an in-lap, an
    out-lap or a lap with a spin in it is seconds slow for reasons that have
    nothing to do with the tyres. Marking them here means the trend can throw
    them out rather than reporting the tyres as dead because of one off.
    """

    lap: int = 0
    time: float = 0.0
    position: Optional[int] = None
    fuel_left: Optional[float] = None
    fuel_used: Optional[float] = None   # actual burn on this lap, never filtered
    clean: bool = True             # no pit lane, no off, not an outlier
    note: str = ""                 # why it was discarded, when it was
    # Which session segment this lap belongs to. The history is deliberately
    # not cleared when practice rolls into qualifying into the race — the
    # fuel and pace baselines are worth carrying — so without this a lap set
    # in practice is indistinguishable from one set in the race, and "you
    # started P7" could be quoting a practice out-lap.
    session_num: Optional[int] = None


@dataclass
class TyreState:
    """One tyre across its three tread strips.

    iRacing publishes tread and carcass temperature at three points across
    each tyre. Averaging them away loses the only part that diagnoses
    anything: a hot inner edge is camber, a hot middle is pressure. The
    strips are stored here as inner/middle/outer relative to the car, not
    as the SDK's left/middle/right.
    """

    # Carcass temperatures. The SDK publishes no surface temperature at
    # all, and carcass runs much cooler than the tyre box shows — around 35
    # to 50 C is ordinary where the surface reads 80 to 100.
    inner_temp: Optional[float] = None
    middle_temp: Optional[float] = None
    outer_temp: Optional[float] = None
    inner_wear: Optional[float] = None
    middle_wear: Optional[float] = None
    outer_wear: Optional[float] = None

    @property
    def temp_spread(self) -> Optional[float]:
        """Inner minus outer, in Celsius. Positive means the inner edge is hotter."""
        if self.inner_temp is None or self.outer_temp is None:
            return None
        return self.inner_temp - self.outer_temp

    @property
    def crown(self) -> Optional[float]:
        """Middle minus the mean of the edges — the over/under-inflation signal."""
        if self.middle_temp is None or self.inner_temp is None or self.outer_temp is None:
            return None
        return self.middle_temp - (self.inner_temp + self.outer_temp) / 2.0

    def diagnosis(self, threshold: float = 8.0,
                  setup_advice: bool = True) -> Optional[str]:
        """Plain-language reading of an uneven temperature profile.

        Only speaks up past a clear threshold: a few degrees across the
        tread is normal loading, and calling that a setup fault would have
        the engineer inventing problems.

        `setup_advice` separates what the tyre is doing from what to change
        about the car. In a fixed-setup series the cause is real and the
        remedy is unavailable, so naming camber or pressure sends the
        driver looking for a setting that is greyed out.
        """
        spread, crown = self.temp_spread, self.crown
        if spread is not None and abs(spread) >= threshold:
            if spread > 0:
                return ("inner edge running hot, too much negative camber"
                        if setup_advice else "inner edge running hot")
            return ("outer edge running hot, not enough negative camber or too much roll"
                    if setup_advice else "outer edge running hot")
        if crown is not None and abs(crown) >= threshold:
            if crown > 0:
                return ("middle running hot, over-inflated"
                        if setup_advice else "middle running hot")
            return ("edges hotter than the middle, under-inflated"
                    if setup_advice else "edges hotter than the middle")
        return None


@dataclass
class CarTelemetry:
    # Live on-track running order. iRacing's own position channel only
    # recomputes at each start/finish crossing, so it lags by up to a lap.
    # iRacing's classification — what the driver sees and what decides the
    # result. Compare class_position against this, never against track order.
    #
    # None where iRacing has not classified the car: before a first timed
    # lap, in the garage, or when the server is not transmitting it. The
    # channel says 0 for that, which is not a place and must never be
    # rendered as one — see _position() in the reader.
    position: Optional[int] = None
    official_position: Optional[int] = None
    # Who is physically ahead right now, which differs mid-lap and after a
    # stop. Useful for rivals, misleading as "your position".
    on_track_position: Optional[int] = None
    speed: float = 0.0
    rpm: int = 0
    gear: str = ""

    # Tyres. None means the sim did not supply the channel — reporting 0
    # would tell the driver their tread or temps are at zero.
    tire_fl_wear: Optional[float] = None
    tire_fr_wear: Optional[float] = None
    tire_rl_wear: Optional[float] = None
    tire_rr_wear: Optional[float] = None
    tire_fl_temp: Optional[float] = None
    tire_fr_temp: Optional[float] = None
    tire_rl_temp: Optional[float] = None
    tire_rr_temp: Optional[float] = None
    # False when the tyre readings have stopped moving. iRacing restricts
    # live tyre telemetry to in-car dashboards, so for a third-party reader
    # they can freeze at whatever they were when the car last left the pits.
    tyre_data_live: bool = True
    # Per-strip detail behind the four averages above.
    tyre_fl: Optional[TyreState] = None
    tyre_fr: Optional[TyreState] = None
    tyre_rl: Optional[TyreState] = None
    tyre_rr: Optional[TyreState] = None

    tire_fl_age: int = 0
    tire_fr_age: int = 0
    tire_rl_age: int = 0
    tire_rr_age: int = 0

    # Brakes
    brake_fl_temp: Optional[float] = None
    brake_fr_temp: Optional[float] = None
    brake_rl_temp: Optional[float] = None
    brake_rr_temp: Optional[float] = None

    # Fuel
    # Fuel is reported in whatever units the driver's own sim shows, so the
    # engineer and the in-car fuel box never disagree. The SDK always sends
    # litres; iRacing's DisplayUnits decides what the driver actually sees.
    fuel_unit: str = "litres"
    # "metric" or "imperial" — resolved once by the reader, so the
    # prompt never has to ask what the driver prefers.
    units: str = "metric"
    fuel_level_pct: float = 0.0
    fuel_level: Optional[float] = None      # litres in the tank right now
    fuel_capacity: Optional[float] = None   # litres, full tank
    fuel_remaining_laps: Optional[float] = None
    # Prediction: what a racing lap costs, from clean laps only. An in-lap
    # does not forecast a green lap.
    fuel_per_lap: Optional[float] = None
    # Provenance for the prediction above.  The average alone hides whether
    # every measured clean lap reaches the flag or whether the observations
    # straddle it, which are different strategy calls.
    fuel_burn_samples: int = 0
    fuel_burn_min: Optional[float] = None
    fuel_burn_max: Optional[float] = None
    # Measurement: what actually happened, filtered by nothing. Under a
    # safety car these diverge, and the difference is the useful part.
    fuel_last_lap: Optional[float] = None
    # Whether that raw measurement came from a complete clean lap. False for
    # an in-lap, out-lap, tow or off; None when its provenance is unavailable.
    fuel_last_lap_clean: Optional[bool] = None
    fuel_used_stint: Optional[float] = None

    # Cars immediately ahead and behind, named so the engineer can say who.
    ahead: Optional[Rival] = None
    behind: Optional[Rival] = None

    # Lap data
    lap_progress: float = 0.0       # 0.0–1.0
    stint_laps: int = 0
    # iRacing reports -1 for "no valid lap yet"; that is normalised to None.
    best_lap_time: Optional[float] = None
    current_lap_time: Optional[float] = None   # live elapsed time on this lap
    last_lap_time: Optional[float] = None      # completed previous lap
    # Last completed lap, one entry per sector, None where a crossing was
    # missed. Replaces the sector_1/sector_2 pair, which existed for months
    # and was never populated by anything.
    sector_times: List[Optional[float]] = field(default_factory=list)
    # Personal best per sector, from clean laps. Rarely all from one lap,
    # which is the point: the gap to the sum of these is what is on offer.
    sector_bests: List[Optional[float]] = field(default_factory=list)
    # Live pace deltas — "am I up or down on my best?" is the question a
    # driver asks most, and iRacing publishes the answer directly.
    delta_to_best: Optional[float] = None
    delta_to_session_best: Optional[float] = None
    # Multiclass racing: overall position is not the position you are racing for.
    class_position: Optional[int] = None
    # Derived strategy numbers.
    laps_to_go: Optional[float] = None       # incl. timed races
    # True when laps_to_go was derived from the session clock rather than
    # counted. iRacing recalculates the leader's remaining laps as pace
    # changes, so this is a moving figure and must be spoken as one.
    laps_to_go_is_estimate: bool = False
    fuel_needed: Optional[float] = None      # litres to reach the end
    fuel_margin: Optional[float] = None      # +spare / -short
    fuel_needed_conservative: Optional[float] = None
    fuel_margin_best: Optional[float] = None
    fuel_margin_conservative: Optional[float] = None
    fuel_extra_lap_margin: Optional[float] = None
    # Legacy numeric rates stay empty.  Short-window physical-distance slopes
    # mostly described corner phase, not whether a race gap was closing.
    closing_ahead: Optional[float] = None
    closing_behind: Optional[float] = None
    # Change in the CarIdxEstTime gap across comparable mini-sectors. Positive
    # means the two cars are getting closer in either direction.
    closing_ahead_change: Optional[float] = None
    closing_behind_change: Optional[float] = None
    closing_ahead_segments: int = 0
    closing_behind_segments: int = 0
    # "early" after a short measured window, "confirmed" after a stable
    # longer one, "steady" when there is no useful movement, and
    # "fluctuating" when the direction changes. These keep the model from
    # turning every missing numeric rate into "I need a few laps".
    closing_ahead_status: str = "waiting"
    closing_behind_status: str = "waiting"
    closing_ahead_seconds: float = 0.0
    closing_behind_seconds: float = 0.0

    # Weather
    track_surface_temp: float = 0.0
    air_temp: float = 0.0
    rain_intensity: float = 0.0

    # Incidents. iRacing counts these per driver / per team; None means the
    # channel is not published for this session type.
    incidents: Optional[int] = None
    # In a team race it is the team total that runs against the limit, so a
    # driver on two with the team on sixteen of seventeen needs to hear
    # sixteen, not two.
    team_incidents: Optional[int] = None
    incident_limit: Optional[int] = None

    # Damage and repair state.
    tow_time: Optional[float] = None       # >0 means terminal damage, being towed
    # iRacing reports damage as outstanding repair time, not a damage percent.
    repair_required: Optional[float] = None   # seconds of mandatory repairs
    repair_optional: Optional[float] = None   # seconds of optional repairs
    brake_pressure_fl: Optional[float] = None  # bar — the SDK has no brake temps
    brake_pressure_fr: Optional[float] = None
    # Dashboard warning lights, decoded from the EngineWarnings bitfield.
    warnings: List[str] = field(default_factory=list)
    track_surface: Optional[str] = None    # on track / off track / pit stall
    pit_service_status: Optional[str] = None
    avg_lap_time: Optional[float] = None   # rolling average over clean laps
    fast_repair_used: Optional[int] = None
    fast_repair_available: Optional[int] = None
    pit_service_pending: bool = False
    on_pit_road: bool = False
    in_pit_stall: bool = False
    pits_open: Optional[bool] = None
    tire_sets_available: Dict[str, Any] = field(default_factory=dict)
    push_to_pass_count: Optional[int] = None
    push_to_pass_active: Optional[bool] = None
    joker_laps_remaining: Optional[int] = None
    on_joker_lap: Optional[bool] = None
    pit_autofill_active: Optional[bool] = None
    pit_service_requests: List[str] = field(default_factory=list)
    pit_tire_compound: Optional[int] = None

    # Engine health — early warning of a mechanical problem.
    # Levels, not just temperatures. A temperature that climbs tells you
    # something is wrong; a level that falls tells you what.
    oil_level: Optional[float] = None        # litres
    water_level: Optional[float] = None      # litres
    oil_level_drop: Optional[float] = None   # litres lost since the session began
    water_level_drop: Optional[float] = None
    water_temp: Optional[float] = None
    oil_temp: Optional[float] = None
    oil_pressure: Optional[float] = None
    fuel_pressure: Optional[float] = None
    voltage: Optional[float] = None

    track_wetness: Optional[str] = None
    # Fuller weather picture. Wind moves braking points and top speed; air
    # density moves engine power; skies and declared-wet drive tyre calls.
    wind_speed: Optional[float] = None        # km/h
    wind_direction: Optional[str] = None      # compass point
    humidity: Optional[float] = None          # percent
    air_pressure: Optional[float] = None      # hPa (converted from the SDK's Pa)
    air_density: Optional[float] = None
    skies: Optional[str] = None
    fog: Optional[float] = None               # percent
    declared_wet: Optional[bool] = None
    time_of_day: Optional[str] = None         # local clock at the track

    # Flag
    flag: str = "Green"
    # The black flag means two different penalties depending on whether the
    # furled flag is up with it, and they need different driving.
    penalty: Optional[str] = None
    # The live index. Modern iRacing session YAML also publishes the player's
    # index-to-name table; rival-car names still are not published.
    tyre_compound: Optional[int] = None


class TelemetryAdapter(ABC):
    """Abstract telemetry interface for any simulator."""

    #: Short identifier reported to the UI.
    name: str = "adapter"

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the sim is running or data is streaming."""
        ...

    @abstractmethod
    def get_telemetry_snapshot(self) -> Optional["TelemetrySnapshot"]:
        """Return the current telemetry, or None when unavailable.

        Implementations must never raise: a dead sim looks like None, so the
        rest of the pipeline keeps working with no telemetry context.
        """
        ...

    def shutdown(self) -> None:
        """Release any sim resources. Safe to call more than once."""
        return None

    def pit_commands_ready(self) -> Tuple[bool, str]:
        """Whether this adapter can safely send and verify a pit command."""
        return False, "pit commands need a live iRacing session"

    def pit_tire_compounds(self) -> Sequence[Any]:
        """Available named compounds for the player's current car."""
        return ()

    def apply_pit_commands(self, commands: Sequence[Any]) -> Tuple[bool, str]:
        """Apply already-confirmed commands and verify their resulting state."""
        return False, "pit commands are not supported by this telemetry source"

    # ── Diagnostics ─────────────────────────────────────────────────
    #
    # The panel's three diagnostic views need what the sim is publishing
    # right now, unprocessed — that is how a wrong number gets traced back
    # to its source. They used to read the iRacing reader's privates
    # directly, which made this ABC a lie: a second backend would have
    # satisfied the interface and still broken the panel. Sources that
    # cannot answer say so out loud rather than pretending to be empty.

    def _no_raw_access(self) -> Dict[str, Any]:
        return {"available": False,
                "reason": f"source '{self.name}' does not publish raw channels"}

    def raw_channels(self, names: Sequence[str] = ()) -> Dict[str, Any]:
        """Current values of named channels. No names means a curated set."""
        return self._no_raw_access()

    def list_channels(self, match: str = "") -> Dict[str, Any]:
        """Every channel the sim publishes, with its value. `match` filters."""
        return self._no_raw_access()

    def session_yaml(self, key: str = "") -> Dict[str, Any]:
        """The sim's session-info blocks. No key lists what is available."""
        return self._no_raw_access()


class TelemetrySnapshot:
    """Lightweight wrapper returned by get_telemetry_snapshot()."""

    def __init__(self, car: CarTelemetry, track: TrackConfig):
        self.car = car
        self.track = track
        self._raw: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Flat dict of car + track values, for the web UI."""
        data = asdict(self.car)
        data.update(asdict(self.track))
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TelemetrySnapshot":
        fields = {k: v for k, v in data.items() if k in CarTelemetry.__dataclass_fields__}
        # asdict() flattens the nested tyre records, so rebuild them: the
        # prompt calls TyreState.diagnosis() and a bare dict has no methods.
        for key in ("tyre_fl", "tyre_fr", "tyre_rl", "tyre_rr"):
            if isinstance(fields.get(key), dict):
                fields[key] = TyreState(**fields[key])
        for key in ("ahead", "behind"):
            if isinstance(fields.get(key), dict):
                fields[key] = Rival(**fields[key])
        car = CarTelemetry(**fields)
        track_fields = {
            k: v for k, v in data.items() if k in TrackConfig.__dataclass_fields__
        }
        track_fields["laps"] = [
            LapRecord(**lap) if isinstance(lap, dict) else lap
            for lap in track_fields.get("laps", [])
        ]
        track_fields["field"] = [
            Rival(**rival) if isinstance(rival, dict) else rival
            for rival in track_fields.get("field", [])
        ]
        if isinstance(track_fields.get("fastest_lap"), dict):
            track_fields["fastest_lap"] = Rival(**track_fields["fastest_lap"])
        track = TrackConfig(**track_fields)
        snap = cls(car, track)
        snap._raw = data
        return snap


def session_kind(session_type: str) -> str:
    """Classify an iRacing session type as race / qualify / practice.

    The strings iRacing publishes are not obvious — Test Drive reports
    "Offline Testing", and a warmup is practice in all but name. Matching
    on substrings independently in two places is how "Offline Testing"
    ended up being treated as a race, so both the prompt and the
    calculations come through here.

    Anything unrecognised is treated as a race, which is the conservative
    answer: a race engineer being cautious about fuel in a practice session
    is a smaller failure than one ignoring it in a race.
    """
    name = (session_type or "").strip().lower()
    if "qual" in name:
        return "qualify"
    # "Offline Testing", "Lone Practice", "Warmup" are all practice in
    # everything that matters: no result, no finish, nothing at stake.
    if any(word in name for word in ("practice", "warmup", "test")):
        return "practice"
    return "race"


def create_telemetry_adapter(source: str = "auto",
                             fuel_units: str = "auto",
                             units: str = "auto") -> TelemetryAdapter:
    """Build the telemetry adapter named by `source`.

    "auto" prefers iRacing and falls back to the simulator when irsdk is
    unavailable, which is what lets the app run off a race rig.
    """
    from .simulated import SimulatedTelemetryAdapter

    source = (source or "auto").lower()

    if source in ("simulated", "sim", "demo"):
        return SimulatedTelemetryAdapter()

    if source in ("auto", "irsdk", "iracing"):
        from .irsdk_reader import HAS_IRSDK, iRacingTelemetryReader

        if HAS_IRSDK:
            return iRacingTelemetryReader(fuel_units=fuel_units, units=units)
        if source == "auto":
            logger.info("irsdk unavailable — using simulated telemetry")
            return SimulatedTelemetryAdapter()
        logger.warning("Telemetry source 'irsdk' requested but irsdk is not installed")
        return iRacingTelemetryReader(fuel_units=fuel_units, units=units)

    logger.warning("Unknown telemetry source %r — using simulated", source)
    return SimulatedTelemetryAdapter()
