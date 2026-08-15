"""Simulated telemetry source.

Lets the whole pipeline run — and the answers stay grounded in plausible
numbers — without iRacing, Windows, or the irsdk package. This is what the
"auto" telemetry source falls back to.
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional

from . import (CarTelemetry, LapRecord, Rival, TelemetryAdapter,
               TelemetrySnapshot, TrackConfig)

_LAP_TIME = 92.4          # seconds per lap
_RACE_LAPS = 40
_TRACK_NAME = "Spa-Francorchamps"
_GRID_POSITION = 7        # so a race story has somewhere to start from
_OPENING_LAP = 21         # laps already run when the fixture is created


class SimulatedTelemetryAdapter(TelemetryAdapter):
    """Generate believable, slowly-evolving telemetry from a wall clock."""

    name = "simulated"

    def __init__(self, lap_time: float = _LAP_TIME, race_laps: int = _RACE_LAPS):
        # Open partway into the race rather than on lap one. A fixture that
        # always starts on the formation lap has no lap history, no stint and
        # no fuel drawn down, so pace, degradation, strategy and the race
        # story all answer "I haven't got that" offline — indistinguishable
        # from being broken.
        self._start = time.monotonic() - _OPENING_LAP * lap_time
        self._lap_time = lap_time
        self._race_laps = race_laps
        self._rng = random.Random(0xF1)

    def is_connected(self) -> bool:
        return True

    def _lap_history(self, completed: int) -> list:
        """A plausible race so far: a climb from P7, a stop, and one off.

        Without this the lap history is empty, and pace_trend, race_history
        and the debrief all answer "I haven't got that" offline — which is
        indistinguishable from them being broken. The shape is deliberate:
        a pit lap and a scrappy lap have to be in here or the filtering that
        keeps them out of a trend is never exercised.

        Fixture, not reference. These numbers are internally plausible and
        nothing more — agreement with them validates nothing.
        """
        # P7 on the grid, forward to P4, losing two places across the stop.
        positions = [6, 6, 5, 5, 5, 4, 4, 4, 6, 5, 4, 4, 4]
        laps = []
        for n in range(1, min(completed, 60) + 1):
            pit = n % 18 == 0                      # matches the stint counter
            off = n == 9
            time_s = self._lap_time + (n % 5) * 0.14
            if pit:
                time_s += 22.0
            elif off:
                time_s += 6.2
            laps.append(LapRecord(
                lap=n,
                time=round(time_s, 3),
                position=positions[min(n - 1, len(positions) - 1)],
                fuel_left=round(max(0.0, 96.0 - n * 2.4), 1),
                fuel_used=round(0.9 if pit else 2.4 + (n % 3) * 0.05, 3),
                clean=not (pit or off),
                note="pit lane" if pit else ("off track" if off else ""),
                session_num=0,
            ))
        return laps

    def get_telemetry_snapshot(self) -> Optional[TelemetrySnapshot]:
        elapsed = time.monotonic() - self._start
        lap_float = elapsed / self._lap_time
        lap = int(lap_float) + 1
        progress = lap_float - int(lap_float)

        # One smooth oscillation per lap stands in for the track layout.
        phase = progress * 2 * math.pi
        throttle_curve = (math.sin(phase) + 1) / 2

        speed = 90 + throttle_curve * 190          # 90–280 km/h
        rpm = int(4200 + throttle_curve * 3600)
        gear = str(max(1, min(6, int(1 + throttle_curve * 5.99))))

        stint_laps = (lap - 1) % 18
        wear = max(30.0, 100.0 - stint_laps * 3.4)  # tread remaining %
        # Fronts on this layout give up first, right-front most of all.
        tyre_temps = {
            "fl": 82 + throttle_curve * 14 + stint_laps * 0.35,
            "fr": 86 + throttle_curve * 15 + stint_laps * 0.45,
            "rl": 78 + throttle_curve * 12 + stint_laps * 0.25,
            "rr": 80 + throttle_curve * 13 + stint_laps * 0.30,
        }

        fuel_pct = max(0.0, 100.0 - (lap_float / self._race_laps) * 96.0)
        fuel_laps = fuel_pct / max(0.1, 96.0 / self._race_laps)

        car = CarTelemetry(
            position=4,
            official_position=4,
            speed=speed,
            rpm=rpm,
            gear=gear,
            tire_fl_wear=wear,
            tire_fr_wear=max(30.0, wear - 4.0),
            tire_rl_wear=min(100.0, wear + 2.0),
            tire_rr_wear=min(100.0, wear + 1.0),
            tire_fl_temp=tyre_temps["fl"],
            tire_fr_temp=tyre_temps["fr"],
            tire_rl_temp=tyre_temps["rl"],
            tire_rr_temp=tyre_temps["rr"],
            tire_fl_age=stint_laps,
            tire_fr_age=stint_laps,
            tire_rl_age=stint_laps,
            tire_rr_age=stint_laps,
            brake_fl_temp=340 + throttle_curve * 210,
            brake_fr_temp=350 + throttle_curve * 220,
            brake_rl_temp=290 + throttle_curve * 160,
            brake_rr_temp=295 + throttle_curve * 165,
            fuel_level_pct=fuel_pct,
            fuel_remaining_laps=fuel_laps,
            ahead=Rival(name="M. Rossi", short_name="Rossi, M.", car_number="44",
                        car_class_id=1, car_class_name="Porsche Cup",
                        car_id=1, car_model="Porsche 911 GT3 Cup (992)",
                        car_model_short="Porsche 911 Cup",
                        gap=round(1.4 + math.sin(elapsed / 30) * 0.6, 2), position=3),
            behind=Rival(name="J. Kowalski", short_name="Kowalski, J.", car_number="17",
                         car_class_id=1, car_class_name="Porsche Cup",
                         car_id=1, car_model="Porsche 911 GT3 Cup (992)",
                         car_model_short="Porsche 911 Cup",
                         gap=round(2.1 + math.cos(elapsed / 25) * 0.8, 2), position=5),
            lap_progress=progress,
            stint_laps=stint_laps,
            best_lap_time=self._lap_time - 1.2,
            current_lap_time=progress * self._lap_time,
            last_lap_time=(self._lap_time + math.sin(elapsed / 40) * 0.9) if lap > 1 else None,
            # Three sectors summing near the lap time, with the middle one
            # off the personal best - so "which sector am I losing in" has
            # something to find offline.
            sector_times=[28.4, 41.2, 21.9],
            sector_bests=[28.2, 40.1, 21.7],
            track_surface_temp=31.5 + math.sin(elapsed / 600) * 1.5,
            air_temp=22.0 + math.sin(elapsed / 600) * 0.8,
            rain_intensity=0.0,
            flag="Green",
        )

        names = ["M. Rossi", "A. Silva", "T. Becker", "You", "J. Kowalski",
                 "L. Dubois", "K. Nakamura", "P. Novak"]
        grid = []
        for i, nm in enumerate(names, start=1):
            grid.append(Rival(name=nm, car_number=str(10 + i * 3), position=i,
                              car_class_id=1, car_class_name="Porsche Cup",
                              class_est_lap_time=self._lap_time,
                              car_id=1, car_model="Porsche 911 GT3 Cup (992)",
                              car_model_short="Porsche 911 Cup",
                              gap=None if i == 1 else round((i - 1) * 1.9 + math.sin(elapsed / 20 + i) * 0.4, 2),
                              best_lap=self._lap_time - 2.4 + i * 0.35,
                              in_pits=(i == 7)))
        fastest = min(grid, key=lambda r: r.best_lap)

        over = lap > self._race_laps
        track = TrackConfig(
            current_lap=lap,
            race_laps=lap - 1,
            laps_remaining=max(0, self._race_laps - lap),
            laps_total=self._race_laps,
            track_name=_TRACK_NAME,
            session_type="Race",
            car_class_id=1,
            car_class_name="Porsche Cup",
            car_class_est_lap_time=self._lap_time,
            car_id=1,
            car_model="Porsche 911 GT3 Cup (992)",
            car_model_short="Porsche 911 Cup",
            session_num=0,
            session_state="Checkered" if over else "Racing",
            # The reader sets this from SessionState; without it here the
            # debrief prompt could not be exercised offline at all.
            finished=over,
            time_remaining=max(0.0, (self._race_laps - lap_float) * self._lap_time),
            time_total=self._race_laps * self._lap_time,
            field=grid, field_size=len(grid), fastest_lap=fastest,
            laps=self._lap_history(lap - 1),
            grid_position=_GRID_POSITION,
            laps_led=0,
            caution_flags=1,
            caution_laps=3,
            lead_changes=2,
        )
        return TelemetrySnapshot(car, track)
