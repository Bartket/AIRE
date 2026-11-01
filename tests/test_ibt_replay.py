"""The reader against real iRacing telemetry, not sequences written to fit.

Each fixture is a slice of a recorded .ibt — channel values only, one row
per 10 Hz poll, extracted with `tools/ibt_replay.py --extract`. Both bugs
here were invisible to every hand-built test, because the sequences those
tests invented had the channels moving in the order the code assumed.

    uv run pytest tests/test_ibt_replay.py -q
"""

import json
from pathlib import Path

from ai_race_engineer.telemetry.irsdk_reader import iRacingTelemetryReader

FIXTURES = Path(__file__).parent / "fixtures" / "ibt"

# Per-car arrays the reader reads, filled from the driver's own channel at
# the driver's own index, as tools/ibt_replay.py does.
_PLAYER_ARRAYS = {
    "CarIdxLapDistPct": "LapDistPct",
    "CarIdxOnPitRoad": "OnPitRoad",
    "CarIdxTrackSurface": "PlayerTrackSurface",
    "CarIdxLapCompleted": "LapCompleted",
    "CarIdxLap": "Lap",
    "CarIdxLastLapTime": "LapLastLapTime",
}


class _Replay(iRacingTelemetryReader):
    """A reader whose channels come from one fixture row at a time."""

    def __init__(self, fixture):
        super().__init__()
        self.session_blob = fixture["session"]
        self.values = {}

    def _get(self, key, default=None):
        return self.values.get(key, default)

    def _session(self, key, default=None):
        return self.session_blob.get(key, default)


def _replay(name):
    fixture = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    reader = _Replay(fixture)
    rows = [dict(zip(fixture["channels"], row)) for row in fixture["rows"]]
    snapshot = None
    for row in rows:
        idx = int(row["PlayerCarIdx"])
        for array, channel in _PLAYER_ARRAYS.items():
            cars = [-1] * 64
            cars[idx] = row[channel]
            row[array] = cars
        reader.values = row
        snapshot = reader._read_snapshot()
    return reader, rows, snapshot


def test_a_lap_is_recorded_with_its_own_time_not_the_previous_one():
    """LapLastLapTime settles after LapCompleted moves, never with it.

    Across 53 recorded sessions it lagged by 2 to 126 ticks — up to 2.1 s.
    Reading it on the poll where the count moved recorded every lap with the
    lap before's time: 173 of 178 laps. Here lap 3 was a 2:07.643 and the
    reader logged the 2:08.673 it replaced.
    """
    reader, rows, _ = _replay("bathurst_lap_crossing.json")

    crossing = next(n for n, row in enumerate(rows) if row["LapCompleted"] == 3)
    stale = rows[crossing]["LapLastLapTime"]
    settled = rows[-1]["LapLastLapTime"]
    assert stale != settled                  # the fixture shows the lag

    assert [lap.lap for lap in reader._laps] == [3]
    assert reader._laps[0].time == round(settled, 3)
    # The replay starts 7 s before the line, so the reader saw 0.14 L of
    # lap 3 burnt. That is not lap 3's burn and must not be filed as it.
    assert reader._laps[0].fuel_used is None


def test_creeping_backwards_at_a_standstill_is_not_a_lap():
    """LapDistPct steps back by 4e-7 with the car stopped on track.

    Read as two start/finish crossings a tenth of a second apart, that swept
    every sector boundary inside one poll and set personal-best sectors of
    0.007 to 0.023 seconds — in 44 of 52 recorded sessions. A crossing is
    the end of one lap and the start of the next, nothing less.
    """
    reader, rows, snapshot = _replay("bathurst_standstill_jitter.json")

    steps_back = [b for a, b in zip(rows, rows[1:])
                  if b["LapDistPct"] < a["LapDistPct"]]
    assert steps_back                                  # the fixture shows it
    assert all(row["Speed"] < 1.0 for row in steps_back)

    assert snapshot.car.sector_bests == []
    assert snapshot.car.sector_times == []
