#!/usr/bin/env python3
"""Run the current-agent pending queue helper."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    helper_dir = Path(__file__).resolve().parents[1] / "omo_manager"
    helper_env = helper_dir / ".venv"
    if Path(sys.prefix).resolve() != helper_env.resolve():
        python = helper_env / "bin" / "python"
        os.execv(python, [python, __file__, *sys.argv[1:]])
    runpy.run_path(str(helper_dir / "omo_pending.py"), run_name="__main__")
