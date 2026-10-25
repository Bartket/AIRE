"""The optional live trace must help debugging without becoming a log leak."""

import json
import time

from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator
from ai_race_engineer.telemetry import CarTelemetry, Rival, TelemetrySnapshot, TrackConfig


def _orchestrator(tmp_path, enabled):
    config = AppConfig({
        "telemetry": {
            "source": "simulated",
            "diagnostic_trace": enabled,
        },
    }, path=tmp_path / "config.json")
    return Orchestrator(config)


def _snapshot():
    return TelemetrySnapshot(
        CarTelemetry(
            lap_progress=0.42,
            speed=188.0,
            fuel_level=31.0,
            fuel_unit="litres",
            fuel_per_lap=3.0,
            fuel_burn_samples=5,
            fuel_burn_min=2.8,
            fuel_burn_max=3.3,
            ahead=Rival(name="Rossi", car_number="13", gap=1.2,
                        gap_metres=58.0, trend_gap=1.14),
            closing_ahead_status="confirmed",
            closing_ahead_change=0.2,
            closing_ahead_segments=3,
        ),
        TrackConfig(current_lap=8, session_num=2, session_type="Race",
                    session_state="Racing"),
    )


def test_diagnostic_trace_is_off_by_default(tmp_path):
    orch = _orchestrator(tmp_path, False)
    orch._remember_trace_sample(_snapshot())
    orch._write_diagnostic_trace("Am I gaining?", "Yes.", "voice")

    assert not (tmp_path / "diagnostic-trace.jsonl").exists()


def test_diagnostic_trace_records_the_inputs_behind_an_answer(tmp_path):
    orch = _orchestrator(tmp_path, True)
    orch._remember_trace_sample(_snapshot())
    orch._write_diagnostic_trace(
        "Am I gaining on the car ahead?",
        "You're gaining on Rossi.",
        "voice",
        _snapshot(),
        route="deterministic",
    )
    orch._flush_trace()

    line = (tmp_path / "diagnostic-trace.jsonl").read_text(encoding="utf-8")
    record = json.loads(line)
    assert record["question"] == "Am I gaining on the car ahead?"
    assert record["route"] == "deterministic"
    assert record["snapshot"]["ahead"]["name"] == "Rossi"
    assert record["samples"][0]["ahead"] == {
        "name": "Rossi",
        "car_number": "13",
        "gap_seconds": 1.2,
        "gap_metres": 58.0,
        "timing_gap_seconds": 1.14,
    }
    assert record["samples"][0]["fuel_burn_min"] == 2.8
    assert record["samples"][0]["ahead_trend"]["mini_sectors"] == 3
    assert "monotonic" not in record["samples"][0]
    assert record["samples"][0]["age_seconds"] >= 0


def test_diagnostic_trace_rotates_instead_of_growing_without_bound(tmp_path,
                                                                  monkeypatch):
    from ai_race_engineer import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "_TRACE_MAX_BYTES", 500)
    orch = _orchestrator(tmp_path, True)
    orch._remember_trace_sample(_snapshot())
    orch._write_diagnostic_trace("First question", "First answer", "voice")
    orch._write_diagnostic_trace("Second question", "Second answer", "voice")
    orch._flush_trace()

    path = tmp_path / "diagnostic-trace.jsonl"
    backup = tmp_path / "diagnostic-trace.jsonl.1"
    assert path.exists()
    assert backup.exists()
    assert "Second question" in path.read_text(encoding="utf-8")
    assert "First question" in backup.read_text(encoding="utf-8")


def test_the_trace_carries_what_the_driver_waited_for(tmp_path, monkeypatch):
    """Stage timings, including the stages that happen after the answer exists.

    The pipeline had no timing in it at all, so "answers take a couple of
    seconds" was an estimate. This is the reading behind it — and it has to
    cover speech and playback, which is why the record is staged during the
    turn and written at the end of it rather than the moment the words exist.
    """
    import asyncio

    orch = _orchestrator(tmp_path, True)
    monkeypatch.setattr(orch.llm, "generate", lambda *_a: "P4, Rossi one two ahead.")

    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    asyncio.run(orch.ask("Where am I?", speak=False))

    record = json.loads(
        (tmp_path / "diagnostic-trace.jsonl").read_text(encoding="utf-8"))
    timings = record["timings_ms"]
    assert "llm" in timings
    assert timings["total"] >= 0
    # The panel reads the same numbers, so the driver can see where it went.
    assert orch.status()["last_timings"]["total"] == timings["total"]


def test_a_measurement_is_never_worth_the_answer(tmp_path, monkeypatch):
    """`_speak` must still run with no turn in flight, and still report why.

    The first version of this asserted `_flush_trace() is None`, which is true
    whatever the code does — a test that could not fail, reporting safety it
    was not providing. This one calls the thing the name is about.
    """
    import asyncio

    from ai_race_engineer.tts import TTSError

    orch = _orchestrator(tmp_path, True)
    orch._timings = None
    monkeypatch.setattr(
        orch.tts, "synthesize",
        lambda *_a, **_k: (_ for _ in ()).throw(TTSError("no key")))

    async def directly(function, *args):
        return function(*args)
    monkeypatch.setattr(asyncio, "to_thread", directly)

    # The fallback stopwatch keeps the stage wrapper working; the real failure
    # still gets through to the status bar rather than being masked by it.
    assert asyncio.run(orch._speak("Anything.")) is False
    assert "no key" in (orch._last_error or "")


def test_one_turn_is_one_measurement(tmp_path):
    """The panel and the trace must not disagree about the same exchange.

    `as_dict` is asked for twice in a turn, and on a voice turn a third time
    after the trace has been written. Re-reading the clock each time made the
    later readings include the cost of writing the trace — 6.5 ms against the
    trace's own 1.5 ms, on a record holding a full minute of samples.
    """
    from ai_race_engineer.orchestrator import _Timings

    timings = _Timings()
    with timings.stage("llm"):
        pass
    first = timings.as_dict()
    time.sleep(0.02)
    assert timings.as_dict() == first

    # And the copy handed out cannot edit the one the next caller gets.
    first["total"] = 999.0
    assert timings.as_dict()["total"] != 999.0
