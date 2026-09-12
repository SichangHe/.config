#!/usr/bin/env python3
"""Run the task audit helper from the configured bin directory."""
from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    _ = runpy.run_path(str(Path(__file__).resolve().parents[1] / "omo_manager" / "omo_task_audit.py"), run_name="__main__")
