"""Locate bundled data files.

In a source checkout, assets live next to the package. In a PyInstaller
bundle they are unpacked into a temporary directory that only the frozen
process knows about, so every asset lookup has to go through here.
"""

import sys
from pathlib import Path


def resource_root() -> Path:
    """Directory that contains the bundled `static/` tree."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:  # running from a PyInstaller build
        return Path(bundle_dir)
    return Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle.

    The one copy of this question. `cli` asked it separately, which is two
    answers to a thing that has one.
    """
    return getattr(sys, "frozen", False)


STATIC_DIR = resource_root() / "static"
UI_DIR = STATIC_DIR / "ui"
ICON_PATH = STATIC_DIR / "icon.ico"
