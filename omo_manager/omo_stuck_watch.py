#!/usr/bin/env python3
"""Check registered agent panes with Codex tmux-tail status."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import inspect


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


DEFAULT_REGISTRY = Path(os.environ.get("OMO_MANAGER_SESSION_REGISTRY", default_state_dir() / "sessions.json"))
DEFAULT_STATE = Path(os.environ.get("OMO_MANAGER_STUCK_STATE", default_state_dir() / "codex-tail-state.json"))


@dataclass(frozen=True)
class Args:
    registry: Path
    state: Path
    n_lines: int


class ParsedArgs(argparse.Namespace):
    registry: Path = DEFAULT_REGISTRY
    state: Path = DEFAULT_STATE
    n_lines: int = 80


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    _ = parser.add_argument("--lines", type=int, default=80)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.n_lines <= 0:
        parser.error("--lines must be positive.")
    return Args(parsed.registry, parsed.state, parsed.n_lines)


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


def check(args: Args) -> int:
    registry = read_json(args.registry, {"sessions": []})
    sessions = registry.get("sessions", []) if isinstance(registry, dict) else []
    if not isinstance(sessions, list):
        return 0
    state = read_json(args.state, {"targets": {}})
    targets = state.setdefault("targets", {})
    if not isinstance(targets, dict):
        targets = {}
        state["targets"] = targets
    for item in sessions:
        if not isinstance(item, dict):
            continue
        target = str(item.get("tmux_target", ""))
        if not target:
            continue
        task_file = str(item.get("task_file", ""))
        report = inspect(StatusArgs(target, args.n_lines))
        tail_hash = digest(report.lines)
        old_hash = targets.get(target)
        changed = old_hash != tail_hash
        targets[target] = tail_hash
        print(f"{report.status}: task={task_file} target={target} changed={str(changed).lower()}")
    write_json_private(args.state, state)
    return 0


def main(argv: list[str]) -> int:
    try:
        return check(parse_args(argv))
    except Exception as exc:
        print(f"omo_stuck_watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
