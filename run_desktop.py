#!/usr/bin/env python3
"""Desktop entry point — the script PyInstaller freezes into AIRE.exe.

Running this directly is equivalent to `python -m ai_race_engineer --desktop`.
"""

import multiprocessing
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_race_engineer.cli import main  # noqa: E402

if __name__ == "__main__":
    # Required so a frozen build does not re-launch itself in child processes.
    multiprocessing.freeze_support()
    if "--server" not in sys.argv and "--desktop" not in sys.argv:
        sys.argv.append("--desktop")
    sys.exit(main())
