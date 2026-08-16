from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_module_entrypoint_exposes_safety_workflow() -> None:
    """The package entrypoint must advertise every step of Plan -> Apply."""

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, "-m", "slotlease", "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    help_text = f"{result.stdout}\n{result.stderr}".lower()
    for command in ("scan", "plan", "apply"):
        assert command in help_text
