#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Restart Qbot GUI: kill all main.py, then start exactly one instance."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _kill_existing() -> None:
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "restart_qbot.ps1"),
            ],
            check=False,
        )
        return

    subprocess.run(["pkill", "-f", str(MAIN)], capture_output=True)
    time.sleep(2)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.Popen(
        [sys.executable, str(MAIN)],
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
    )
    print("Qbot restarted (single instance)")


if __name__ == "__main__":
    if sys.platform == "win32":
        # ps1 已包含 kill + start
        sys.exit(0)
    _kill_existing()
