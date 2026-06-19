#!/usr/bin/env python3
"""Check registered agent panes with Codex tmux-tail status."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import inspect
from omo_manager.omo_codex_status import submit_stuck_input_if_present


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


DEFAULT_REGISTRY = Path(os.environ.get("OMO_MANAGER_SESSION_REGISTRY", default_state_dir() / "sessions.json"))
DEFAULT_STATE = Path(os.environ.get("OMO_MANAGER_STUCK_STATE", default_state_dir() / "codex-tail-state.json"))


@dataclass(frozen=True)
class Args:
    registry: Path
    state: Path
    n_lines: int
    stale_after_s: float
    watch: bool
    interval_s: float
    max_iterations: int
    manager_target: str = ""
    auto_unstick: bool = True


class ParsedArgs(argparse.Namespace):
    registry: Path = DEFAULT_REGISTRY
    state: Path = DEFAULT_STATE
    n_lines: int = 80
    stale_after_s: float = 900.0
    watch: bool = False
    interval_s: float = 60.0
    max_iterations: int = 1
    manager_target: str = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
    auto_unstick: bool = True


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    _ = parser.add_argument("--lines", type=int, default=80)
    _ = parser.add_argument("--stale-after-s", type=float, default=900.0)
    _ = parser.add_argument("--watch", action="store_true")
    _ = parser.add_argument("--interval-s", type=float, default=60.0)
    _ = parser.add_argument("--max-iterations", type=int, default=1)
    _ = parser.add_argument("--manager-target", default=os.environ.get("OMO_MANAGER_TMUX_TARGET", ""))
    _ = parser.add_argument("--no-auto-unstick", dest="auto_unstick", action="store_false")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.n_lines <= 0:
        parser.error("--lines must be positive.")
    if parsed.stale_after_s < 0:
        parser.error("--stale-after-s must be non-negative.")
    if parsed.interval_s <= 0:
        parser.error("--interval-s must be positive.")
    if parsed.max_iterations < 1:
        parser.error("--max-iterations must be positive.")
    return Args(parsed.registry, parsed.state, parsed.n_lines, parsed.stale_after_s, parsed.watch, parsed.interval_s, parsed.max_iterations, parsed.manager_target, parsed.auto_unstick)


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


def digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def target_state(raw: object) -> dict[str, object]:
    return raw if isinstance(raw, dict) else {}


def session_targets(sessions: list[object], manager_target: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in sessions:
        if not isinstance(item, dict):
            continue
        target = str(item.get("tmux_target", ""))
        if not target:
            continue
        targets.append((str(item.get("task_file", "")), target))
        seen.add(canonical_target(target))
    if manager_target and canonical_target(manager_target) not in seen:
        targets.append(("manager", manager_target))
    return targets


def check(args: Args) -> int:
    now_s = time.time()
    registry = read_json(args.registry, {"sessions": []})
    sessions = registry.get("sessions", []) if isinstance(registry, dict) else []
    if not isinstance(sessions, list):
        return 0
    state = read_json(args.state, {"targets": {}})
    targets = state.setdefault("targets", {})
    if not isinstance(targets, dict):
        targets = {}
        state["targets"] = targets
    unstick_by_target: dict[str, str] = {}
    for task_file, target in session_targets(sessions, args.manager_target):
        report = inspect(StatusArgs(target, args.n_lines))
        tail_hash = digest(report.lines)
        old = target_state(targets.get(target))
        old_hash = str(old.get("hash", ""))
        changed = old_hash != tail_hash
        first_seen_s = now_s if changed else float(old.get("first_seen_s", now_s))
        age_s = max(0.0, now_s - first_seen_s)
        status = "stale_running" if report.status == "running" and not changed and age_s >= args.stale_after_s else report.status
        targets[target] = {"hash": tail_hash, "first_seen_s": first_seen_s, "status": report.status}
        unstick = ""
        if report.status == "stuck_input":
            if args.auto_unstick:
                unstick_key = canonical_target(target)
                if unstick_key in unstick_by_target:
                    unstick = "already_sent" if unstick_by_target[unstick_key] == "sent_enter" else unstick_by_target[unstick_key]
                else:
                    unstick = submit_stuck_input_if_present(target, report)
                    unstick_by_target[unstick_key] = unstick
            else:
                unstick = "disabled:no_auto_unstick"
        unstick_text = f" unstick={unstick}" if unstick else ""
        print(f"{status}: task={task_file} target={target} changed={str(changed).lower()} same_tail_s={age_s:.0f}{unstick_text}", flush=True)
    write_json_private(args.state, state)
    return 0


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        for idx in range(args.max_iterations if args.watch else 1):
            rc = check(args)
            if not args.watch or idx == args.max_iterations - 1:
                return rc
            time.sleep(args.interval_s)
    except Exception as exc:
        print(f"omo_stuck_watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
