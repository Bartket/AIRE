"""Documentation examples must describe the settings the app actually ships."""

import json
from pathlib import Path

from ai_race_engineer.config import _DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parent.parent


def test_example_config_tracks_every_shipped_default():
    """The example had silently kept old voice settings and an unsafe prompt."""
    example = json.loads((ROOT / "config.json.example").read_text(encoding="utf-8"))

    assert example == _DEFAULT_CONFIG


def test_no_retired_elevenlabs_model_is_shipped_or_offered():
    """A retired model answers until it does not, and then fails silently.

    ElevenLabs replaced both models this app launched with. The ids are plain
    config strings, so nothing fails loudly when one goes away — the request
    simply stops working, mid-race, on the one path the driver cannot check.
    This walks every place an id is written: the shipped defaults, the
    fallbacks in the code, and the dropdowns in the panel.
    """
    from ai_race_engineer.config import _RETIRED_STT, _RETIRED_TTS

    retired = set(_RETIRED_STT) | set(_RETIRED_TTS)
    assert retired, "the retirement table is what this test reads from"

    elevenlabs = _DEFAULT_CONFIG["elevenlabs"]
    assert elevenlabs["stt_model"] not in retired
    assert elevenlabs["tts_model"] not in retired

    searched = [
        ROOT / "ai_race_engineer" / name for name in
        ("config.py", "orchestrator.py", "web_ui.py", "stt.py", "tts.py")
    ] + [ROOT / "ai_race_engineer" / "static" / "ui" / "panel.html"]

    for path in searched:
        text = path.read_text(encoding="utf-8")
        for model in retired:
            if path.name == "config.py" and model in _RETIRED_STT | _RETIRED_TTS.keys():
                # config.py names them on purpose: it is the table that
                # migrates them away.
                continue
            assert model not in text, f"{path.name} still names {model}"


def test_a_saved_config_is_moved_off_a_retired_model():
    """The id is persisted, so changing the default alone reaches nobody."""
    from ai_race_engineer.config import AppConfig

    config = AppConfig({"elevenlabs": {
        "stt_model": "scribe_v1", "tts_model": "eleven_turbo_v2_5"}})

    assert config.elevenlabs["stt_model"] == "scribe_v2"
    assert config.elevenlabs["tts_model"] == "eleven_flash_v2_5"

    # A model nobody retired is left exactly as the driver set it.
    kept = AppConfig({"elevenlabs": {"tts_model": "eleven_multilingual_v2"}})
    assert kept.elevenlabs["tts_model"] == "eleven_multilingual_v2"
