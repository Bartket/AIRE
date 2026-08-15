#!/usr/bin/env python3
"""AI Race Engineer — entry point.

Launches the FastAPI web server (settings UI + test endpoints) together
with the background orchestration loop.
"""

import sys
from pathlib import Path

# Allow `python main.py` from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_race_engineer.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
