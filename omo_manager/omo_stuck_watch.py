#!/usr/bin/env python3
"""Check running OMO agents for likely stuck states using timing and latest turns."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_REGISTRY = Path(os.environ.get("OMO_MANAGER_SESSION_REGISTRY", default_state_dir() / "sessions.json"))
DEFAULT_DURATION = Path(os.environ.get("OMO_MANAGER_DURATION_HISTORY", default_state_dir() / "duration-history.json"))
RUNNING_COMMANDS = {"opencode", "claude", "codex"}


@dataclass(frozen=True)
class Args:
    registry: Path
    durations: Path
    now_s: float
    stale_factor: float
    min_stale_s: float
    complete_task: str
    complete_duration_s: float


class ParsedArgs(argparse.Namespace):
    registry: Path = DEFAULT_REGISTRY
    durations: Path = DEFAULT_DURATION
    now_s: float = 0.0
    stale_factor: float = 2.5
    min_stale_s: float = 1800.0
    complete_task: str = ""
    complete_duration_s: float = 0.0


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--durations", type=Path, default=DEFAULT_DURATION)
    _ = parser.add_argument("--now-s", type=float, default=0.0)
    _ = parser.add_argument("--stale-factor", type=float, default=2.5)
    _ = parser.add_argument("--min-stale-s", type=float, default=1800.0)
    _ = parser.add_argument("--complete-task", default="", help="Record a completed task duration and exit.")
    _ = parser.add_argument("--complete-duration-s", type=float, default=0.0)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.registry, parsed.durations, parsed.now_s or time.time(), parsed.stale_factor, parsed.min_stale_s, parsed.complete_task, parsed.complete_duration_s)


def read_json(path: Path, fallback: dict[str, object]) -> dict[str, object]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return fallback
    return data if isinstance(data, dict) else fallback


def write_json_private(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != Path("/tmp"):
        path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        _ = handle.write("\n")


def record_completion(args: Args) -> None:
    data = read_json(args.durations, {"tasks": {}})
    raw_tasks = data.setdefault("tasks", {})
    tasks = raw_tasks if isinstance(raw_tasks, dict) else {}
    values = tasks.setdefault(args.complete_task, [])
    if isinstance(values, list) and args.complete_duration_s > 0:
        values.append(args.complete_duration_s)
        del values[:-20]
    write_json_private(args.durations, data)


def tmux_command(target: str) -> str:
    out = subprocess.run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"], capture_output=True, text=True, timeout=5, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def learned_threshold(task_file: str, durations: object, factor: float, minimum_s: float) -> float:
    if not isinstance(durations, dict):
        return minimum_s
    tasks = durations.get("tasks")
    if not isinstance(tasks, dict):
        return minimum_s
    values = tasks.get(task_file)
    if not isinstance(values, list) or not values:
        return minimum_s
    nums = [float(value) for value in values if isinstance(value, (int, float)) and value > 0]
    return max(minimum_s, statistics.median(nums) * factor) if nums else minimum_s


def latest_turn_hint(session_id: str) -> str:
    if not session_id:
        return "no-session-id"
    out = subprocess.run([str(Path.home() / ".config/omo_manager/omo_oc_history.py"), "--session", session_id, "--limit", "4"], capture_output=True, text=True, timeout=20, check=False)
    if out.returncode != 0:
        return "history-unavailable"
    text = out.stdout.lower()
    if any(word in text for word in ("blocked", "stuck", "question", "permission", "waiting")):
        return "latest-turn-needs-manager"
    return "latest-turn-no-obvious-blocker"


def check(args: Args) -> int:
    registry = read_json(args.registry, {"sessions": []})
    durations = read_json(args.durations, {"tasks": {}})
    sessions = registry.get("sessions", []) if isinstance(registry, dict) else []
    if not isinstance(sessions, list):
        return 0
    for item in sessions:
        if not isinstance(item, dict):
            continue
        target = str(item.get("tmux_target", ""))
        if not target:
            continue
        task_file = str(item.get("task_file", ""))
        started_at_s = float(item.get("started_at_s", args.now_s))
        elapsed_s = max(0.0, args.now_s - started_at_s)
        command = tmux_command(target)
        if command not in RUNNING_COMMANDS:
            hint = latest_turn_hint(str(item.get("session_id", "")))
            shown_command = command or "unavailable"
            print(f"maybe-complete-silent: task={task_file} target={target} command={shown_command} elapsed_s={elapsed_s:.0f} hint={hint}")
            continue
        threshold_s = learned_threshold(task_file, durations, args.stale_factor, args.min_stale_s)
        state = "ok" if elapsed_s < threshold_s else "maybe-stuck"
        hint = latest_turn_hint(str(item.get("session_id", ""))) if state == "maybe-stuck" else "not-checked"
        print(f"{state}: task={task_file} target={target} elapsed_s={elapsed_s:.0f} threshold_s={threshold_s:.0f} hint={hint}")
    return 0


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.complete_task:
            record_completion(args)
            return 0
        return check(args)
    except Exception as exc:
        print(f"omo_stuck_watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
