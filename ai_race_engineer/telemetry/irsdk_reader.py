"""iRacing telemetry reader using pyirsdk.

Reads from the iRacing shared memory SDK. Target platform: Windows with
iRacing running. On any other platform `irsdk` simply won't import and
`is_connected()` stays False, so the app falls back to the simulator.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from . import (CarTelemetry, LapRecord, Rival, TelemetryAdapter,
               TelemetrySnapshot, TrackConfig, TyreState, session_kind)
from ..pit_commands import PitCommand, PitTireCompound

logger = logging.getLogger("race-engineer")

# How long to wait before retrying irsdk.startup() while iRacing is closed.
_STARTUP_RETRY_SECONDS = 2.0

try:
    import irsdk

    HAS_IRSDK = True
except ImportError:  # not Windows, or irsdk not installed
    irsdk = None
    HAS_IRSDK = False


# iRacing SessionFlags bitfield, from irsdk.h. Ordered most severe first so
# the driver hears the flag that actually matters.
#
# These values were verified against a live session: the previous table had
# every bit misassigned, which decoded a black flag as "Green".
_FLAG_BITS: List[Tuple[int, str]] = [
    (0x00010000, "Black"),
    (0x00020000, "Disqualify"),
    (0x00100000, "Meatball — repair required"),
    (0x00080000, "Furled — warning"),
    (0x00000010, "Red"),
    (0x00008000, "Caution Waving"),
    (0x00004000, "Caution"),
    (0x00000100, "Yellow Waving"),
    (0x00000008, "Yellow"),
    (0x00000001, "Checkered"),
    (0x00000002, "White — final lap"),
    (0x00000020, "Blue — faster car behind"),
    (0x00000040, "Debris"),
    (0x00000080, "Crossed"),
    (0x00001000, "Five to go"),
    (0x00000800, "Ten to go"),
    (0x00000400, "Green held"),
    (0x00000200, "One lap to green"),
    (0x00000004, "Green"),
]

# iRacing TrackWetness enum. Absent on older builds — _get() returns None and
# the field simply never reaches the prompt.
_TRACK_WETNESS = {
    0: "unknown", 1: "dry", 2: "mostly dry", 3: "damp",
    4: "slightly wet", 5: "wet", 6: "very wet", 7: "extremely wet",
}

def _enum_map(cls_name: str, fallback: Dict[int, str]) -> Dict[int, str]:
    """Prefer pyirsdk's own constants over hardcoded values.

    Every previous hardcoded table in this file (the flag bits, PitLapCompleted)
    turned out wrong. Reading the library's values removes the guess.
    """
    cls = getattr(irsdk, cls_name, None) if irsdk else None
    if cls is None:
        return fallback
    return {v: k.replace("_", " ") for k, v in vars(cls).items()
            if not k.startswith("_") and isinstance(v, int)}


# _enum_map returns pyirsdk's own identifiers, and those reach the model and
# then the driver's ears. "not in world" is engine vocabulary, not radio, and
# "aproaching pits" is misspelled in the library — read aloud it came out as
# the typo. Translate here rather than at the point of speech, so the reader
# stays the only place that knows what the numbers mean.
_SPOKEN_SURFACE = {
    "not in world": "not in the session",
    "in pit stall": "in the pit stall",
    "aproaching pits": "in the pit lane",
    "approaching pits": "in the pit lane",
}


def _at(seq: Any, idx: int, default: Any = None) -> Any:
    """seq[idx], or `default` when the channel is short or absent."""
    try:
        return seq[idx]
    except (IndexError, TypeError):
        return default


def _lap_deficit(best: Optional[float],
                 session_best: Optional[float]) -> Optional[float]:
    """How far off the session's best lap this car is, in seconds.

    The time-session equivalent of a gap to the leader, and the number the
    driver is reading off the timing screen. None when either lap is
    missing — a car with no time yet is not "zero off the pace".
    """
    if best is None or session_best is None:
        return None
    return round(best - session_best, 3)


def _decode_warnings(value: Any) -> List[str]:
    """EngineWarnings bitfield -> readable dashboard lights."""
    try:
        bits = int(value or 0)
    except (TypeError, ValueError):
        return []
    labels = {
        1: "water temperature warning", 2: "fuel pressure warning",
        4: "oil pressure warning", 8: "engine stalled",
        16: "pit speed limiter on", 32: "on the rev limiter",
        64: "oil temperature warning", 128: "mandatory repairs needed",
        256: "optional repairs needed",
    }
    cls = getattr(irsdk, "EngineWarnings", None) if irsdk else None
    if cls is not None:
        pretty = {
            "water_temp_warning": "water temperature warning",
            "fuel_pressure_warning": "fuel pressure warning",
            "oil_pressure_warning": "oil pressure warning",
            "engine_stalled": "engine stalled",
            "pit_speed_limiter": "pit speed limiter on",
            "rev_limiter_active": "on the rev limiter",
            "oil_temp_warning": "oil temperature warning",
            "mand_rep_needed": "mandatory repairs needed",
            "opt_rep_needed": "optional repairs needed",
        }
        labels = {v: pretty.get(k, k.replace("_", " "))
                  for k, v in vars(cls).items()
                  if not k.startswith("_") and isinstance(v, int)}
    # Limiter and rev limiter are normal driving, not faults worth radioing.
    routine = {"pit speed limiter on", "on the rev limiter"}
    return [label for bit, label in sorted(labels.items())
            if bits & bit and label not in routine]


_SKIES = {0: "clear", 1: "partly cloudy", 2: "mostly cloudy", 3: "overcast"}

_COMPASS = ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"]


def _bearing(radians: Any) -> Optional[str]:
    """iRacing gives wind direction in radians; drivers think in compass points."""
    try:
        deg = math.degrees(float(radians)) % 360
    except (TypeError, ValueError):
        return None
    return _COMPASS[int((deg + 22.5) % 360 // 45)]


def _clock_time(seconds: Any) -> Optional[str]:
    """SessionTimeOfDay is seconds past midnight at the track."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return None
    return f"{(total // 3600) % 24:02d}:{(total % 3600) // 60:02d}"


# iRacing SessionState enum (irsdk.h). Without this the app had no signal
# that a race had finished — the checkered flag shows for one lap and the
# session clock simply stops counting.
_SESSION_STATES = {
    0: "Invalid",
    1: "GetInCar",
    2: "Warmup",
    3: "ParadeLaps",
    4: "Racing",
    5: "Checkered",
    6: "CoolDown",
}


# Bits that describe the start sequence or car serviceability rather than a
# flag being shown, so they must not mask the real flag.
_NON_FLAG_BITS = 0x00040000 | 0x10000000 | 0x20000000 | 0x40000000 | 0x80000000


# Channels this reader consumes, plus close alternatives — dumped by
# raw_channels() so a wrong value can be traced back to its source.
DIAGNOSTIC_CHANNELS: Tuple[str, ...] = (
    "Lap", "LapCompleted", "LapDistPct",
    "LapCurrentLapTime", "LapLastLapTime", "LapBestLapTime",
    "SessionTime", "SessionTimeRemain", "SessionTimeTotal",
    "SessionLapsRemain", "SessionLapsRemainEx", "SessionLapsTotal",
    "SessionNum", "SessionState", "SessionFlags",
    "Speed", "RPM", "Gear", "PlayerCarPosition", "PlayerCarIdx",
    "FuelLevel", "FuelLevelPct", "FuelUsePerHour",
    "PitsOpen", "TireSetsAvailable", "FrontTireSetsAvailable",
    "RearTireSetsAvailable", "LeftTireSetsAvailable", "RightTireSetsAvailable",
    "P2P_Count", "P2P_Status", "CarIdxP2P_Count", "CarIdxP2P_Status",
    "SessionJokerLapsRemain", "SessionOnJokerLap",
    "dpFuelAutoFillActive", "PitSvFlags", "PitSvFuel",
    "PitSvLFP", "PitSvRFP", "PitSvLRP", "PitSvRRP", "PitSvTireCompound",
    "LFwearL", "LFwearM", "LFwearR", "RFwearL", "RFwearM", "RFwearR",
    "LRwearL", "LRwearM", "LRwearR", "RRwearL", "RRwearM", "RRwearR",
    "LFtempCL", "LFtempCM", "LFtempCR", "RFtempCL", "RFtempCM", "RFtempCR",
    "TrackTemp", "TrackTempCrew", "AirTemp", "Precipitation",
    "OnPitRoad", "PlayerCarInPitStall",
    "CarIdxEstTime", "CarIdxPosition", "CarIdxLapDistPct",
)

# Session-info blocks worth listing when no key was asked for. Live channels
# are only half the picture — session setup and weather configuration live
# in this blob and in no telemetry var.
_SESSION_BLOCKS = ("WeekendInfo", "SessionInfo", "DriverInfo", "SplitTimeInfo",
                   "CarSetup", "RadioInfo", "QualifyResultsInfo")

_NOT_IN_SESSION = "iRacing is not running or not in a session"


def _flag_bits() -> List[Tuple[int, str]]:
    """Prefer pyirsdk's own constants when it exposes them."""
    flags = getattr(irsdk, "Flags", None)
    if flags is None:
        return _FLAG_BITS
    named = [
        ("black", "Black"), ("disqualify", "Disqualify"), ("repair", "Meatball — repair required"),
        ("furled", "Furled — warning"), ("red", "Red"), ("caution_waving", "Caution Waving"),
        ("caution", "Caution"), ("yellow_waving", "Yellow Waving"), ("yellow", "Yellow"),
        ("checkered", "Checkered"), ("white", "White — final lap"),
        ("blue", "Blue — faster car behind"), ("debris", "Debris"), ("crossed", "Crossed"),
        ("five_to_go", "Five to go"), ("ten_to_go", "Ten to go"),
        ("green_held", "Green held"), ("one_lap_to_green", "One lap to green"),
        ("green", "Green"),
    ]
    resolved = [(getattr(flags, attr), label) for attr, label in named if hasattr(flags, attr)]
    return resolved or _FLAG_BITS


def _flag_value(attr: str, fallback: int) -> int:
    """One SessionFlags bit, from pyirsdk where it exposes them."""
    flags = getattr(irsdk, "Flags", None) if irsdk else None
    return getattr(flags, attr, fallback) if flags is not None else fallback


def _decode_penalty(value: Optional[int]) -> Optional[str]:
    """What the black flag currently means, if anything.

    Black on its own and black with the furled flag are *different
    penalties* and need different driving: one is a stop-and-go, the other
    is a slow-down. _decode_flag() collapses both to "Black" because it
    reports a single most-severe label, so the driver could not tell which
    one they were serving.

    The pairing is CrewChief's, which has been run against live iRacing for
    years (iRacingGameStateMapper.cs, PenaltiesData).
    """
    try:
        bits = int(value or 0)
    except (TypeError, ValueError):
        return None
    black = _flag_value("black", 0x00010000)
    furled = _flag_value("furled", 0x00080000)
    repair = _flag_value("repair", 0x00100000)
    disqualify = _flag_value("disqualify", 0x00020000)

    if bits & disqualify:
        return "disqualified"
    if bits & black:
        # Furled alongside black downgrades it from a stop-and-go to a
        # slow-down. Furled on its own is only a warning.
        return "slow-down penalty" if bits & furled else "stop-and-go penalty"
    if bits & repair:
        return "meatball — must pit for repairs"
    if bits & furled:
        return "warned — furled black flag"
    return None


def _decode_flag(value: Optional[int]) -> str:
    """Map the iRacing SessionFlags bitfield to a single readable label."""
    try:
        bits = int(value or 0)
    except (TypeError, ValueError):
        return "Green"
    bits &= ~_NON_FLAG_BITS
    if not bits:
        return "Green"
    for bit, label in _flag_bits():
        if bits & bit:
            return label
    return "Green"


def _gear_label(gear: Optional[int]) -> str:
    if gear is None:
        return ""
    if gear == -1:
        return "R"
    if gear == 0:
        return "N"
    return str(gear)


def _mean(values: List[Optional[float]]) -> Optional[float]:
    """Mean of the values present, or None when the sim supplied none."""
    nums = [v for v in values if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


# iRacing sends fuel in litres regardless of what the driver sees. Reading
# it back as litres to someone whose black box is in gallons is a 3.785x
# error, and the driver has no way to tell which of you is wrong.
_LITRES_PER_US_GALLON = 3.785411784
# Petrol density, used only when the session does not publish its own.
_DEFAULT_FUEL_DENSITY = 0.75


def _positive(value: Any) -> Optional[float]:
    """Normalise iRacing's -1 'no valid value' sentinel to None.

    Lap times, deltas and estimates all use -1 before a valid sample
    exists; passed through untouched they surface as negative seconds.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _delta(value: Any, ok_flag: Any = None) -> Optional[float]:
    """Lap deltas, which are meaningfully negative when you are up on the lap.

    iRacing pairs each delta with an `_OK` validity flag; when that flag is
    published and false, the delta is stale rather than zero.
    """
    if ok_flag is not None and not ok_flag:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fast_repairs(value: Any) -> Optional[int]:
    """FastRepairAvailable uses 255 as 'unlimited' per the SDK docs."""
    count = _count(value)
    return None if count == 255 else count


def _irating(entry: Dict[str, Any]) -> Optional[int]:
    """A driver's iRating, or None when there is not a real one.

    AI opponents carry a rating that describes the AI strength setting, not
    a person, so reporting it as a driver's skill would be inventing a fact
    about a competitor. iRacing also uses -1 and 0 for "no rating yet".
    """
    if entry.get("CarIsAI"):
        return None
    try:
        rating = int(entry.get("IRating") or 0)
    except (TypeError, ValueError):
        return None
    return rating if rating > 0 else None


def _non_negative(value: Any) -> Optional[float]:
    """A float where zero is a real reading, not a missing channel.

    `_positive` maps 0 to None, which is right for lap times and wrong for
    fuel: a car that has run the tank dry reads 0.0, and reporting that as
    "I've got no fuel reading" is the worst possible moment to go quiet.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _count(value: Any) -> Optional[int]:
    """Non-negative counters — negative or absent means not published."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _optional_bool(value: Any) -> Optional[bool]:
    """A published false is data; an absent channel is not false."""
    return None if value is None else bool(value)


def _who(rival) -> str:
    """Stable identity for a rival, so a closing rate cannot span two cars."""
    if rival is None:
        return ""
    return f"{getattr(rival, 'car_number', '')}|{getattr(rival, 'name', '')}"


#: Longest roster string kept. iRacing names and team names run well under
#: this; anything longer is not a name being reported.
_MAX_ROSTER_TEXT = 80


def _roster_text(value: Any) -> str:
    """A driver-supplied string, made safe to put in the prompt.

    Names, team names and car numbers are the only text in this app that
    another person chooses. They reach the model inside a block of "- "
    lines, and every behavioural guarantee here — never invent, always call
    the calculators, refuse in character — is enforced by that prompt. A
    display name carrying a newline could open a line of its own and say
    whatever it liked.

    So the structural fix rather than a stronger instruction: control
    characters and line breaks collapse to spaces, runs of whitespace
    collapse to one, and the result is capped. iRacing's real-name
    verification makes a hostile name remote; this costs nothing and does
    not depend on it.
    """
    text = str(value or "")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(cleaned.split())[:_MAX_ROSTER_TEXT].strip()


def _int_or(value: Any, default: Optional[int] = None) -> Optional[int]:
    """int() that returns a default instead of raising. Keeps negatives —
    callers here need to tell "-1, not yet published" from "0 laps done"."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _duration(value: Any, limit: float = 86400.0) -> Optional[float]:
    """Session durations, with the 'unlimited' sentinel mapped to None.

    iRacing reports a week (604800 s) for sessions with no time limit.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0 or seconds >= limit:
        return None
    return seconds


class iRacingTelemetryReader(TelemetryAdapter):
    """Read telemetry from the iRacing shared memory SDK."""

    name = "irsdk"

    def __init__(self, fuel_units: str = "auto", units: str = "auto") -> None:
        # How the driver wants fuel spoken; see _fuel_unit().
        self._fuel_units_setting = fuel_units
        # Everything else that carries a unit; see _unit_system().
        self._units_setting = units
        self._ir = irsdk.IRSDK() if HAS_IRSDK else None
        self._started = False
        # Session info is a large YAML blob. iRacing only republishes it on
        # session events, so re-reading it every poll is pure overhead — and
        # it is what made snapshots slow enough to feel stale.
        self._session_cache: Dict[str, Any] = {}
        self._session_update: int = -1
        # Identity of the session the accumulated history belongs to.
        # None until the first read; see _check_session_identity().
        self._session_identity: Optional[Tuple] = None
        self._session_generation: int = 0
        # SessionTime and SessionTick are monotonic within one SessionNum.
        # Their reset catches an offline/AI restart whose YAML IDs stay zero.
        self._session_clock: Optional[Tuple[Optional[int], Optional[float],
                                            Optional[int]]] = None
        self._last_snapshot_ms: float = 0.0
        self._last_startup_attempt: float = 0.0
        self._pit_lap: int = 0
        # (LapCompleted, FuelLevel) at the last crossing, plus recent per-lap
        # burn — the only trustworthy basis for a fuel estimate.
        self._fuel_marker: Optional[Tuple[int, float]] = None
        # Lowest live level seen since that marker. A splash can raise the
        # tank substantially without ending above the previous line-crossing
        # level, so the crossing alone cannot detect it.
        self._fuel_minimum: Optional[float] = None
        # The first marker is set wherever the car happens to be in the lap,
        # so the burn measured to the next crossing covers part of a lap and
        # is not comparable with a full one.
        self._fuel_marker_is_partial: bool = True
        # Set by the lap recorder when the session segment changes, consumed
        # by the fuel average. See _update_fuel_usage().
        self._segment_restarted: bool = False
        self._fuel_usage: Deque[float] = deque(maxlen=5)
        # Completed laps this session, plus flags for whether the lap in
        # progress can be trusted as a clean reference.
        self._laps: Deque[LapRecord] = deque(maxlen=120)
        self._lap_marker: Optional[int] = None
        # Which session segment we were in at the last poll. LapCompleted is
        # "Laps completed count" with no session qualifier — which is why
        # RaceLaps exists separately — so it is not known whether it keeps
        # counting across practice → qualifying → race or restarts. Watching
        # SessionNum covers both: the laps get tagged either way, and the
        # marker is re-baselined so a restarted count cannot lock the
        # recorder out of the race entirely.
        self._session_num: Optional[int] = None
        # Where the race started from, latched at green when the qualifying
        # results do not supply it. See _grid_position().
        self._grid_latched: Optional[int] = None
        self._grid_latched_class: Optional[int] = None
        self._lap_dirty: bool = False
        self._lap_dirty_reason: str = ""
        # Verdict on the lap most recently completed, read by the fuel average.
        self._last_lap_clean: bool = True
        # iRacing restricts live tyre telemetry to in-car dashboards: for
        # third-party readers the values only refresh in the pits, so a race
        # spent on track reports whatever they were at the last stop. Rather
        # than assume that holds for every car and series, watch whether the
        # numbers actually move and say which it is.
        # Opening levels, so a loss can be reported rather than a bare
        # number the driver has no baseline for.
        self._oil_level_start: Optional[float] = None
        self._water_level_start: Optional[float] = None
        self._tyre_signature: Optional[tuple] = None
        self._tyre_changed_at: float = 0.0
        # Actual burn, kept regardless of whether the lap was clean.
        self._last_lap_fuel: Optional[float] = None
        self._last_lap_fuel_clean: Optional[bool] = None
        self._stint_start_fuel: Optional[float] = None
        # (lap-progress gate, monotonic time, timing gap) samples. Comparing
        # the same small slices of track removes the enormous false closing
        # rates that braking and corner phase produced from physical distance.
        self._gap_hist_ahead: Deque[Tuple[int, float, float]] = deque(maxlen=40)
        self._gap_hist_behind: Deque[Tuple[int, float, float]] = deque(maxlen=40)
        # Which car each gap history belongs to; see _closing_trend.
        self._closing_who: Dict[int, str] = {}
        # Last seen lap number per car index, for the start/finish desync in
        # _live_order(). Cleared per session, not per poll.
        self._prev_lap: Dict[int, int] = {}
        # Sector timing, per car index. Each entry keeps the previous
        # (lap distance %, session time) sample so a boundary crossing can
        # be interpolated between polls rather than snapped to one, plus
        # the crossing times collected so far this lap.
        self._sector_prev: Dict[int, Tuple[float, float]] = {}
        self._sector_marks: Dict[int, Dict[int, float]] = {}
        self._sector_times: Dict[int, List[Optional[float]]] = {}
        self._sector_dirty: Dict[int, bool] = {}
        self._sector_best: List[Optional[float]] = []
        self._read_errors: int = 0
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def last_error(self) -> Optional[str]:
        """Why the most recent read failed, surfaced to the status bar."""
        return self._last_error

    @property
    def last_snapshot_ms(self) -> float:
        """How long the most recent snapshot took, for diagnostics."""
        return self._last_snapshot_ms

    # ── TelemetryAdapter ────────────────────────────────────────────

    def is_connected(self) -> bool:
        if self._ir is None:
            return False
        try:
            if not self._started or not self._ir.is_initialized:
                # startup() is expensive; without this throttle the poll loop
                # retried it on every tick while iRacing was closed.
                now = time.monotonic()
                if now - self._last_startup_attempt < _STARTUP_RETRY_SECONDS:
                    return False
                self._last_startup_attempt = now
                self._started = bool(self._ir.startup())
                if self._started:
                    self._session_cache.clear()
                    self._session_update = -1
            return bool(self._ir.is_initialized and self._ir.is_connected)
        except Exception as exc:  # irsdk raises assorted OS errors
            logger.debug("irsdk connection check failed: %s", exc)
            return False

    def shutdown(self) -> None:
        if self._ir is not None and self._started:
            try:
                self._ir.shutdown()
            except Exception:
                pass
            self._started = False

    def pit_commands_ready(self) -> Tuple[bool, str]:
        """Writes are allowed only with live state and the driver in the car."""
        with self._lock:
            if not self.is_connected():
                return False, "iRacing is not connected"
            if not bool(self._get("IsOnTrack", False)):
                return False, "you need to be in the car"
            if self._get("PitSvFlags") is None:
                return False, "iRacing is not publishing pit-service state"
            return True, ""

    def pit_tire_compounds(self) -> Tuple[PitTireCompound, ...]:
        """Read iRacing's live tire-index mapping for the player's car."""
        with self._lock:
            info = self._session("DriverInfo", {}) or {}
            rows = info.get("DriverTires") if isinstance(info, dict) else None
            if not isinstance(rows, list):
                return ()
            compounds: List[PitTireCompound] = []
            names = set()
            indexes = set()
            for row in rows:
                if not isinstance(row, dict):
                    return ()
                name = str(row.get("TireCompoundType") or "").strip()
                index = row.get("TireIndex")
                if (not name or isinstance(index, bool)
                        or not isinstance(index, int) or index < 0):
                    return ()
                key = " ".join(name.lower().replace("-", " ").split())
                if key in names or index in indexes:
                    return ()
                names.add(key)
                indexes.add(index)
                compounds.append(PitTireCompound(name, index))
            return tuple(compounds)

    def apply_pit_commands(
        self, commands: List[PitCommand]
    ) -> Tuple[bool, str]:
        """Broadcast a confirmed plan, then read the pit controls back.

        A successful Windows message only says the broadcast was queued. The
        driver needs to know that iRacing actually changed its controls, so
        every affected flag and numeric setting is checked on later ticks.
        """
        with self._lock:
            if not self.is_connected():
                return False, "iRacing disconnected before confirmation"
            if not bool(self._get("IsOnTrack", False)):
                return False, "you are no longer in the car"
            initial = self._get("PitSvFlags")
            if initial is None:
                return False, "pit-service state is unavailable"

            flag_cls = getattr(irsdk, "PitSvFlags", None)
            mode_cls = getattr(irsdk, "PitCommandMode", None)
            if flag_cls is None or mode_cls is None:
                return False, "this pyirsdk build has no pit-command definitions"

            flag_for_mode = {
                "lf": getattr(flag_cls, "lf_tire_change", None),
                "rf": getattr(flag_cls, "rf_tire_change", None),
                "lr": getattr(flag_cls, "lr_tire_change", None),
                "rr": getattr(flag_cls, "rr_tire_change", None),
                "fuel": getattr(flag_cls, "fuel_fill", None),
                "ws": getattr(flag_cls, "windshield_tearoff", None),
                "fr": getattr(flag_cls, "fast_repair", None),
            }
            if any(bit is None for bit in flag_for_mode.values()):
                return False, "this pyirsdk build has incomplete pit-service flags"

            expected: Dict[str, bool] = {}
            values: Dict[str, int] = {}
            pressure_channels = {
                "lf": "PitSvLFP", "rf": "PitSvRFP",
                "lr": "PitSvLRP", "rr": "PitSvRRP",
            }
            for command in commands:
                if not hasattr(mode_cls, command.mode):
                    return False, f"unsupported pit command '{command.mode}'"
                if command.mode == "clear":
                    expected = {mode: False for mode in flag_for_mode}
                elif command.mode == "clear_tires":
                    for mode in ("lf", "rf", "lr", "rr"):
                        expected[mode] = False
                elif command.mode == "clear_ws":
                    expected["ws"] = False
                elif command.mode == "clear_fr":
                    expected["fr"] = False
                elif command.mode == "clear_fuel":
                    expected["fuel"] = False
                elif command.mode in flag_for_mode:
                    expected[command.mode] = True
                    if command.value:
                        channel = ("PitSvFuel" if command.mode == "fuel"
                                   else pressure_channels.get(command.mode))
                        if channel:
                            if self._get(channel) is None:
                                return False, f"{channel} is unavailable"
                            values[channel] = command.value
                elif command.mode == "tc":
                    if self._get("PitSvTireCompound") is None:
                        return False, "PitSvTireCompound is unavailable"
                    values["PitSvTireCompound"] = command.value

            for command in commands:
                accepted = self._ir.pit_command(
                    getattr(mode_cls, command.mode), int(command.value))
                if not accepted:
                    return False, "Windows rejected the pit-command broadcast"

            def applied() -> bool:
                try:
                    flags = int(self._get("PitSvFlags"))
                except (TypeError, ValueError):
                    return False
                if any(bool(flags & flag_for_mode[mode]) != wanted
                       for mode, wanted in expected.items()):
                    return False
                for channel, wanted in values.items():
                    try:
                        actual = float(self._get(channel))
                    except (TypeError, ValueError):
                        return False
                    if abs(actual - wanted) > 0.51:
                        return False
                return True

            # iRacing publishes at 60 Hz. Give the broadcast several fresh
            # ticks, but do not stall the radio for more than a quarter second.
            for _ in range(5):
                time.sleep(0.05)
                if applied():
                    return True, ""
            return False, "iRacing did not report the requested pit service"

    def get_telemetry_snapshot(self) -> Optional[TelemetrySnapshot]:
        # The poll loop and the request handlers can both land here, and
        # pyirsdk keeps the frozen buffer as instance state — concurrent
        # freeze/unfreeze would interleave and corrupt a read.
        with self._lock:
            if not self.is_connected():
                return None
            started = time.perf_counter()
            try:
                # Freeze so every channel in a snapshot comes from one tick.
                self._ir.freeze_var_buffer_latest()
                snapshot = self._read_snapshot()
                self._read_errors = 0
                self._last_error = None
                return snapshot
            except Exception as exc:
                # A bug in here reads to the driver as "no telemetry", with
                # nothing in the log to say why. Report the first occurrence
                # loudly, with a traceback, then throttle the repeats.
                self._read_errors += 1
                if self._read_errors == 1 or self._read_errors % 500 == 0:
                    logger.error(
                        "Telemetry read failed (%d times): %s",
                        self._read_errors, exc, exc_info=True,
                    )
                self._last_error = f"{type(exc).__name__}: {exc}"
                return None
            finally:
                try:
                    self._ir.unfreeze_var_buffer_latest()
                except Exception:
                    pass
                self._last_snapshot_ms = (time.perf_counter() - started) * 1000

    # ── Diagnostics ─────────────────────────────────────────────────
    #
    # What the sim is publishing right now, unprocessed, so a wrong number
    # in an answer can be traced to the channel it came from. The lock is
    # the same one the snapshot takes: pyirsdk keeps the frozen buffer as
    # instance state, so an unsynchronised read here would interleave with
    # a poll.

    def raw_channels(self, names: Sequence[str] = ()) -> Dict[str, Any]:
        """Values of named channels. No names means `DIAGNOSTIC_CHANNELS`."""
        wanted = [str(name).strip() for name in names if str(name).strip()]
        wanted = wanted or list(DIAGNOSTIC_CHANNELS)
        with self._lock:
            if not self.is_connected():
                return {"available": False, "reason": _NOT_IN_SESSION}
            present: List[str] = []
            missing: List[str] = []
            values: Dict[str, Any] = {}
            for name in wanted:
                raw = self._get(name)
                if raw is None:                       # absent, or unreadable
                    missing.append(name)
                    continue
                present.append(name)
                # Per-car arrays are long; a head is enough to see the shape.
                values[name] = raw[:8] if isinstance(raw, list) else raw
            return {
                "available": True,
                "session_type": self._session_type(),
                "present": present,
                "missing": missing,
                "values": values,
                "read_ms": self._last_snapshot_ms,
            }

    def list_channels(self, match: str = "") -> Dict[str, Any]:
        """Every channel iRacing publishes, with its current value.

        This reader consumes a hand-picked subset; listing the whole set is
        how a new field gets chosen from what the sim actually exposes rather
        than from memory.
        """
        with self._lock:
            if not self.is_connected():
                return {"available": False, "reason": _NOT_IN_SESSION}
            try:
                # pyirsdk-private, and there is no public equivalent. It stays
                # in the module that owns pyirsdk, so a different backend can
                # answer this question its own way instead of inheriting this
                # one's internals.
                names = sorted(self._ir._var_headers_dict.keys())
            except Exception as exc:
                return {"available": False,
                        "reason": f"could not enumerate channels: {exc}"}

            needle = match.lower()
            out: Dict[str, Any] = {}
            for name in names:
                if needle and needle not in name.lower():
                    continue
                value = self._get(name)
                if value is None:
                    continue
                if isinstance(value, list):
                    value = {"list_len": len(value), "head": value[:6]}
                out[name] = value
            return {"available": True, "total_channels": len(names),
                    "returned": len(out), "channels": out}

    def session_yaml(self, key: str = "") -> Dict[str, Any]:
        """Dump iRacing's session-info YAML. No key lists the top-level blocks."""
        with self._lock:
            if not self.is_connected():
                return {"available": False, "reason": _NOT_IN_SESSION}
            if key:
                try:
                    return {"available": True, "key": key, "value": self._ir[key]}
                except Exception as exc:
                    return {"available": True, "key": key, "error": str(exc)}

            found: Dict[str, Any] = {}
            for name in _SESSION_BLOCKS:
                value = self._get(name)
                if value is None:
                    continue
                found[name] = (sorted(value.keys()) if isinstance(value, dict)
                               else type(value).__name__)
            return {"available": True, "blocks": found,
                    "hint": "add ?key=WeekendInfo to expand one"}

    # ── Internal helpers ────────────────────────────────────────────

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self._ir[key]
        except Exception:
            return default
        return default if value is None else value

    def _check_session_identity(self) -> None:
        """Throw away history that belongs to a session we have left.

        `session_info_update` bumps on every routine republish — a flag, an
        incident, practice rolling into qualifying — so it cannot be used
        for this: clearing lap times on each of those would destroy the very
        data the fuel and pace answers are built from.

        A genuinely different session is a different track or subsession.
        When that changes, everything measured is about somewhere else: lap
        times feed the representative lap behind laps-remaining, the fuel
        burn behind every strategy call, and the oil and water baselines
        behind "down half a litre". Carried across a track change they are
        not stale readings, they are confident wrong ones.
        """
        restarted = self._session_clock_restarted()
        track = self._session("WeekendInfo", {}) or {}
        identity = (track.get("SubSessionID"), track.get("SessionID"),
                    track.get("TrackID"), track.get("TrackDisplayName"))
        if identity == (None, None, None, None):
            if restarted:
                logger.info("Session clock reset — dropping history from the last run")
                self._drop_session_history()
            return                       # nothing to go on yet
        if self._session_identity is None:
            self._session_identity = identity
            return
        if identity == self._session_identity and not restarted:
            return

        if identity != self._session_identity:
            logger.info("Session changed (%s -> %s) — dropping history from the last one",
                        self._session_identity[3], identity[3])
        else:
            logger.info("Session clock reset at %s — dropping history from the last run",
                        identity[3])
        self._session_identity = identity
        self._drop_session_history()

    def _session_clock_restarted(self) -> bool:
        """A same-segment clock reset is a new offline/AI run.

        Offline IDs can all be zero and an AI restart leaves SessionNum
        unchanged. SessionTime and SessionTick are monotonic within that
        segment, so either moving backwards is the structural signal the
        YAML cannot provide. Replay scrubbing moves backwards deliberately
        and must not erase live history.
        """
        if bool(self._get("IsReplayPlaying", False)):
            return False
        session_num = _int_or(self._get("SessionNum"), None)
        session_time = _non_negative(self._get("SessionTime"))
        session_tick = _count(self._get("SessionTick"))
        if session_time is None and session_tick is None:
            return False

        current = (session_num, session_time, session_tick)
        previous = getattr(self, "_session_clock", None)
        self._session_clock = current
        if previous is None or previous[0] != session_num:
            return False

        time_reset = (previous[1] is not None and session_time is not None
                      and session_time < previous[1] - 1.0)
        tick_reset = (previous[2] is not None and session_tick is not None
                      and session_tick < previous[2])
        return time_reset or tick_reset

    def _drop_session_history(self) -> None:
        """Clear every measurement whose meaning belongs to one run."""
        self._session_generation = getattr(self, "_session_generation", 0) + 1
        self._laps.clear()
        self._lap_marker = None
        self._session_num = None
        self._segment_restarted = False
        self._grid_latched = None
        self._grid_latched_class = None
        self._lap_dirty = False
        self._lap_dirty_reason = ""
        self._last_lap_clean = True
        self._fuel_marker = None
        self._fuel_minimum = None
        self._fuel_marker_is_partial = True
        self._fuel_usage.clear()
        self._last_lap_fuel = None
        self._last_lap_fuel_clean = None
        self._stint_start_fuel = None
        self._gap_hist_ahead.clear()
        self._gap_hist_behind.clear()
        getattr(self, "_closing_who", {}).clear()
        self._prev_lap.clear()
        self._sector_prev.clear()
        self._sector_marks.clear()
        self._sector_times.clear()
        self._sector_dirty.clear()
        self._sector_best = []
        self._pit_lap = 0
        self._oil_level_start = None
        self._water_level_start = None
        # Tyre staleness is judged by watching a signature change; the old
        # session's signature would read as "these never move".
        self._tyre_signature = None
        self._tyre_changed_at = 0.0

    def _force_session_reparse(self) -> None:
        """Make pyirsdk drop its own parsed session YAML.

        Clearing our cache is not enough on its own. pyirsdk keeps a parsed
        copy per key and only discards it when the counter goes *up*:

            if self.last_session_info_update < self._header.session_info_update:

        iRacing rebuilds the header when a session is torn down and
        recreated — changing track, or editing the AI roster — and the
        counter starts low again. A strict less-than never fires, so every
        later read returns the previous session's YAML: the old track name,
        and the old driver list paired against live car indices, which is
        how the wrong names and numbers appear against the right positions.

        Our own check is `!=`, so it catches the reset; this makes the layer
        underneath catch it too.
        """
        try:
            self._ir.last_session_info_update = -1
        except Exception:                             # noqa: BLE001
            # A stale name is a wrong answer, not a crash — log and carry on.
            logger.warning("Could not force a session re-parse; "
                           "track and driver names may be stale")

    def _session(self, key: str, default: Any = None) -> Any:
        """Read a session-info (YAML) key, cached until iRacing republishes it.

        Parsing this blob costs far more than reading a telemetry channel,
        so doing it per poll throttled the whole snapshot rate.
        """
        try:
            update = int(self._ir.session_info_update)
        except Exception:
            update = self._session_update

        if update != self._session_update:
            self._session_cache.clear()
            self._session_update = update
            self._force_session_reparse()

        if key in self._session_cache:
            return self._session_cache[key]

        value = self._get(key, default)
        # pyirsdk can return None while an async YAML parse is still running;
        # don't cache that, or the value never appears.
        if value:
            self._session_cache[key] = value
        return value

    def _read_snapshot(self) -> TelemetrySnapshot:
        # Before anything is measured, check the measurements still belong
        # to the session we are in.
        self._check_session_identity()

        weekend = self._session("WeekendInfo", {}) or {}
        # iRacing authors track names, not drivers, but it costs nothing to
        # put every string that reaches the prompt through the same door.
        track_name = _roster_text(
            weekend.get("TrackDisplayName") or weekend.get("TrackName"))

        # Order matters: the lap recorder decides whether the lap just
        # completed was clean, and the fuel average needs that verdict.
        # Sectors go first — the crossing that ends a lap is the same one
        # that starts the next, and it has to be timed before LapCompleted
        # moves underneath it.
        self._update_sectors()
        self._update_laps()
        self._update_fuel_usage()
        ahead, behind = self._rivals()
        standings, field_size, fastest = self._standings()
        grid, grid_class = self._grid_position()
        results = self._session_results()
        player = self._player()

        track = TrackConfig(
            race_laps=_count(self._get("RaceLaps")),
            current_lap=int(self._get("Lap", 0) or 0),
            laps_remaining=self._laps_remaining(),
            laps_total=self._laps_total(),
            track_name=track_name,
            session_type=self._session_type(),
            session_num=self._session_num,
            session_id=_count(weekend.get("SessionID")),
            subsession_id=_count(weekend.get("SubSessionID")),
            session_generation=self._session_generation,
            field=standings, field_size=field_size, fastest_lap=fastest,
            session_state=_SESSION_STATES.get(self._get("SessionState"), ""),
            driver_name=player.get("name", ""),
            driver_short_name=player.get("short_name", ""),
            driver_car_number=player.get("car_number", ""),
            team_name=player.get("team", ""),
            car_class_id=player.get("class_id"),
            car_class_name=player.get("class_name", ""),
            car_class_est_lap_time=player.get("class_est_lap_time"),
            car_id=player.get("car_id"),
            car_model=player.get("car_model", ""),
            car_model_short=player.get("car_model_short", ""),
            car_path=player.get("car_path", ""),
            finished=self._finished(),
            fixed_setup=self._fixed_setup(),
            laps=self._lap_history(),
            grid_position=grid,
            grid_class_position=grid_class,
            laps_led=results["laps_led"],
            caution_flags=results["caution_flags"],
            caution_laps=results["caution_laps"],
            lead_changes=results["lead_changes"],
            time_remaining=_duration(self._get("SessionTimeRemain")),
            time_total=_duration(self._get("SessionTimeTotal")),
        )

        per_lap = self._fuel_per_lap()
        levels = self._fluid_levels()
        car_progress = float(self._get("LapDistPct", 0.0) or 0.0)
        laps_estimated = track.laps_remaining is None and track.time_remaining is not None
        laps_to_go, fuel_needed, fuel_margin = self._strategy(
            per_lap, self._get("FuelLevelPct"), track)
        fuel_best_margin = None
        fuel_conservative_margin = None
        fuel_needed_conservative = None
        fuel_extra_lap_margin = None
        if laps_to_go is not None and self._fuel_usage:
            have_litres = _non_negative(self._get("FuelLevel"))
            if have_litres is not None:
                low_burn = min(self._fuel_usage)
                high_burn = max(self._fuel_usage)
                fuel_best_margin = self._fuel(have_litres - laps_to_go * low_burn)
                fuel_conservative_margin = self._fuel(
                    have_litres - laps_to_go * high_burn)
                fuel_needed_conservative = self._fuel(laps_to_go * high_burn)
                if laps_estimated:
                    fuel_extra_lap_margin = self._fuel(
                        have_litres - (laps_to_go + 1.0) * high_burn)

        # One call, so the overall and class figures cannot disagree.
        live_position, live_class_position = self._live_positions()
        if self._race_not_started() and grid is not None:
            # Cars are still leaving their stalls or forming rows. Their
            # physical lap-distance order is not a race position; the race
            # starts from the published grid slot.
            live_position = grid
            live_class_position = grid_class or live_class_position
        ahead_trend = self._closing_trend(
            self._gap_hist_ahead, ahead.trend_gap if ahead else None,
            _who(ahead), track.current_lap, car_progress)
        behind_trend = self._closing_trend(
            self._gap_hist_behind, behind.trend_gap if behind else None,
            _who(behind), track.current_lap, car_progress)
        car = CarTelemetry(
            laps_to_go=laps_to_go,
            laps_to_go_is_estimate=laps_estimated,
            fuel_needed=fuel_needed,
            fuel_margin=fuel_margin,
            fuel_needed_conservative=fuel_needed_conservative,
            fuel_margin_best=fuel_best_margin,
            fuel_margin_conservative=fuel_conservative_margin,
            fuel_extra_lap_margin=fuel_extra_lap_margin,
            closing_ahead=ahead_trend["rate"],
            closing_behind=behind_trend["rate"],
            closing_ahead_status=ahead_trend["status"],
            closing_behind_status=behind_trend["status"],
            closing_ahead_seconds=ahead_trend["seconds"],
            closing_behind_seconds=behind_trend["seconds"],
            closing_ahead_change=ahead_trend["change_seconds"],
            closing_behind_change=behind_trend["change_seconds"],
            closing_ahead_segments=ahead_trend["segments"],
            closing_behind_segments=behind_trend["segments"],
            # Live track order during a green race, iRacing's classification
            # in practice, qualifying and after the flag. _live_positions()
            # carries the reasoning; the short version is that the
            # classification only moves at start/finish, so reporting it
            # meant the engineer said P2 to a driver who had just taken the
            # lead. Both position and class_position come from the one call
            # so they cannot disagree in a single-class field.
            position=live_position,
            class_position=live_class_position,
            on_track_position=live_position,
            official_position=int(self._get("PlayerCarPosition", 0) or 0),
            speed=float(self._get("Speed", 0.0) or 0.0) * 3.6,  # m/s → km/h
            rpm=int(self._get("RPM", 0) or 0),
            gear=_gear_label(self._get("Gear")),

            # iRacing reports remaining tread across the L/M/R strips (0–1).
            tire_fl_wear=self._tread_pct("LF"),
            tire_fr_wear=self._tread_pct("RF"),
            tire_rl_wear=self._tread_pct("LR"),
            tire_rr_wear=self._tread_pct("RR"),

            tire_fl_temp=self._tread_temp("LF"),
            tire_fr_temp=self._tread_temp("RF"),
            tire_rl_temp=self._tread_temp("LR"),
            tire_rr_temp=self._tread_temp("RR"),

            # iRacing has no per-tyre age channel; stint length is the proxy.
            tyre_data_live=self._tyre_fresh(),
            tyre_fl=self._tyre_state("LF"),
            tyre_fr=self._tyre_state("RF"),
            tyre_rl=self._tyre_state("LR"),
            tyre_rr=self._tyre_state("RR"),

            tire_fl_age=self._stint_laps(),
            tire_fr_age=self._stint_laps(),
            tire_rl_age=self._stint_laps(),
            tire_rr_age=self._stint_laps(),

            # The SDK exposes brake LINE PRESSURE (bar), never temperature.
            # These stay None so the engineer says it has no reading rather
            # than reporting a fabricated zero.
            brake_fl_temp=None,
            brake_fr_temp=None,
            brake_rl_temp=None,
            brake_rr_temp=None,
            brake_pressure_fl=_positive(self._get("LFbrakeLinePress")),
            brake_pressure_fr=_positive(self._get("RFbrakeLinePress")),

            fuel_level_pct=float(self._get("FuelLevelPct", 0.0) or 0.0) * 100.0,
            fuel_remaining_laps=self._fuel_laps(),
            fuel_unit=self._fuel_unit(),
            units=self._unit_system(),
            # `or -1` here was a bug: PlayerCarIdx is legitimately 0, which
            # is falsy, so the driver's own sectors were looked up under -1
            # and came back empty for every car index 0 — which is most of
            # them. Zero is a reading, not a missing value.
            sector_times=list(self._sector_times.get(_int_or(
                self._get("PlayerCarIdx"), -1), [])),
            sector_bests=list(self._sector_best),
            fuel_per_lap=self._fuel(self._fuel_per_lap()),
            fuel_burn_samples=len(self._fuel_usage),
            fuel_burn_min=self._fuel(min(self._fuel_usage)) if self._fuel_usage else None,
            fuel_burn_max=self._fuel(max(self._fuel_usage)) if self._fuel_usage else None,
            fuel_last_lap=self._fuel(self._last_lap_fuel),
            fuel_last_lap_clean=self._last_lap_fuel_clean,
            fuel_used_stint=self._fuel(self._stint_used()),
            fuel_level=self._fuel(_non_negative(self._get("FuelLevel"))),
            fuel_capacity=self._fuel(self._fuel_capacity()),

            ahead=ahead,
            behind=behind,

            lap_progress=car_progress,
            stint_laps=self._stint_laps(),
            # iRacing publishes -1 for "no valid lap yet". Passing that
            # through is what made the engineer report negative lap times.
            best_lap_time=_positive(self._get("LapBestLapTime")),
            # Live elapsed time on the current lap. This was previously wired
            # to LapLastLapTime, which only changes once a lap.
            current_lap_time=_positive(self._get("LapCurrentLapTime")),
            last_lap_time=_positive(self._get("LapLastLapTime")),
            # Deltas may legitimately be negative (faster), so _positive is
            # the wrong filter — only reject the literal "no data" case.
            delta_to_best=_delta(self._get("LapDeltaToBestLap"),
                                 self._get("LapDeltaToBestLap_OK")),
            delta_to_session_best=_delta(self._get("LapDeltaToSessionBestLap"),
                                         self._get("LapDeltaToSessionBestLap_OK")),

            track_surface_temp=float(self._get("TrackTempCrew", 0.0) or 0.0),
            air_temp=float(self._get("AirTemp", 0.0) or 0.0),
            rain_intensity=float(self._get("Precipitation", 0.0) or 0.0),

            flag=_decode_flag(self._get("SessionFlags", 0)),
            penalty=_decode_penalty(self._get("SessionFlags", 0)),
            # Integer index only — iRacing publishes no compound
            # names anywhere, so this can say whether two cars are
            # on the same tyre but never what that tyre is called.
            tyre_compound=_count(self._get("PlayerTireCompound")),

            # Every channel below is read through _get(), which returns None
            # for anything this build of iRacing does not publish. A missing
            # channel therefore costs nothing — it just never renders.
            incidents=_count(self._get("PlayerCarMyIncidentCount")),
            team_incidents=_count(self._get("PlayerCarTeamIncidentCount")),
            incident_limit=self._incident_limit(),
            tow_time=_positive(self._get("PlayerCarTowTime")),
            # Damage is expressed as outstanding repair time, not a percentage.
            repair_required=_positive(self._get("PitRepairLeft")),
            repair_optional=_positive(self._get("PitOptRepairLeft")),
            fast_repair_used=_count(self._get("PlayerFastRepairsUsed"))
                             or _count(self._get("FastRepairUsed")),
            fast_repair_available=_fast_repairs(self._get("FastRepairAvailable")),
            pit_service_pending=bool(self._get("PitSvFlags", 0)),
            on_pit_road=bool(self._get("OnPitRoad", False)),
            in_pit_stall=bool(self._get("PlayerCarInPitStall", False)),
            pits_open=_optional_bool(self._get("PitsOpen")),
            tire_sets_available=self._tire_sets_available(),
            push_to_pass_count=_count(self._get("P2P_Count")),
            push_to_pass_active=_optional_bool(self._get("P2P_Status")),
            joker_laps_remaining=_count(self._get("SessionJokerLapsRemain")),
            on_joker_lap=_optional_bool(self._get("SessionOnJokerLap")),
            pit_autofill_active=_optional_bool(self._get("dpFuelAutoFillActive")),
            pit_service_requests=self._pit_service_requests(),
            pit_tire_compound=_count(self._get("PitSvTireCompound")),

            oil_level=levels[0],
            water_level=levels[1],
            oil_level_drop=levels[2],
            water_level_drop=levels[3],
            water_temp=_positive(self._get("WaterTemp")),
            oil_temp=_positive(self._get("OilTemp")),
            oil_pressure=_positive(self._get("OilPress")),
            fuel_pressure=_positive(self._get("FuelPress")),
            voltage=_positive(self._get("Voltage")),

            track_wetness=_TRACK_WETNESS.get(self._get("TrackWetness")),
            warnings=_decode_warnings(self._get("EngineWarnings")),
            track_surface=self._track_surface(),
            pit_service_status=_enum_map("PitSvStatus", {0: "none"}).get(
                self._get("PlayerCarPitSvStatus")),
            avg_lap_time=_positive(self._get("LapLastNLapTime")),
            # m/s -> km/h; the rest are read as published.
            wind_speed=(lambda v: v * 3.6 if v is not None else None)(
                _positive(self._get("WindVel"))),
            wind_direction=_bearing(self._get("WindDir")),
            humidity=(lambda v: v * 100 if v is not None else None)(
                _positive(self._get("RelativeHumidity"))),
            # Published in Pa (101325); hPa is the unit any weather report uses.
            air_pressure=(lambda v: round(v / 100.0, 1) if v is not None else None)(
                _positive(self._get("AirPressure"))),
            air_density=_positive(self._get("AirDensity")),
            skies=_SKIES.get(self._get("Skies")),
            fog=(lambda v: v * 100 if v is not None else None)(
                _positive(self._get("FogLevel"))),
            declared_wet=(lambda v: bool(v) if v is not None else None)(
                self._get("WeatherDeclaredWet")),
            time_of_day=_clock_time(self._get("SessionTimeOfDay")),
        )

        return TelemetrySnapshot(car, track)

    def _tread_pct(self, corner: str) -> Optional[float]:
        """Mean remaining tread across the three strips, as a percentage."""
        strips = [self._get(f"{corner}wear{s}") for s in ("L", "M", "R")]
        mean = _mean(strips)
        # No channel at all: report unknown rather than "0% tread left".
        return None if mean is None else mean * 100.0

    def _tread_temp(self, corner: str) -> Optional[float]:
        """Mean carcass temperature across the three strips, in Celsius."""
        return _mean(
            [self._get(f"{corner}tempCL"), self._get(f"{corner}tempCM"), self._get(f"{corner}tempCR")]
        )

    def _fluid_levels(self) -> Tuple[Optional[float], Optional[float],
                                     Optional[float], Optional[float]]:
        """Oil and coolant level, and how much has been lost.

        Both are litres. The absolute number means little without knowing
        what the car started with, whereas "down half a litre" is a leak.
        """
        oil = _non_negative(self._get("OilLevel"))
        water = _non_negative(self._get("WaterLevel"))
        if oil is not None and self._oil_level_start is None:
            self._oil_level_start = oil
        if water is not None and self._water_level_start is None:
            self._water_level_start = water

        def drop(now, start):
            if now is None or start is None:
                return None
            lost = start - now
            # Only a loss is interesting, and only past sensor noise.
            return round(lost, 2) if lost > 0.05 else None

        return (oil, water,
                drop(oil, self._oil_level_start),
                drop(water, self._water_level_start))

    def _tyre_fresh(self) -> bool:
        """Whether the tyre readings have moved recently.

        Frozen values are not a failure to report — they are last-stop data,
        which is useful if labelled and misleading if not.
        """
        sig = tuple(
            round(float(self._get(f"{c}tempC{p}") or -1), 2)
            for c in ("LF", "RF", "LR", "RR") for p in ("L", "M", "R")
        )
        now = time.monotonic()
        if self._tyre_signature is None:
            self._tyre_signature = sig
            self._tyre_changed_at = now
            return True
        if sig != self._tyre_signature:
            self._tyre_signature = sig
            self._tyre_changed_at = now
        # A green lap is well under two minutes; unchanged for longer than
        # that on track means the feed is not updating.
        return (now - self._tyre_changed_at) < 150.0

    def _tyre_state(self, corner: str) -> Optional[TyreState]:
        """The three tread strips, mapped from the SDK's frame to the car's.

        iRacing names the strips L / M / R across the tyre as seen from a
        fixed direction, so "L" is the outboard edge on the left-hand tyres
        and the inboard edge on the right-hand ones. Reading them as
        inner/outer without that flip inverts every camber call on one side
        of the car.
        """
        left_side = corner in ("LF", "LR")
        inner_key, outer_key = ("R", "L") if left_side else ("L", "R")

        def temp(strip: str) -> Optional[float]:
            return _positive(self._get(f"{corner}tempC{strip}"))

        def wear(strip: str) -> Optional[float]:
            value = self._get(f"{corner}wear{strip}")
            try:
                return round(float(value) * 100.0, 1)
            except (TypeError, ValueError):
                return None

        state = TyreState(
            inner_temp=temp(inner_key), middle_temp=temp("M"), outer_temp=temp(outer_key),
            inner_wear=wear(inner_key), middle_wear=wear("M"), outer_wear=wear(outer_key),
        )
        # All six channels absent means this car does not publish them.
        if all(v is None for v in vars(state).values()):
            return None
        return state

    def _stint_laps(self) -> int:
        """Laps since leaving the pits.

        iRacing has no PitLapCompleted channel (verified against a live
        session), so the lap count at each pit exit is tracked here instead.
        """
        completed = int(self._get("LapCompleted", 0) or 0)
        on_pit_road = bool(self._get("OnPitRoad", False))

        if on_pit_road:
            self._pit_lap = completed
        return max(0, completed - self._pit_lap)

    def _incident_limit(self) -> Optional[int]:
        """Per-session incident cap from the session YAML, when there is one."""
        weekend = self._session("WeekendInfo", {}) or {}
        opts = weekend.get("WeekendOptions") or {}
        raw = opts.get("IncidentLimit")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None   # iRacing writes "unlimited" when uncapped

    def _fixed_setup(self) -> bool:
        """Whether the series has locked the car setup.

        `WeekendInfo:WeekendOptions:IsFixedSetup`, per the SDK YAML docs.
        Plenty of series run fixed, and there the engineer telling a driver
        to take camber out is advice they cannot act on — worse than
        useless at speed, because they will go looking for the setting.
        """
        weekend = self._session("WeekendInfo", {}) or {}
        opts = weekend.get("WeekendOptions") or {}
        return bool(_int_or(opts.get("IsFixedSetup"), 0))

    def _current_session(self) -> Dict[str, Any]:
        """The SessionInfo entry for the segment running right now.

        SessionInfo lists every segment of the weekend — practice, qualifying,
        race — and SessionNum says which one is live. Four callers now need
        that entry, so the walk lives here rather than being repeated with
        four chances to disagree about which session is current.
        """
        info = self._session("SessionInfo", {}) or {}
        current = self._get("SessionNum", 0)
        for entry in info.get("Sessions") or []:
            if entry.get("SessionNum") == current:
                return entry or {}
        return {}

    def _session_type(self) -> str:
        """Race / Practice / Qualify for the session currently running."""
        return str(self._current_session().get("SessionType") or "")

    def _laps_total(self) -> Optional[int]:
        """Scheduled race distance, when the session is lap-limited."""
        laps = self._current_session().get("SessionLaps")
        # iRacing writes the string "unlimited" for timed sessions.
        try:
            return int(laps)
        except (TypeError, ValueError):
            return None

    def _player_idx(self) -> Optional[int]:
        """The driver's own car index, or None when it is not published.

        PlayerCarIdx is legitimately 0 — the first entry in the field — so
        this cannot be written as `or -1`. That exact slip looked up the
        driver's own sectors under index -1 and made them unanswerable for
        months.
        """
        idx = self._get("PlayerCarIdx", -1)
        if idx is None or int(idx) < 0:
            return None
        return int(idx)

    def _grid_position(self) -> Tuple[Optional[int], Optional[int]]:
        """Where the race started from, overall and in class.

        Two sources, in order:

        1. ``QualifyResultsInfo:Results`` — the qualifying classification,
           which is the grid for a standard race. Carries CarIdx, Position
           and ClassPosition per the SDK YAML docs.
        2. Latched at green, when there was no qualifying session to publish.
           A race gridded from a heat, from championship order, or joined in
           progress has no qualifying result; AI races and Test Drive may
           have none either. None of that has been checked against a live
           session, so it is handled as "no grid" rather than assumed.

        Returns (None, None) when neither produced one. That is the answer.
        Substituting the position at the end of lap one would be a plausible
        number arrived at by inference, which is the failure this whole
        feature exists to avoid.
        """
        idx = self._player_idx()
        if idx is not None:
            results = (self._session("QualifyResultsInfo", {}) or {}).get("Results") or []
            for entry in results:
                if _int_or(entry.get("CarIdx"), -1) != idx:
                    continue
                # QualifyResultsInfo positions are zero-based. Convert them
                # at the YAML boundary; the live PlayerCar*Position channels
                # used by the fallback below are already one-based.
                overall_raw = _count(entry.get("Position"))
                in_class_raw = _count(entry.get("ClassPosition"))
                overall = overall_raw + 1 if overall_raw is not None else None
                in_class = in_class_raw + 1 if in_class_raw is not None else None
                if overall is not None:
                    return overall, in_class

        # No qualifying result. Take the classification once, at the moment
        # the race actually starts, and keep it.
        if self._grid_latched is None:
            racing = _SESSION_STATES.get(self._get("SessionState"), "") == "Racing"
            if racing and session_kind(self._session_type()) == "race":
                latched = _count(self._get("PlayerCarPosition"))
                if latched and latched >= 1:
                    self._grid_latched = latched
                    self._grid_latched_class = _count(self._get("PlayerCarClassPosition")) or None
        return self._grid_latched, self._grid_latched_class

    def _session_results(self) -> Dict[str, Optional[int]]:
        """Published results for the segment running, from the session YAML.

        Facts iRacing states outright, so none of this needs an event log:
        how many laps the driver led, how many cautions there were, how many
        times the lead changed. Whether these are filled in live or only once
        the session is official is NOT verified — if they turn out to be
        end-of-session only, the mid-race answer simply omits them, which the
        caller already handles.

        Leading zero laps is a fact, not a missing channel, so these go
        through _count (which keeps 0) and never _positive (which would drop
        it — the same slip that made the leader's gap permanently
        unavailable).
        """
        entry = self._current_session()
        results = {
            "laps_led": None,
            "caution_flags": _count(entry.get("ResultsNumCautionFlags")),
            "caution_laps": _count(entry.get("ResultsNumCautionLaps")),
            "lead_changes": _count(entry.get("ResultsNumLeadChanges")),
        }
        idx = self._player_idx()
        if idx is not None:
            for row in entry.get("ResultsPositions") or []:
                if _int_or(row.get("CarIdx"), -1) == idx:
                    results["laps_led"] = _count(row.get("LapsLed"))
                    break
        return results

    def _laps_remaining(self) -> Optional[int]:
        """Laps left, or None for a timed session.

        Previously the "unlimited" sentinel was collapsed to 0, which the
        engineer read back as "no laps remaining".
        """
        value = self._get("SessionLapsRemainEx")
        if value is None:
            value = self._get("SessionLapsRemain")
        try:
            laps = int(value)
        except (TypeError, ValueError):
            return None
        if laps < 0 or laps > 10000:  # sentinel for "not lap limited"
            return None
        return laps

    def _imperial(self) -> bool:
        """Whether the driver's sim is showing imperial units.

        DisplayUnits: 0 = english, 1 = metric. Absent or unreadable, assume
        metric, which is iRacing's default and the SDK's native unit.
        """
        try:
            return int(self._get("DisplayUnits", 1)) == 0
        except (TypeError, ValueError):
            return False

    def _tire_sets_available(self) -> Dict[str, Any]:
        """Published set counts, preserving iRacing's 255=unlimited sentinel."""
        channels = (
            ("total", "TireSetsAvailable"),
            ("front", "FrontTireSetsAvailable"),
            ("rear", "RearTireSetsAvailable"),
            ("left", "LeftTireSetsAvailable"),
            ("right", "RightTireSetsAvailable"),
        )
        result: Dict[str, Any] = {}
        for label, channel in channels:
            count = _count(self._get(channel))
            if count is None:
                continue
            result[label] = "unlimited" if count == 255 else count
        return result

    def _pit_service_requests(self) -> List[str]:
        """Decode PitSvFlags through pyirsdk's runtime enum values."""
        try:
            bits = int(self._get("PitSvFlags", 0) or 0)
        except (TypeError, ValueError):
            return []
        labels = {
            "lf tire change": "left front tyre",
            "rf tire change": "right front tyre",
            "lr tire change": "left rear tyre",
            "rr tire change": "right rear tyre",
            "fuel fill": "fuel",
            "windshield tearoff": "windscreen tear-off",
            "fast repair": "fast repair",
        }
        return [labels.get(name, name) for bit, name in sorted(
            _enum_map("PitSvFlags", {}).items()) if bits & bit]

    def _unit_system(self) -> str:
        """"metric" or "imperial", for everything that is not fuel.

        "auto" follows the sim, so the engineer reads back the same units
        the driver is already looking at. Conversion happens where these
        become words, not here: no calculator uses a temperature, a speed or
        a gap in metres, so keeping the snapshot in one system means the
        arithmetic can never end up mixing two.
        """
        choice = str(self._units_setting or "auto").lower()
        if choice in ("imperial", "english", "us"):
            return "imperial"
        if choice in ("metric", "si"):
            return "metric"
        return "imperial" if self._imperial() else "metric"

    def _fuel_density(self) -> float:
        """Fuel mass per litre, for cars whose dashboard reads in kilograms."""
        info = self._session("DriverInfo", {}) or {}
        try:
            density = float(info.get("DriverCarFuelKgPerLtr") or 0.0)
        except (TypeError, ValueError):
            return _DEFAULT_FUEL_DENSITY
        # Petrol sits near 0.75; anything far outside that is not a density.
        return density if 0.4 < density < 1.2 else _DEFAULT_FUEL_DENSITY

    def _fuel_unit(self) -> str:
        """What to report fuel in.

        iRacing publishes litres and says whether the UI is metric, but the
        car's own dashboard may show mass instead and nothing announces that.
        So "auto" follows the sim, and the driver can override to kilograms.
        """
        choice = str(self._fuel_units_setting or "auto").lower()
        if choice in ("kg", "kilograms", "kilos"):
            return "kilograms"
        if choice in ("gallons", "gal", "imperial"):
            return "gallons"
        if choice in ("litres", "liters", "l", "metric"):
            return "litres"
        return "gallons" if self._imperial() else "litres"

    def _fuel(self, litres: Optional[float]) -> Optional[float]:
        """Convert a litre figure into whatever the driver is reading."""
        if litres is None:
            return None
        unit = self._fuel_unit()
        if unit == "gallons":
            return round(litres / _LITRES_PER_US_GALLON, 3)
        if unit == "kilograms":
            return round(litres * self._fuel_density(), 3)
        return round(litres, 3)

    def _track_surface(self) -> Optional[str]:
        """Where the car is: on track, off it, or in the pit lane."""
        return _enum_map("TrkLoc", {
            -1: "not in world", 0: "off track", 1: "in pit stall",
            2: "approaching pits", 3: "on track",
        }).get(self._get("PlayerTrackSurface"))

    def _sector_bounds(self) -> List[float]:
        """Sector start percentages for this track, from the session YAML.

        iRacing publishes no sector *time* channel, but it does publish the
        boundaries — SplitTimeInfo:Sectors, each with a SectorStartPct. The
        count varies by track, so nothing here assumes three.

        CrewChief does not use this: its iRacing sector handling hardcodes
        thirds of lap distance. Reading the real boundaries is why these
        times can be compared against the sim's own display at all.
        """
        info = self._session("SplitTimeInfo", {}) or {}
        bounds = []
        for sector in info.get("Sectors", []) or []:
            try:
                pct = float(sector["SectorStartPct"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 <= pct < 1.0:
                bounds.append(pct)
        bounds = sorted(set(bounds))
        # A single boundary at 0 is just the start/finish line, which is the
        # lap timer we already have — not sectors.
        return bounds if len(bounds) >= 2 else []

    def _update_sectors(self) -> None:
        """Time every car through the sector boundaries.

        Called each poll. The crossing time is **interpolated** between the
        two samples either side of the boundary, not taken as the time of
        whichever poll first read past it. At 10 Hz a car covers one to two
        percent of a lap between polls, so snapping to a sample would put
        every sector out by up to a tenth — useless against sector times
        that differ by hundredths, and exactly the kind of confident wrong
        number this app exists to avoid.
        """
        bounds = self._sector_bounds()
        if not bounds:
            return
        now = _positive(self._get("SessionTime"))
        if now is None:
            return

        pcts = self._get("CarIdxLapDistPct") or []
        pits = self._get("CarIdxOnPitRoad") or []
        surfaces = self._get("CarIdxTrackSurface") or []

        for idx, raw in enumerate(pcts):
            try:
                pct = float(raw)
            except (TypeError, ValueError):
                continue
            if pct < 0:                       # in the garage, not on track
                self._sector_prev.pop(idx, None)
                continue

            previous = self._sector_prev.get(idx)
            self._sector_prev[idx] = (pct, now)

            # A lap in the pits or off the road is not a reference lap.
            try:
                if bool(pits[idx]) or int(surfaces[idx]) == 0:
                    self._sector_dirty[idx] = True
            except (IndexError, TypeError, ValueError):
                pass

            if previous is None:
                continue
            prev_pct, prev_t = previous
            if now <= prev_t:                 # session time went backwards
                continue

            if pct < prev_pct:
                # Crossed the start/finish line between the two samples.
                # Interpolate across the wrap by treating this sample as
                # sitting past 1.0, so the line time is as accurate as any
                # other boundary rather than snapped to a poll.
                span = (pct + 1.0) - prev_pct
                at_line = (prev_t + (1.0 - prev_pct) / span * (now - prev_t)
                           if span > 0 else now)
                # Anything still ahead of the car belongs to the old lap.
                self._cross(idx, bounds, prev_pct, prev_t, 1.0, at_line)
                self._finish_lap(idx, bounds, at_line)
                # The line is boundary 0 of the lap now starting.
                self._sector_marks.setdefault(idx, {})[0] = at_line
                self._cross(idx, bounds, 0.0, at_line, pct, now)
            else:
                self._cross(idx, bounds, prev_pct, prev_t, pct, now)

    def _cross(self, idx, bounds, p0, t0, p1, t1):
        """Record any boundary strictly between p0 and p1.

        Boundary 0 is the start/finish line and is never marked here — it
        only ever happens on the wrap, where the caller interpolates it
        across the discontinuity.
        """
        span = p1 - p0
        for n, bound in enumerate(bounds):
            if n == 0 or not (p0 < bound <= p1):
                continue
            at = t0 + (bound - p0) / span * (t1 - t0) if span > 0 else t1
            self._sector_marks.setdefault(idx, {})[n] = at

    def _finish_lap(self, idx, bounds, at_line: float) -> None:
        """Turn this lap's crossings into sector times.

        Sector n runs from boundary n to boundary n+1; the last one runs to
        the line, which is why the line time has to be passed in rather than
        looked up — it belongs to the lap that is starting, not this one.
        """
        marks = self._sector_marks.pop(idx, {})
        dirty = self._sector_dirty.pop(idx, False)
        times: List[Optional[float]] = []
        for n in range(len(bounds)):
            start = marks.get(n)
            stop = marks.get(n + 1) if n + 1 < len(bounds) else at_line
            times.append(round(stop - start, 3)
                         if start is not None and stop is not None and stop > start
                         else None)
        if not any(t is not None for t in times):
            return
        self._sector_times[idx] = times
        try:
            me = int(self._get("PlayerCarIdx", -1))
        except (TypeError, ValueError):
            me = -1
        # Bests come from clean laps only — an off or a pit lap through one
        # sector would otherwise set a "best" nobody can repeat.
        if idx == me and not dirty and all(t is not None for t in times):
            self._note_best_sectors(times)

    def _note_best_sectors(self, times: List[Optional[float]]) -> None:
        """Personal best per sector, from clean laps only.

        Kept apart from the best lap time: the quickest lap is rarely the
        best in every sector, and the gap between the two is exactly what
        says how much is still on the table.
        """
        if len(self._sector_best) != len(times):
            self._sector_best = [None] * len(times)
        for n, t in enumerate(times):
            if t is None:
                continue
            if self._sector_best[n] is None or t < self._sector_best[n]:
                self._sector_best[n] = t

    def _update_laps(self) -> None:
        """Record each completed lap, flagging the ones not worth trusting.

        Called every poll. The dirty flags accumulate across the lap so a
        single moment off-track or on pit road disqualifies the whole lap —
        by the time it is recorded the car is long since back on the island.
        """
        completed = int(self._get("LapCompleted", 0) or 0)
        session_num = _int_or(self._get("SessionNum"), None)

        # A new segment. The history is kept — the fuel and pace baselines
        # are worth carrying across — but the marker is re-baselined, because
        # if LapCompleted restarts here then `completed <= self._lap_marker`
        # would be true for the rest of the race and nothing would ever be
        # recorded again. The boundary lands while the driver is on the grid
        # or in the pits, so no racing lap is lost to the re-baseline.
        if session_num != self._session_num:
            if self._session_num is not None:
                logger.info("Session segment %s -> %s — re-baselining the lap marker",
                            self._session_num, session_num)
                self._lap_marker = None
                # The fuel marker is baselined against LapCompleted too, and
                # cannot detect this itself — this runs first and has already
                # moved _session_num on. See _update_fuel_usage().
                self._segment_restarted = True
            self._session_num = session_num

        # Watch for anything that spoils the lap in progress.
        if bool(self._get("OnPitRoad", False)):
            self._lap_dirty, self._lap_dirty_reason = True, "pit lane"
        if self._track_surface() == "off track":
            self._lap_dirty, self._lap_dirty_reason = True, "off track"
        if _positive(self._get("PlayerCarTowTime")):
            self._lap_dirty, self._lap_dirty_reason = True, "towed"

        if self._lap_marker is None:
            self._lap_marker = completed
            return
        if completed <= self._lap_marker:
            return

        # A lap was just completed. LapLastLapTime is the one that finished.
        self._lap_marker = completed
        lap_time = _positive(self._get("LapLastLapTime"))
        dirty, reason = self._lap_dirty, self._lap_dirty_reason
        self._lap_dirty, self._lap_dirty_reason = False, ""
        self._last_lap_clean = not dirty
        if lap_time is None:
            return

        self._laps.append(LapRecord(
            lap=completed,
            time=round(lap_time, 3),
            position=_count(self._get("PlayerCarPosition")),
            fuel_left=_positive(self._get("FuelLevel")),
            clean=not dirty,
            note=reason,
            session_num=session_num,
        ))

    def _lap_history(self) -> List[LapRecord]:
        """Completed laps, with statistical outliers marked unclean.

        A lap can be slow without touching the pit lane or the grass —
        traffic, a lift for a yellow, a missed shift. Anything well off the
        median of the clean laps is excluded from trend work too, because a
        single bad lap otherwise reads as a cliff in tyre performance.
        """
        laps = list(self._laps)
        times = sorted(l.time for l in laps if l.clean)
        if len(times) >= 5:
            median = times[len(times) // 2]
            # 107% is the usual yardstick for a representative lap.
            limit = median * 1.07
            for lap in laps:
                if lap.clean and lap.time > limit:
                    lap.clean, lap.note = False, "well off the median"
        return laps

    def _update_fuel_usage(self) -> None:
        """Measure fuel burnt per lap by watching the tank across lap crossings.

        FuelUsePerHour is an *instantaneous* flow rate, not a stint average —
        sampled while idling in the pits it read 3.7 L/h, which worked out to
        330 laps of fuel when the true figure was 11. Actual consumption
        between lap crossings is the only reliable basis.
        """
        completed = int(self._get("LapCompleted", 0) or 0)
        level = float(self._get("FuelLevel", 0.0) or 0.0)
        if level <= 0:
            return

        # A new segment can restart LapCompleted, and the marker from the last
        # one then describes a different run. Left in place it does one of two
        # things, both silent: if the old count is never reached again nothing
        # is measured for the whole race and the burn stays frozen at the
        # previous session's figure; if it is passed, the first crossing beyond
        # it divides the fuel burnt across *both* segments by a couple of laps.
        # Replayed at a true 3.5 L/lap that produced a 24.5 L "lap" and told
        # the driver they had three laps of fuel when they had eleven.
        # Consumed after the level guard, so it survives the poll where the car
        # is not in the world yet and FuelLevel reads 0.
        if self._segment_restarted:
            self._segment_restarted = False
            self._fuel_marker = None
            self._fuel_minimum = None
        # The samples themselves are kept: burn measured in practice is the
        # same car on the same track, and is the only basis the opening laps
        # of the race have. It is the marker that belongs to one segment.

        if self._fuel_marker is None:
            self._fuel_marker = (completed, level)
            self._fuel_minimum = level
            self._fuel_marker_is_partial = True
            self._stint_start_fuel = level
            return

        last_completed, last_level = self._fuel_marker
        minimum = min(last_level, level) if self._fuel_minimum is None else min(
            self._fuel_minimum, level)

        # Fuel rises only when fuel is added. On pit road, compare against the
        # live low point so a small splash is not hidden below the fuel level
        # at the previous line crossing. Keep the old crossing comparison as a
        # fallback in case a slow reader misses the pit-road samples entirely.
        refuelled = ((bool(self._get("OnPitRoad", False))
                      or bool(self._get("PlayerCarInPitStall", False)))
                     and level > minimum + 0.05) \
            or level > last_level + 0.5
        if refuelled:
            self._fuel_marker = (completed, level)
            self._fuel_minimum = level
            # Refuelling happens in the pit box, mid-lap.
            self._fuel_marker_is_partial = True
            self._stint_start_fuel = level
            return

        self._fuel_minimum = minimum

        laps = completed - last_completed
        if laps <= 0:
            return

        used = (last_level - level) / laps
        # Record the real figure before any filtering decides what to predict
        # from. "How much did that lap actually cost?" is always answerable.
        if 0.0 <= used < 50.0:
            self._last_lap_fuel = round(used, 3)
            # A marker planted mid-lap, or a reader that missed more than one
            # crossing, did not measure exactly one complete lap. The number
            # remains useful as a raw measurement but cannot describe what
            # the last lap did or change the predicted range.
            self._last_lap_fuel_clean = (
                self._last_lap_clean
                if not self._fuel_marker_is_partial and laps == 1 else None)
            # The lap recorder ran earlier this poll and could not know the
            # burn yet, so fill it in on the record it just created.
            if self._laps and self._laps[-1].lap == completed:
                self._laps[-1].fuel_used = self._last_lap_fuel
        # An in-lap, an out-lap or a lap behind a safety car burns far less
        # than a racing lap. Averaged in, one of them overstated the range by
        # nearly two laps — enough to plan a stop around and be wrong.
        # Guard against garbage from resets, tows and session changes too.
        # Skip the first measurement after a marker was placed mid-lap: it
        # spans a fraction of a lap but is divided as though it were one, and
        # a single early sample sets the whole fuel picture until four more
        # arrive.
        if self._fuel_marker_is_partial:
            self._fuel_marker_is_partial = False
        elif 0.05 < used < 50.0 and self._last_lap_clean:
            self._fuel_usage.append(used)
        self._fuel_marker = (completed, level)
        self._fuel_minimum = level

    def _fuel_capacity(self) -> Optional[float]:
        """Usable tank size in litres, from the session YAML.

        There is no FuelCapacity telemetry channel — reading one returned
        None every time, which quietly disabled the pit window's earliest-lap
        arm. iRacing publishes the tank in DriverInfo, and a series fuel limit
        as a percentage of it.
        """
        info = self._session("DriverInfo", {}) or {}
        try:
            litres = float(info.get("DriverCarFuelMaxLtr") or 0.0)
        except (TypeError, ValueError):
            return None
        if litres <= 0:
            return None
        try:
            pct = float(info.get("DriverCarMaxFuelPct") or 1.0)
        except (TypeError, ValueError):
            pct = 1.0
        if 0 < pct <= 1.0:
            litres *= pct
        return round(litres, 2)

    #: One lap of burn is not a stint average. Traffic, a lift, a slow
    #: sector — any of them move a single sample enough to change a pit call,
    #: and the driver acts on that call.
    _MIN_FUEL_SAMPLES = 2

    def _fuel_per_lap(self) -> Optional[float]:
        if len(self._fuel_usage) < self._MIN_FUEL_SAMPLES:
            return None
        return sum(self._fuel_usage) / len(self._fuel_usage)

    def _representative_lap(self) -> Optional[float]:
        """A lap time worth dividing the race clock by.

        In a timed race, laps-to-go is the session clock over a lap time, so
        which lap time you pick decides how much fuel the driver is told they
        need. Using the *best* lap is optimistic in the dangerous direction:
        a qualifying lap is seconds faster than race pace, so more laps fit
        in the remaining time, the fuel requirement is inflated, and the
        engineer calls a stop the driver did not need.

        The mean of recent clean laps is what actually gets driven. Best lap
        is a last resort, and only once nothing better exists.
        """
        clean = [l.time for l in self._laps if l.clean and l.time > 0]
        if clean:
            recent = clean[-5:]
            return sum(recent) / len(recent)
        last = _positive(self._get("LapLastLapTime"))
        if last:
            return last
        return _positive(self._get("LapBestLapTime"))

    def _stint_used(self) -> Optional[float]:
        """Fuel burnt since the last refuel — measured, not predicted."""
        if self._stint_start_fuel is None:
            return None
        level = _positive(self._get("FuelLevel"))
        if level is None:
            return None
        used = self._stint_start_fuel - level
        return round(used, 3) if used >= 0 else None

    def _fuel_laps(self) -> Optional[float]:
        """Laps of fuel left, or None until a full lap has been measured.

        Reporting a number before there is any basis for it is worse than
        admitting it is unknown — the driver plans a stop on this figure.
        """
        per_lap = self._fuel_per_lap()
        if per_lap is None or per_lap <= 0:
            return None
        fuel_level = float(self._get("FuelLevel", 0.0) or 0.0)
        if fuel_level <= 0:
            return None
        return fuel_level / per_lap

    def _live_ranks(self) -> Dict[int, Tuple[int, Optional[int]]]:
        """car index -> (overall rank, class rank) from the live track order.

        Empty outside a green race, where iRacing's classification is the
        right answer: practice and qualifying order by lap time, and after
        the checkered the number is a finishing position that must not
        drift.

        This is the only place running order is decided. It used to be
        worked out here for the driver's own position and separately in
        _build_field() from CarIdxPosition, so mid-lap the prompt carried
        the live number on the position line and the classified one in the
        standings, and the model answered from whichever it happened to
        read — right sometimes, a lap stale others.
        """
        state = _SESSION_STATES.get(self._get("SessionState"), "")
        if (session_kind(self._session_type()) != "race"
                or state != "Racing" or self._finished()):
            return {}
        drivers = self._drivers()
        order = [idx for _, idx in self._live_order()]

        # Cars the session has classified but is not currently sending live
        # position for. iRacing's Max Cars setting (serverTransmitMaxCars,
        # 10-64) caps how many cars the server transmits: "you will only
        # ever be given positions for 15 cars at any one time... generally
        # the cars closest to you... over time, as you move through the
        # field, the particular set will change".
        #
        # Dropping them would renumber everyone below, and because the set
        # changes as you move, the error moves too — which reads as a
        # position that is right sometimes and wrong others rather than
        # simply wrong. They keep their classified slot instead.
        tracked = set(order)
        pinned: Dict[int, int] = {}
        for idx, pos in enumerate(self._get("CarIdxPosition") or []):
            rank = _int_or(pos)
            if not rank or rank < 1 or idx in tracked or idx not in drivers:
                continue
            pinned.setdefault(rank, idx)
        # Ascending, so an earlier insertion cannot displace a later one.
        for rank in sorted(pinned):
            order.insert(min(rank - 1, len(order)), pinned[rank])

        ranks: Dict[int, Tuple[int, Optional[int]]] = {}
        per_class: Dict[Any, int] = {}
        for rank, idx in enumerate(order, start=1):
            class_id = (drivers.get(idx) or {}).get("class_id")
            if class_id is None:
                ranks[idx] = (rank, None)
                continue
            per_class[class_id] = per_class.get(class_id, 0) + 1
            ranks[idx] = (rank, per_class[class_id])
        return ranks

    def _live_positions(self) -> Tuple[int, Optional[int]]:
        """The driver's own running order, overall and in class, 1-based.

        Falls back to the classification when the driver is not in the live
        order at all — in the garage, or a car index the session YAML does
        not know about — rather than inventing a rank.
        """
        official = int(self._get("PlayerCarPosition", 0) or 0)
        official_class = _count(self._get("PlayerCarClassPosition"))

        my_idx = self._get("PlayerCarIdx", -1)
        if my_idx is None or my_idx < 0:
            return official, official_class

        overall, in_class = self._live_ranks().get(int(my_idx), (None, None))
        if overall is None:
            return official, official_class
        return overall, (in_class or official_class)

    def _closing_trend(self, history, gap: Optional[float], who: str = "",
                       lap_number: Optional[int] = None,
                       lap_progress: Optional[float] = None) -> Dict[str, Any]:
        """Adjacent-car movement at comparable points around the lap.

        CarDistAhead/Behind is still the right displayed snapshot, but its
        short derivative produced 20+ m/s "closing rates" through braking
        zones.  The trend instead samples iRacing's CarIdxEstTime gap once per
        five percent of lap.  Two points give a qualitative early direction;
        four consecutive points confirm a measured change across three
        mini-sectors.  No physical speed or contact time is inferred.
        """
        if not hasattr(self, "_closing_who"):
            self._closing_who = {}
        if who and self._closing_who.get(id(history)) != who:
            history.clear()
            self._closing_who[id(history)] = who

        def result(status: str, seconds: float = 0.0,
                   change: Optional[float] = None,
                   segments: int = 0) -> Dict[str, Any]:
            return {
                "rate": None,
                "status": status,
                "seconds": round(seconds, 1),
                "change_seconds": None if change is None else round(change, 2),
                "segments": segments,
            }

        if gap is None:
            return result("unavailable")
        try:
            lap = int(lap_number)
            progress = min(0.999999, max(0.0, float(lap_progress)))
        except (TypeError, ValueError):
            return result("waiting")

        gates_per_lap = 20
        gate = lap * gates_per_lap + int(progress * gates_per_lap)
        now = time.monotonic()
        if history and gate < history[-1][0]:
            history.clear()
        if not history or gate > history[-1][0]:
            # A skipped gate means the samples no longer describe adjacent
            # slices of track. Start again instead of measuring across a tow,
            # disconnect or delayed poll.
            if history and gate != history[-1][0] + 1:
                history.clear()
            history.append((gate, now, float(gap)))

        samples = list(history)[-4:]
        segments = max(0, len(samples) - 1)
        seconds = samples[-1][1] - samples[0][1] if segments else 0.0
        if not segments:
            return result("waiting")

        # Positive means the timing gap shrank, whichever side the rival is
        # on. Changes below three hundredths are beneath useful radio
        # precision and are treated as noise.
        changes = [samples[n - 1][2] - samples[n][2]
                   for n in range(1, len(samples))]
        meaningful = [change for change in changes if abs(change) >= 0.03]
        total = samples[0][2] - samples[-1][2]
        if not meaningful or abs(total) < 0.05:
            return result("steady", seconds, 0.0, segments)
        if any(change > 0 for change in meaningful) \
                and any(change < 0 for change in meaningful):
            return result("fluctuating", seconds, None, segments)
        magnitudes = [abs(change) for change in meaningful]
        if len(magnitudes) >= 2 and max(magnitudes) > max(0.5, min(magnitudes) * 5.0):
            return result("fluctuating", seconds, None, segments)
        if segments < 3:
            return result("early", seconds, total, segments)
        return result("confirmed", seconds, total, segments)

    def _strategy(self, car_fuel_per_lap, fuel_pct, track) -> Tuple:
        """Laps still to run, fuel required, and the margin either way."""
        laps_to_go = None
        try:
            progress = min(1.0, max(0.0, float(self._get("LapDistPct", 0.0))))
        except (TypeError, ValueError):
            progress = 0.0
        if track.laps_remaining is not None:
            laps_to_go = max(0.0, float(track.laps_remaining) - progress)
        elif track.time_remaining is not None:
            lap_time = self._representative_lap()
            if lap_time:
                laps_to_go = max(
                    0.0,
                    math.ceil(track.time_remaining / lap_time + progress) - progress,
                )

        if laps_to_go is None or not car_fuel_per_lap:
            return laps_to_go, None, None

        # Measured in litres, reported in the driver's units — mixing the two
        # would put fuel_needed and fuel_level on different scales.
        needed = laps_to_go * car_fuel_per_lap
        have = float(self._get("FuelLevel", 0.0) or 0.0)
        return (laps_to_go,
                self._fuel(needed),
                self._fuel(have - needed))

    def _direct_gap(self, ahead: bool) -> Tuple[Optional[float], Optional[float]]:
        """Gap from CarDistAhead / CarDistBehind — metres, and seconds.

        Per the iRacing SDK variable list these are "distance to first car in
        front of / behind player, in meters". That is the number a relative
        display shows. Deriving it from CarIdxEstTime differences (what this
        used to do) compounds every per-car estimate and drifts.
        """
        metres = self._get("CarDistAhead" if ahead else "CarDistBehind")
        try:
            metres = float(metres)
        except (TypeError, ValueError):
            return None, None
        # iRacing parks this at a huge sentinel when nobody is there.
        if metres <= 0 or metres > 50000:
            return None, None

        speed = float(self._get("Speed", 0.0) or 0.0)   # m/s
        seconds = round(metres / speed, 2) if speed > 3.0 else None
        return round(metres, 1), seconds

    def _gap_seconds(
        self,
        my_idx: int,
        their_idx: int,
        ahead: bool,
        est_times: List[float],
        lap_time: float,
    ) -> Optional[float]:
        """Time gap to another car, in seconds.

        Prefers CarIdxEstTime, which accounts for track layout — 10% of a lap
        is worth less time down a straight than through a corner sequence.
        Falls back to a track-position delta so the gap still updates
        continuously if EstTime is unpopulated.
        """
        def unwrap(gap: float) -> Optional[float]:
            if gap < 0 and lap_time > 0:
                gap += lap_time
            return round(gap, 2) if gap >= 0 else None

        try:
            mine = float(est_times[my_idx])
            theirs = float(est_times[their_idx])
            if mine > 0 or theirs > 0:
                return unwrap((theirs - mine) if ahead else (mine - theirs))
        except (IndexError, TypeError, ValueError):
            pass

        # Fallback: fraction of a lap between us, converted to seconds. Built
        # only from channels that are known to update every tick.
        if lap_time <= 0:
            return None
        pcts = self._get("CarIdxLapDistPct") or []
        try:
            mine_pct = float(pcts[my_idx])
            theirs_pct = float(pcts[their_idx])
        except (IndexError, TypeError, ValueError):
            return None
        if mine_pct < 0 or theirs_pct < 0:
            return None
        delta = (theirs_pct - mine_pct) if ahead else (mine_pct - theirs_pct)
        if delta < 0:
            delta += 1.0
        return unwrap(delta * lap_time)

    def _standings(self) -> Tuple[List[Rival], int, Optional[Rival]]:
        """Running order, field size, and the session's fastest lap.

        Which cars are in the field comes from CarIdxPosition — a car with
        no classification is not racing. What number each one is given comes
        from _live_ranks(), the same call that answers the driver's own
        position, so the standings and the position line cannot disagree.
        """
        positions = self._get("CarIdxPosition") or []
        laps = self._get("CarIdxLap") or []
        pits = self._get("CarIdxOnPitRoad") or []
        f2 = self._get("CarIdxF2Time") or []
        surfaces = self._get("CarIdxTrackSurface") or []
        class_pos = self._get("CarIdxClassPosition") or []
        surface_names = {value: _SPOKEN_SURFACE.get(name, name)
                         for value, name in _enum_map("TrkLoc", {
                             -1: "not in world", 0: "off track",
                             1: "in pit stall", 2: "aproaching pits",
                             3: "on track"}).items()}
        best = self._get("CarIdxBestLapTime") or []
        last = self._get("CarIdxLastLapTime") or []
        compound = self._get("CarIdxTireCompound") or []
        p2p_counts = self._get("CarIdxP2P_Count") or []
        p2p_statuses = self._get("CarIdxP2P_Status") or []
        drivers = self._drivers()
        my_idx = self._get("PlayerCarIdx", -1)
        live_ranks = self._live_ranks()

        # CarIdxF2Time is, in the SDK's own words, "Race time behind leader
        # or fastest lap time otherwise". Outside a race it is a LAP TIME,
        # not a gap — read as one it made every car in practice and
        # qualifying a minute and a half behind the leader.
        #
        # What "behind" means in a time session is how far off the best lap
        # you are, so that is what is computed. Same field, same units, and
        # the number a driver is actually looking at on the timing screen.
        racing = session_kind(self._session_type()) == "race"
        session_best = None
        if not racing:
            times = [t for i, p in enumerate(positions) if p and p >= 1
                     and i in drivers
                     for t in [_positive(_at(best, i))] if t is not None]
            session_best = min(times) if times else None

        entries: List[Tuple[int, Rival]] = []
        for idx, pos in enumerate(positions):
            if not pos or pos < 1 or idx not in drivers:
                continue
            info = drivers[idx]

            def at(seq, default=None):
                try:
                    return seq[idx]
                except (IndexError, TypeError):
                    return default

            laps_down = 0
            try:
                laps_down = int(laps[my_idx]) - int(at(laps, 0) or 0)
            except (IndexError, TypeError, ValueError):
                pass

            # Live running order in a green race, iRacing's classification
            # everywhere else. The standings and the driver's own position
            # line must agree: when they did not, the model answered from
            # whichever it read first and the position appeared to flicker
            # between correct and a lap stale.
            rank, class_rank = live_ranks.get(idx, (None, None))
            shown = rank if rank is not None else int(pos)
            shown_class = class_rank if rank is not None else _count(at(class_pos))

            entries.append((shown, Rival(
                name=info.get("name", ""),
                short_name=info.get("short_name", ""),
                car_number=info.get("car_number", ""),
                car_class_id=info.get("class_id"),
                car_class_name=info.get("class_name", ""),
                class_est_lap_time=info.get("class_est_lap_time"),
                car_id=info.get("car_id"),
                car_model=info.get("car_model", ""),
                car_model_short=info.get("car_model_short", ""),
                car_path=info.get("car_path", ""),
                # The leader is 0s behind the leader, but _positive() maps 0
                # to None, which left every gap-to-leader sum unanswerable.
                # Keyed on the position actually reported, so race_math's
                # "P1 means zero gap" assumption stays true.
                gap=(0.0 if shown == 1 else _positive(at(f2))) if racing
                else _lap_deficit(_positive(at(best)), session_best),
                position=shown,
                laps_down=laps_down,
                best_lap=_positive(at(best)),
                last_lap=_positive(at(last)),
                tyre_compound=_count(at(compound)),
                sector_times=list(self._sector_times.get(idx, [])),
                in_pits=bool(at(pits, False)),
                track_surface=surface_names.get(at(surfaces), "") or "",
                class_position=shown_class,
                is_player=(idx == my_idx),
                # Read per driver since the field-strength work, and never
                # copied onto the row until now — so Rival.irating was None
                # for every car in every session, "what's his iRating" could
                # not be answered, and the field-strength line rendered
                # empty. Nothing offline could catch it: the simulated
                # adapter does not set these either, so the fixtures agreed
                # with the bug.
                irating=info.get("irating"),
                licence=info.get("licence", ""),
                is_ai=bool(info.get("is_ai")),
                push_to_pass_count=_count(at(p2p_counts)),
                push_to_pass_active=_optional_bool(at(p2p_statuses)),
                in_world=at(surfaces) != -1,
            )))

        entries.sort(key=lambda e: e[0])
        order = [r for _, r in entries]

        # Outside a race, add the people who are here but not classified.
        #
        # The field is built from CarIdxPosition, and in practice a driver
        # who has not set a lap yet may not have one — they have just
        # joined, or they are still in the garage. Dropped, they cannot be
        # asked about at all: "what's Kowalski's iRating" comes back as
        # "I can't find that car" for somebody sitting in the next pit box.
        # In a race the classified set is the running order and must not be
        # padded, which is why this is only for time sessions.
        if not racing:
            listed = {r.name for r in order if r.name}
            for idx, info in sorted(drivers.items()):
                name = info.get("name", "")
                if not name or name in listed:
                    continue
                order.append(Rival(
                    name=name,
                    short_name=info.get("short_name", ""),
                    car_number=info.get("car_number", ""),
                    car_class_id=info.get("class_id"),
                    car_class_name=info.get("class_name", ""),
                    class_est_lap_time=info.get("class_est_lap_time"),
                    car_id=info.get("car_id"),
                    car_model=info.get("car_model", ""),
                    car_model_short=info.get("car_model_short", ""),
                    car_path=info.get("car_path", ""),
                    # No position and no gap on purpose: they have not been
                    # classified, and inventing a slot for them would put a
                    # car in the order that the timing screen does not show.
                    position=None,
                    best_lap=_positive(_at(best, idx)),
                    last_lap=_positive(_at(last, idx)),
                    in_pits=bool(_at(pits, idx, False)),
                    track_surface=surface_names.get(_at(surfaces, idx), "") or "",
                    push_to_pass_count=_count(_at(p2p_counts, idx)),
                    push_to_pass_active=_optional_bool(_at(p2p_statuses, idx)),
                    irating=info.get("irating"),
                    licence=info.get("licence", ""),
                    is_ai=bool(info.get("is_ai")),
                    in_world=_at(surfaces, idx) != -1,
                    is_player=(idx == my_idx),
                ))

        fastest = None
        timed = [r for r in order if r.best_lap]
        if timed:
            fastest = min(timed, key=lambda r: r.best_lap)

        return order, len(order), fastest

    def _rivals(self) -> Tuple[Optional[Rival], Optional[Rival]]:
        """The cars immediately ahead and behind on track, with names."""
        if self._race_not_started():
            return None, None
        order = self._live_order()
        my_idx = self._get("PlayerCarIdx", -1)
        if my_idx is None or my_idx < 0 or not order:
            return None, None

        ranks = {idx: rank for rank, (_, idx) in enumerate(order)}
        my_rank = ranks.get(my_idx)
        if my_rank is None:
            return None, None

        est_times = self._get("CarIdxEstTime") or []
        laps = self._get("CarIdxLapCompleted") or self._get("CarIdxLap") or []
        p2p_counts = self._get("CarIdxP2P_Count") or []
        p2p_statuses = self._get("CarIdxP2P_Status") or []
        drivers = self._drivers()
        lap_time = (
            _positive(self._get("LapBestLapTime"))
            or _positive(self._get("LapLastLapTime"))
            or 0.0
        )

        def build(rank: int, ahead: bool) -> Optional[Rival]:
            if rank < 0 or rank >= len(order):
                return None
            _, idx = order[rank]
            info = drivers.get(idx, {})

            # Prefer iRacing's own distance channel; fall back to the
            # position-delta estimate only when it is not published.
            metres, seconds = self._direct_gap(ahead)
            timing_gap = self._gap_seconds(
                my_idx, idx, ahead, est_times, lap_time)
            gap = seconds if seconds is not None else timing_gap

            laps_down = 0
            try:
                laps_down = int(laps[my_idx]) - int(laps[idx])
            except (IndexError, TypeError, ValueError):
                pass

            return Rival(
                name=info.get("name", ""),
                short_name=info.get("short_name", ""),
                car_number=info.get("car_number", ""),
                car_class_id=info.get("class_id"),
                car_class_name=info.get("class_name", ""),
                class_est_lap_time=info.get("class_est_lap_time"),
                car_id=info.get("car_id"),
                car_model=info.get("car_model", ""),
                car_model_short=info.get("car_model_short", ""),
                car_path=info.get("car_path", ""),
                gap=gap,
                gap_metres=metres,
                trend_gap=timing_gap,
                position=rank + 1,
                laps_down=laps_down,
                push_to_pass_count=_count(_at(p2p_counts, idx)),
                push_to_pass_active=_optional_bool(_at(p2p_statuses, idx)),
            )

        return build(my_rank - 1, ahead=True), build(my_rank + 1, ahead=False)

    def _player(self) -> Dict[str, Any]:
        """The driver's own identity, so the engineer can use their name."""
        idx = self._get("PlayerCarIdx", -1)
        if idx is None or idx < 0:
            return {}
        return self._drivers().get(int(idx), {})

    def _finished(self) -> bool:
        """Has the session actually ended?"""
        state = _SESSION_STATES.get(self._get("SessionState"), "")
        return state in ("Checkered", "CoolDown")

    def _race_not_started(self) -> bool:
        """Whether a race is still gridding or running its formation lap."""
        state = _SESSION_STATES.get(self._get("SessionState"), "")
        return (session_kind(self._session_type()) == "race"
                and state in ("GetInCar", "Warmup", "ParadeLaps"))

    def _drivers(self) -> Dict[int, Dict[str, Any]]:
        """Map car index → driver identity from the session's DriverInfo YAML.

        Cached with the rest of the session info, so naming a rival costs
        nothing per poll.
        """
        info = self._session("DriverInfo", {}) or {}
        drivers: Dict[int, Dict[str, Any]] = {}
        live_classes = self._get("CarIdxClass") or []
        player_idx = _int_or(self._get("PlayerCarIdx"), -1)
        player_class = _count(self._get("PlayerCarClass"))
        for entry in info.get("Drivers", []):
            try:
                idx = int(entry["CarIdx"])
            except (KeyError, TypeError, ValueError):
                continue
            # Neither the pace car nor a spectator is an opponent.
            if entry.get("CarIsPaceCar") or entry.get("IsSpectator"):
                continue
            name = _roster_text(entry.get("UserName"))
            yaml_class = _count(entry.get("CarClassID"))
            live_class = _count(_at(live_classes, idx))
            live_conflict = False
            if idx == player_idx:
                if live_class is None:
                    live_class = player_class
                elif player_class is not None and player_class != live_class:
                    live_class = None
                    live_conflict = True
            class_id = (None if live_conflict else
                        live_class if live_class is not None else yaml_class)
            # A mismatched row can be stale after the roster is rebuilt.
            # Keep the live numeric class for ordering, but do not put the
            # old row's readable class or model against it.
            class_name = _roster_text(entry.get("CarClassShortName"))
            identity_conflict = live_conflict or (
                live_class is not None and yaml_class is not None
                and live_class != yaml_class)
            if identity_conflict:
                class_name = ""
            drivers[idx] = {
                "name": name,
                # AbbrevName is "Surname, F." — nicer to hear on radio.
                "short_name": _roster_text(entry.get("AbbrevName")) or name,
                "car_number": _roster_text(entry.get("CarNumber")),
                "team": _roster_text(entry.get("TeamName")),
                # AI entries carry a rating that is not a real driver's, so
                # it is dropped rather than reported as one. CarIsAI exists
                # precisely because these are not people.
                "is_ai": bool(entry.get("CarIsAI")),
                # Needed to rank the live order within the driver's own
                # class; without it a live overall position would disagree
                # with a classified class position in a single-class field.
                "class_id": class_id,
                "class_name": class_name,
                "class_est_lap_time": (None if identity_conflict else
                                        _positive(entry.get("CarClassEstLapTime"))),
                "car_id": None if identity_conflict else _count(entry.get("CarID")),
                "car_model": ("" if identity_conflict else
                              _roster_text(entry.get("CarScreenName"))),
                "car_model_short": ("" if identity_conflict else
                                    _roster_text(entry.get("CarScreenNameShort"))),
                "car_path": ("" if identity_conflict else
                             str(entry.get("CarPath") or "").strip()),
                "irating": _irating(entry),
                "licence": _roster_text(entry.get("LicString")),
            }
        return drivers

    def _live_order(self) -> List[Tuple[float, int]]:
        """Cars on track sorted by distance covered, leader first.

        iRacing's CarIdxPosition is the *official* classification and only
        recomputes as cars cross the line, so asking mid-lap returns last
        lap's answer. Distance covered gives the true running order now.

        Two traps, both of which put a car a whole lap out of place:

        `CarIdxLap` is laps *started*, not completed, and it increments a
        tick or two before `CarIdxLapDistPct` wraps back to zero. For that
        window `lap + fraction` reads a full lap high, which is why the
        engineer once called P1 to a driver who had just been passed — the
        spike lands exactly at the start/finish line. `CarIdxLapCompleted`
        is the channel that pairs with the fraction.

        Even then the two can disagree for a tick, so a car whose lap
        counter has just advanced while its fraction is still near the end
        of the lap has its fraction forced to zero. CrewChief carries the
        same correction (`FixPercentagesOnLapChange`) for the same reason.
        """
        completed = self._get("CarIdxLapCompleted") or []
        started = self._get("CarIdxLap") or []
        pcts = self._get("CarIdxLapDistPct") or []
        drivers = self._drivers()

        order: List[Tuple[float, int]] = []
        for idx, pct in enumerate(pcts):
            try:
                fraction = float(pct)
            except (TypeError, ValueError):
                continue
            # -1 marks a car in the garage or not yet out.
            if fraction < 0:
                continue
            if drivers and idx not in drivers:
                continue  # pace car or empty slot

            started_lap = _int_or(started[idx] if idx < len(started) else None)
            # CarIdxLapCompleted is -1 before the first crossing; laps
            # started minus one is the same count, and is what is left when
            # the channel is not yet populated.
            lap = _int_or(completed[idx] if idx < len(completed) else None)
            if lap is None or lap < 0:
                lap = (started_lap - 1) if started_lap is not None else None
            if lap is None or lap < 0:
                lap = 0

            if started_lap is not None:
                if started_lap > self._prev_lap.get(idx, started_lap) and fraction > 0.90:
                    # Counter moved but the fraction has not wrapped yet.
                    fraction = 0.0
                else:
                    self._prev_lap[idx] = started_lap

            order.append((lap + fraction, idx))

        order.sort(reverse=True)
        return order
