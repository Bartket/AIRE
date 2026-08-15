"""Documentation examples must describe the settings the app actually ships."""

import json
from pathlib import Path

from ai_race_engineer.config import _DEFAULT_CONFIG


ROOT = Path(__file__).resolve().parent.parent


def test_example_config_tracks_every_shipped_default():
    """The example had silently kept old voice settings and an unsafe prompt."""
    example = json.loads((ROOT / "config.json.example").read_text(encoding="utf-8"))

    assert example == _DEFAULT_CONFIG
