"""Allow running as python -m ai_race_engineer."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
