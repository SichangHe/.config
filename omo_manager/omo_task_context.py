#!/usr/bin/env python3
"""Resolve the active task owned by the current tmux pane."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_agent_status import resolve_task_path
from omo_manager.omo_agent_status import same_tmux_target

ACTIVE_STATUSES = {"running", "long_running", "blocked"}
LIVE_SECTIONS = {"todo:current", "todo:human pending", "todo:low priority", "todo:previous"}


def current_tmux_target() -> str:
    """Return the exact current tmux pane target or fail closed."""

    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane and not os.environ.get("TMUX"):
        raise TaskFrontmatterError("current tmux pane cannot be identified")
    command = ["tmux", "display-message", "-p"]
    if pane:
        command.extend(("-t", pane))
    command.append("#{session_name}:#{window_index}.#{pane_index}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskFrontmatterError("current tmux pane cannot be identified") from exc
    target = result.stdout.strip()
    if result.returncode != 0 or not target:
        raise TaskFrontmatterError("current tmux pane cannot be identified")
    return target


def infer_active_task(root: Path, target: str) -> Path:
    """Return the sole active task matching an exact tmux target."""

    matches: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.section not in LIVE_SECTIONS:
            continue
        path = resolve_task_path(root, task.task_file)
        if path is None or path in seen:
            continue
        seen.add(path)
        metadata = read_task_metadata(path)
        if metadata is None or metadata.status not in ACTIVE_STATUSES or not same_tmux_target(metadata.runat, target):
            continue
        matches.append((path, task.section))
    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        raise TaskFrontmatterError("no active work queue matches the current agent")
    raise TaskFrontmatterError("multiple active work queues match the current agent")


def current_active_task(root: Path) -> Path:
    """Resolve the current pane to one active task."""

    return infer_active_task(root, current_tmux_target())
