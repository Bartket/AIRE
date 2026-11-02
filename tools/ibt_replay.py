"""Replay recorded iRacing telemetry (.ibt) through the real reader.

iRacing writes a .ibt file for every stint the driver runs. Feeding one into
`iRacingTelemetryReader` at the app's poll rate, and comparing what the reader
believes with what the 60 Hz samples actually say, is the only way to check
the reader against the sim without sitting in a car.

What a .ibt can and cannot check:

- It holds the driver's own channels only. There are no `CarIdx*` arrays, so
  gaps, rival laps, live order and the field are not exercised. The driver's
  own `CarIdx*` entries are synthesised from the player channels, which is
  enough for the sector timer to run.
- Its session YAML is the copy at the end of the session, not the live one.
- It carries disk-only channels (`LFtempM`, `LFpressure`, ...) that the live
  SDK does not publish. Nothing the reader starts using may be justified by
  one of those; check `vars.txt` first.

Needs pyirsdk. On Windows it is installed with the app; from WSL point
`--irsdk-path` (or AIRE_IRSDK_PATH) at the Windows venv's site-packages.

    python tools/ibt_replay.py "C:/Users/me/Documents/iRacing/telemetry/*.ibt"
    python tools/ibt_replay.py session.ibt --extract out.json --start 600 --seconds 300
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
import time as _time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# The reader is fed the driver's own value at their own index in every
# per-car array it reads, and -1 ("not in the world") everywhere else.
_PLAYER_ARRAYS = {
    "CarIdxLapDistPct": "LapDistPct",
    "CarIdxOnPitRoad": "OnPitRoad",
    "CarIdxTrackSurface": "PlayerTrackSurface",
    "CarIdxLapCompleted": "LapCompleted",
    "CarIdxLap": "Lap",
    "CarIdxLastLapTime": "LapLastLapTime",
    "CarIdxBestLapTime": "LapBestLapTime",
    "CarIdxPosition": "PlayerCarPosition",
    "CarIdxClassPosition": "PlayerCarClassPosition",
}

#: Channels written by --extract: what the lap, fuel, sector, stint and flag
#: code reads. Values only; no YAML, so no names leave the file.
FIXTURE_CHANNELS = (
    "SessionTime", "SessionTick", "SessionNum", "SessionState", "SessionFlags",
    "SessionTimeRemain", "SessionLapsRemainEx", "Lap", "LapCompleted",
    "LapDistPct", "LapLastLapTime", "LapBestLapTime", "LapCurrentLapTime",
    "FuelLevel", "FuelLevelPct", "OnPitRoad", "PlayerCarInPitStall",
    "PlayerTrackSurface", "PlayerCarTowTime", "PlayerCarPosition",
    "PlayerCarClassPosition", "PlayerCarIdx", "Speed", "IsOnTrack",
    "LapDeltaToBestLap", "LapDeltaToBestLap_OK",
    "LapDeltaToOptimalLap", "LapDeltaToOptimalLap_OK",
)

#: Lap time is read this long after the line: LapLastLapTime has been seen
#: to settle up to 2.1 s after LapCompleted moves.
_SETTLE_TICKS = 180


def _import_irsdk(path: Optional[str]):
    path = path or os.environ.get("AIRE_IRSDK_PATH")
    if path:
        # Appended, not prepended, so nothing in that directory can shadow
        # the standard library.
        sys.path.append(path)
    try:
        import irsdk  # noqa: F401
    except ImportError:
        sys.exit("pyirsdk is not importable; pass --irsdk-path to the "
                 "site-packages directory that holds irsdk.py")
    return irsdk


class IbtSource:
    """The slice of pyirsdk's IRSDK interface the reader uses, over a .ibt."""

    def __init__(self, irsdk, path: str) -> None:
        self._ibt = irsdk.IBT()
        self._ibt.open(path)
        mem, header = self._ibt._shared_mem, self._ibt._header
        raw = mem[header.session_info_offset:
                  header.session_info_offset + header.session_info_len]
        text = raw.rstrip(b"\x00").decode("cp1252")
        # The same repair pyirsdk applies before parsing live YAML.
        text = re.sub(r"(\w+: )(,.*)", r'\1"\2"', text)
        import yaml
        self.session_info: Dict[str, Any] = yaml.load(
            text, Loader=irsdk.CustomYamlSafeLoader) or {}
        self.names = set(self._ibt.var_headers_names)
        self.records = self._ibt._disk_header.session_record_count
        self.index = 0
        self.session_info_update = 1
        self.is_initialized = True
        self.is_connected = True
        self.last_session_info_update = 0
        driver = self.session_info.get("DriverInfo") or {}
        self.player_idx = int(driver.get("DriverCarIdx") or 0)
        self._columns: Dict[str, List[Any]] = {}

    def column(self, name: str) -> List[Any]:
        if name not in self._columns:
            self._columns[name] = self._ibt.get_all(name) or []
        return self._columns[name]

    def value(self, name: str, index: Optional[int] = None) -> Any:
        if name not in self.names:
            return None
        return self.column(name)[self.index if index is None else index]

    def __getitem__(self, key: str) -> Any:
        if key in self.names:
            return self.value(key)
        if key in self.session_info:
            return self.session_info[key]
        if key in _PLAYER_ARRAYS:
            cars = [-1] * 64
            cars[self.player_idx] = self.value(_PLAYER_ARRAYS[key])
            return cars
        return None

    def freeze_var_buffer_latest(self) -> None:
        pass

    def unfreeze_var_buffer_latest(self) -> None:
        pass

    def close(self) -> None:
        self._ibt.close()


def _reader_for(source: IbtSource):
    from ai_race_engineer.telemetry import irsdk_reader

    # The reader's stopwatches run on session time, so a replay that runs
    # faster than real time still sees the gaps the sim would have shown.
    irsdk_reader.time = SimpleNamespace(
        monotonic=lambda: float(source.value("SessionTime") or 0.0),
        perf_counter=_time.perf_counter, sleep=_time.sleep)
    reader = irsdk_reader.iRacingTelemetryReader()
    reader._ir = source
    reader._started = True
    return reader


def replay(source: IbtSource, step: int = 6) -> Iterator[Tuple[int, Any, Any]]:
    """(record index, reader, snapshot) at every `step` records (60 Hz / 6 = 10 Hz)."""
    reader = _reader_for(source)
    for index in range(0, source.records, step):
        source.index = index
        yield index, reader, reader._read_snapshot()


def ground_truth(source: IbtSource) -> Dict[int, Dict[str, Any]]:
    """Per completed lap: the time iRacing settled on and the fuel it cost.

    Keyed by the LapCompleted value the crossing produced. Burn is only
    given between two consecutive crossings with no fuel added between them.
    """
    completed = source.column("LapCompleted")
    last = source.column("LapLastLapTime")
    fuel = source.column("FuelLevel")
    crossings = [i for i in range(1, len(completed))
                 if completed[i] == completed[i - 1] + 1]
    laps: Dict[int, Dict[str, Any]] = {}
    for n, i in enumerate(crossings):
        settled = last[min(len(last) - 1, i + _SETTLE_TICKS)]
        laps[completed[i]] = {"time": settled if settled > 0 else None,
                              "burn": None, "record": i}
        if n:
            prev = crossings[n - 1]
            span = fuel[prev:i + 1]
            if completed[prev] + 1 == completed[i] and \
                    all(b <= a + 0.05 for a, b in zip(span, span[1:])):
                laps[completed[i]]["burn"] = fuel[prev] - fuel[i]
    return laps


def check(source: IbtSource, step: int = 6) -> Dict[str, Any]:
    """Replay one file and list every place the reader disagrees with it."""
    truth = ground_truth(source)
    problems: List[str] = []
    snapshot = None
    reader = None
    for _, reader, snapshot in replay(source, step):
        pass
    if reader is None:
        return {"laps": 0, "problems": ["no records"]}

    for record in reader._laps:
        expected = truth.get(record.lap, {})
        if expected.get("time") is None:
            if record.time or record.clean:
                problems.append(f"lap {record.lap}: recorded {record.time:.3f} for a "
                                f"lap iRacing timed as invalid")
        elif abs(record.time - expected["time"]) > 0.0005:
            problems.append(f"lap {record.lap}: time {record.time:.3f}, "
                            f"iRacing says {expected['time']:.3f}")
        burn = expected.get("burn")
        if record.fuel_used is not None and burn is not None \
                and record.clean and abs(record.fuel_used - burn) > 0.05:
            problems.append(f"lap {record.lap}: burn {record.fuel_used:.3f}, "
                            f"measured {burn:.3f}")

    for n, best in enumerate(snapshot.car.sector_bests if snapshot else []):
        if best is not None and best < 2.0:
            problems.append(f"sector {n + 1} best {best:.3f}s is not a sector")

    return {"laps": len(reader._laps), "problems": problems,
            "session_type": snapshot.track.session_type if snapshot else "",
            "stored_best": snapshot.car.stored_best_lap if snapshot else None,
            "stored_optimal": snapshot.car.stored_optimal_lap if snapshot else None}


def extract(source: IbtSource, start: float, seconds: float, out: Path,
            step: int = 6) -> int:
    """Write FIXTURE_CHANNELS for a window of session time, one row per poll.

    The session block is the track and session layout only — sector
    boundaries and session types. Driver names never leave the .ibt.
    """
    times = source.column("SessionTime")
    rows = [i for i, t in enumerate(times) if start <= t < start + seconds][::step]
    channels = [c for c in FIXTURE_CHANNELS if c in source.names]
    info = source.session_info
    data = {
        "source": "iRacing .ibt, channel values only",
        "session": {
            "SplitTimeInfo": info.get("SplitTimeInfo") or {},
            "SessionInfo": {"Sessions": [
                {k: s.get(k) for k in ("SessionNum", "SessionType", "SessionLaps")}
                for s in (info.get("SessionInfo") or {}).get("Sessions") or []]},
        },
        "channels": channels,
        # Not rounded: the back-step that fakes a line crossing at a standstill
        # is 4e-7 of a lap.
        "rows": [[source.value(c, i) for c in channels] for i in rows],
    }
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return len(rows)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", help=".ibt files or globs")
    parser.add_argument("--irsdk-path", help="directory holding pyirsdk's irsdk.py")
    parser.add_argument("--step", type=int, default=6,
                        help="records between polls; 6 is the app's 10 Hz")
    parser.add_argument("--extract", type=Path,
                        help="write a fixture slice instead of checking")
    parser.add_argument("--start", type=float, default=0.0,
                        help="--extract: session time to start at, seconds")
    parser.add_argument("--seconds", type=float, default=300.0,
                        help="--extract: length of the slice, seconds")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    irsdk = _import_irsdk(args.irsdk_path)
    logging.disable(logging.WARNING)

    paths = sorted({p for pattern in args.files for p in glob.glob(pattern)})
    if not paths:
        print("no files matched", file=sys.stderr)
        return 2

    if args.extract:
        source = IbtSource(irsdk, paths[0])
        count = extract(source, args.start, args.seconds, args.extract, args.step)
        source.close()
        print(f"{count} polls -> {args.extract}")
        return 0

    failed = 0
    for path in paths:
        source = IbtSource(irsdk, path)
        result = check(source, args.step)
        source.close()
        name = Path(path).name
        status = "ok" if not result["problems"] else f"{len(result['problems'])} problems"
        reference = ""
        if result.get("stored_best"):
            optimal = result.get("stored_optimal")
            reference = (f", stored best {result['stored_best']:.3f}"
                         f" optimal {f'{optimal:.3f}' if optimal else '-'}")
        print(f"{name}: {result.get('session_type', '')}, "
              f"{result['laps']} laps{reference}, {status}")
        for problem in result["problems"]:
            print(f"    {problem}")
        failed += bool(result["problems"])
    print(f"{len(paths) - failed} of {len(paths)} files clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
