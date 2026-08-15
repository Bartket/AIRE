"""Settings that used to need a restart, and the calls that blocked the loop.

`apply_config` re-applies STT, TTS, devices, the telemetry adapter and the
hotkey. Three things it did not: the exchange-log length and the diagnostic
trace length are both deque `maxlen`s fixed at construction, and the poll
interval was read once before the `while`. Each showed the new value in the
panel while the app kept using the old one, with nothing saying so.

The second half is the same shape one layer down: work that has to happen on
a thread, called inline from a coroutine, stalling every other handler while
it runs.
"""

import asyncio
import threading
import time

import pytest

from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator


def _orchestrator(**overrides):
    data = {"telemetry": {"source": "simulated"}}
    data.update(overrides)
    return Orchestrator(AppConfig(data))


# ── Exchange log length ─────────────────────────────────────────────────

def test_a_longer_exchange_log_applies_without_a_restart():
    orch = _orchestrator(memory_log=2)
    assert orch._history.maxlen == 2

    orch.config.update({"memory_log": 25})
    orch.apply_config()

    assert orch._history.maxlen == 25


def test_changing_the_log_length_keeps_the_exchanges_already_in_it():
    """Nobody resizing a log expects the entries to be the price."""
    orch = _orchestrator(memory_log=20)
    orch._log_exchange("How's my fuel?", "Eight laps left in it.")

    orch.config.update({"memory_log": 5})
    orch.apply_config()

    assert orch._history.maxlen == 5
    assert [entry["question"] for entry in orch.history()] == ["How's my fuel?"]


def test_shrinking_the_log_keeps_the_newest_exchanges():
    orch = _orchestrator(memory_log=20)
    for lap in range(4):
        orch._log_exchange(f"lap {lap}", "copy")

    orch.config.update({"memory_log": 2})
    orch.apply_config()

    assert [entry["question"] for entry in orch.history()] == ["lap 2", "lap 3"]


# ── Poll interval ───────────────────────────────────────────────────────

def test_the_poll_interval_is_re_read_every_tick():
    """Read once before the loop, a new interval did nothing until restart."""
    orch = _orchestrator(telemetry={"source": "simulated", "poll_interval": 0.5})
    assert orch._poll_interval() == 0.5

    orch.config.update({"telemetry": {"poll_interval": 0.05}})

    # No apply_config: the loop reads it itself, which is what makes a save
    # from the panel take effect on the very next tick.
    assert orch._poll_interval() == 0.05


def test_the_running_loop_asks_for_the_interval_on_every_tick():
    """The accessor is only worth having if the loop actually calls it.

    Read once before the `while`, a saved change reached the loop on the next
    restart and not before.
    """
    orch = _orchestrator(telemetry={"source": "simulated"})
    intervals, polls = [], []

    def poll():
        polls.append(True)
        if len(polls) >= 3:
            orch._running = False
        return None

    def interval():
        intervals.append(True)
        return 0.0                     # keep the test off the clock

    orch.telemetry.get_telemetry_snapshot = poll
    orch._poll_interval = interval
    orch._running = True

    asyncio.run(orch._telemetry_loop())

    assert len(polls) == 3
    assert len(intervals) == 3, "the interval was read once and cached"


def test_a_nonsense_poll_interval_does_not_take_the_poll_loop_down():
    """It is read on every tick now, so a bad value would stop telemetry dead."""
    orch = _orchestrator(telemetry={"source": "simulated", "poll_interval": "fast"})

    assert orch._poll_interval() == 0.1


# ── Diagnostic trace length ─────────────────────────────────────────────

def test_the_trace_buffer_is_resized_for_a_new_poll_interval():
    """Its maxlen is a minute's worth of samples at the current rate."""
    orch = _orchestrator(telemetry={"source": "simulated", "poll_interval": 1.0,
                                    "diagnostic_trace": True})
    assert orch._trace_samples.maxlen == 62

    orch.config.update({"telemetry": {"poll_interval": 0.1}})
    orch.apply_config()

    assert orch._trace_samples.maxlen == 602


def test_resizing_the_trace_buffer_keeps_the_samples_in_it():
    """The minute before a wrong answer is the whole point of the trace."""
    orch = _orchestrator(telemetry={"source": "simulated", "poll_interval": 0.1,
                                    "diagnostic_trace": True})
    orch._remember_trace_sample(orch.get_snapshot())
    assert len(orch._trace_samples) == 1

    orch.config.update({"telemetry": {"poll_interval": 0.2}})
    orch.apply_config()

    assert orch._trace_samples.maxlen == 302
    assert len(orch._trace_samples) == 1


# ── Off the event loop ──────────────────────────────────────────────────

class _SlowDuck:
    """Stands in for the WASAPI session enumeration duck() really does."""

    def __init__(self, seconds=0.01):
        self.seconds = seconds
        self.calls = []

    def duck(self):
        self.calls.append(("duck", threading.current_thread()))
        time.sleep(self.seconds)
        return True

    def un_duck(self):
        self.calls.append(("un_duck", threading.current_thread()))
        time.sleep(self.seconds)
        return True


def test_ducking_does_not_run_on_the_event_loop():
    """duck() enumerates every audio session and queries an interface each.

    Called inline from `async def _speak`, that stalls the telemetry poll and
    every HTTP handler at the exact moment the driver is being spoken to —
    while every other await in the same function is already on a thread.
    """
    orch = _orchestrator()
    duck = _SlowDuck()
    orch.ducking = duck
    orch.tts.synthesize = lambda text, regenerate=False: b"\x01\x02" * 1000
    orch.audio_player.play_array = lambda audio, rate: None
    orch._apply_radio_effect = lambda audio, rate: audio

    assert asyncio.run(orch._speak("Box this lap.")) is True

    assert [call for call, _ in duck.calls] == ["duck", "un_duck"]
    loop_thread = threading.main_thread()
    assert all(thread is not loop_thread for _, thread in duck.calls), \
        "ducking blocked the thread running the event loop"


def test_a_cold_snapshot_is_read_on_a_thread():
    """`get_snapshot()` falls back to a blocking SDK read when nothing has
    been polled yet — before the first tick, or right after a source swap —
    and `ask` called it inline. Every other call into the reader is on a
    thread precisely because that read can stall."""
    orch = _orchestrator()
    read_on = []

    def slow_read():
        read_on.append(threading.current_thread())
        time.sleep(0.01)
        return None

    orch.telemetry.get_telemetry_snapshot = slow_read
    orch._snapshot = None                       # cold: no poll has landed

    asyncio.run(orch.snapshot_now())

    assert read_on, "the cold path did not read at all"
    assert read_on[0] is not threading.main_thread()


def test_a_warm_snapshot_costs_nothing():
    """The common case is an attribute read; it must not grow a thread hop."""
    orch = _orchestrator()
    orch._snapshot = "cached"
    orch.telemetry.get_telemetry_snapshot = lambda: pytest.fail(
        "the reader was consulted despite a warm snapshot")

    assert asyncio.run(orch.snapshot_now()) == "cached"
