"""The first answer of a run must not pay for loading the radio effect.

`_apply_radio_effect` imports scipy's filters lazily, so the import landed
inside the first spoken answer of every run — measured at about 300 ms warm
and 1.3 s cold, between the driver's question and the first word.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Run in a fresh interpreter: inside pytest another test has usually imported
# the radio effect already, and this would pass whether or not start() did.
_PROBE = """
import asyncio, sys
from ai_race_engineer.config import AppConfig
from ai_race_engineer.orchestrator import Orchestrator

async def main():
    orch = Orchestrator(AppConfig({"telemetry": {"source": "simulated"}},
                                  path=sys.argv[1]))
    assert "ai_race_engineer.radio_effect" not in sys.modules, "loaded too early to tell"
    await orch.start()
    try:
        preload = getattr(orch, "_preload", None)
        if preload is not None:
            await preload
    finally:
        await orch.stop()
    print("ai_race_engineer.radio_effect" in sys.modules)

asyncio.run(main())
"""


def test_starting_the_engineer_loads_the_radio_effect(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path / "config.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        # Inherited, not built from scratch: Python on Windows needs
        # SYSTEMROOT to start, and CI runs this suite there too.
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("True"), "start() left scipy for the first answer"


def test_the_readmes_first_question_gets_an_answer_offline():
    """README step 7: choose Simulated, type "How's my fuel?".

    The fixture published fuel only as a percentage and laps left — the fields
    from before the calculators existed — so the answer every new user heard
    first was "I've got no fuel reading", which looks like a broken install.
    """
    from ai_race_engineer.llm import _direct_telemetry_answer
    from ai_race_engineer.telemetry.simulated import SimulatedTelemetryAdapter

    snapshot = SimulatedTelemetryAdapter().get_telemetry_snapshot()
    answer = _direct_telemetry_answer("How's my fuel?", snapshot)

    assert answer, "the fuel question did not reach its calculator"
    assert "no fuel reading" not in answer.lower()
    assert "three green laps" not in answer.lower()


def test_a_quick_answer_waits_like_an_engineer_and_a_slow_one_does_not(
        tmp_path):
    """A cached, deterministic answer started playing within moments of the
    driver letting go of the button — too fast to sound like anyone heard the
    question. The floor is measured from release, so it never adds to an
    answer that was already slow."""
    import asyncio

    from ai_race_engineer import orchestrator as module
    from ai_race_engineer.config import AppConfig

    config = AppConfig(
        {"telemetry": {"source": "simulated"}, "ducking": {"enabled": False},
         "audio": {"min_reply_seconds": 0.2}},
        path=tmp_path / "config.json")
    orch = module.Orchestrator(config)
    orch.tts.synthesize = lambda text, regenerate=False: b"\x01\x02" * 1000
    orch.audio_player.play_array = lambda audio, rate: None
    orch._apply_radio_effect = lambda audio, rate: audio

    orch._timings = module._Timings()
    asyncio.run(orch._speak("P3."))
    quick = orch._timings.as_dict()
    assert quick["first_audio"] >= 200, quick

    orch._timings = module._Timings()
    orch._timings._started -= 1.0  # the model already took a second
    asyncio.run(orch._speak("P3."))
    assert "pause" not in orch._timings.as_dict()

    # A setting, read when speaking: 0 is off, and a hand-edited nonsense
    # value falls back to the default rather than costing the answer.
    config.audio["min_reply_seconds"] = 0
    orch._timings = module._Timings()
    asyncio.run(orch._speak("P3."))
    assert "pause" not in orch._timings.as_dict()
    config.audio["min_reply_seconds"] = "soon"
    assert orch._min_reply_seconds() == 1.5
