"""Ending a recording must happen once, however the two enders collide.

Two threads finish a recording: the button listener on release, and the
cutoff timer when the limit runs out. `Timer.cancel()` does nothing to a
timer already inside its callback, so both can be in `_finish_recording`
for the same press — and both used to release `_busy`, which either raised
on the release of an unlocked lock or freed a lock a later press was
holding. Neither announces itself; you just find push-to-talk dead, or two
questions running at once.
"""

import time

from ai_race_engineer import orchestrator as orchestrator_module
from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator


def _orchestrator():
    return Orchestrator(AppConfig({
        # Keeps the adapter off iRacing and out of the test.
        "telemetry": {"source": "simulated"},
        # The blip is not what is under test, and it wants a real device.
        "audio": {"ptt_beep": False},
    }))


def _press(orch):
    """Put the orchestrator in the state a held button leaves behind."""
    assert orch._busy.acquire(blocking=False)
    orch._press_started_at = time.monotonic()


def test_a_cutoff_landing_on_the_release_hands_off_only_once(monkeypatch):
    """Reproduces the collision deterministically rather than by racing.

    The unsafe window is between reading the press marker and clearing it.
    `time.monotonic()` is called inside exactly that window, so hooking it
    once puts the second caller precisely where a real cutoff timer would
    have landed. With the marker claimed atomically the second caller finds
    nothing to do; without it, both run to the end and release `_busy`
    twice.
    """
    orch = _orchestrator()
    _press(orch)

    reentered = []
    real_monotonic = time.monotonic

    def cutoff_fires_mid_window():
        if not reentered:
            reentered.append(True)
            # This is the cutoff timer, arriving while the release handler
            # is still deciding whether it owns the press.
            orch._finish_recording()
        return real_monotonic()

    monkeypatch.setattr(orchestrator_module.time, "monotonic",
                        cutoff_fires_mid_window)

    orch._finish_recording()

    assert reentered, "the test did not reach the window it is guarding"
    # Exactly one release: the lock is free, and it was never released twice
    # (which raises RuntimeError above, before this line).
    assert orch._busy.acquire(blocking=False), "the recording never released _busy"
    orch._busy.release()
    assert orch._press_started_at is None


def test_the_second_caller_does_not_stop_the_recorder_again(monkeypatch):
    """Only the thread that claims the press may touch the audio stream.

    Both callers used to reach `AudioRecorder.stop()`, which stops and
    closes the same stream twice.
    """
    orch = _orchestrator()
    _press(orch)

    stops = []
    monkeypatch.setattr(orch.audio_recorder, "stop",
                        lambda: stops.append(1) or None)

    reentered = []
    real_monotonic = time.monotonic

    def cutoff_fires_mid_window():
        if not reentered:
            reentered.append(True)
            orch._finish_recording()
        return real_monotonic()

    monkeypatch.setattr(orchestrator_module.time, "monotonic",
                        cutoff_fires_mid_window)

    orch._finish_recording()

    assert stops == [1], f"the recorder was stopped {len(stops)} times"


def test_a_recording_that_was_never_started_is_not_handed_off():
    """The plain guard still has to hold: no press, nothing to finish."""
    orch = _orchestrator()
    assert orch._press_started_at is None

    orch._finish_recording()

    # Nothing acquired, so nothing to release — and no exception either.
    assert orch._busy.acquire(blocking=False)
    orch._busy.release()
