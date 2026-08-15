"""The optional live trace must help debugging without becoming a log leak."""

import json

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

    path = tmp_path / "diagnostic-trace.jsonl"
    backup = tmp_path / "diagnostic-trace.jsonl.1"
    assert path.exists()
    assert backup.exists()
    assert "Second question" in path.read_text(encoding="utf-8")
    assert "First question" in backup.read_text(encoding="utf-8")
