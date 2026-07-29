#!/usr/bin/env python3
"""Claim responsibility for a watcher-detected problem."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.amh_problem_claim import claim_problem, default_claim_path


def current_tmux_target() -> str:
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane:
        raise ValueError("run this command from the manager pane that received the problem")
    result = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#S:#I.#P"], capture_output=True, text=True, timeout=2, check=False)
    target = result.stdout.strip() if result.returncode == 0 else ""
    if not target or ":" not in target:
        raise ValueError("could not determine the current manager tmux target")
    return target[:-2] if target.endswith(".0") else target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    claim = subparsers.add_parser("claim", help="Suppress one unchanged problem for ten minutes while you perform a concrete next action.")
    claim.add_argument("problem_id")
    claim.add_argument("--action", required=True, help="One concrete next action you will perform during the lease.")
    args = parser.parse_args(argv)
    try:
        result = claim_problem(default_claim_path(), args.problem_id, current_tmux_target(), args.action)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"claimed {result.problem_id} for 10 minutes; the watcher alone decides when it is resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
