#!/usr/bin/env python3
"""Run the done-task TODO target restoration helper."""
from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    _ = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "omo_manager" / "omo_done_todo_target.py"),
        run_name="__main__",
    )
