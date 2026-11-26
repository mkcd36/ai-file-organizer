"""Double-click launcher for the Sonar web UI (no console window)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sonar_web  # noqa: E402

raise SystemExit(sonar_web.main(sys.argv[1:]))
