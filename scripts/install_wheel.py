"""Install the review wheel into the active environment without changing its lock."""

import subprocess
import sys
from pathlib import Path

wheels = list((Path(__file__).resolve().parents[1] / "dist").glob("*.whl"))
if len(wheels) != 1:
    raise SystemExit("Expected exactly one wheel in dist; remove stale builds and run uv build")
subprocess.run(
    [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--reinstall",
        "--no-deps",
        str(wheels[0]),
    ],
    check=True,
)
