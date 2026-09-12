#!/usr/bin/env python3
"""Resolve the active task owned by the current tmux pane."""
from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_agent_status import resolve_task_path
from omo_manager.omo_agent_status import same_tmux_target

RUNNING_STATUSES = {"running", "long_running"}
ACTIVE_STATUSES = RUNNING_STATUSES | {"blocked"}
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


def _active_task_matches(root: Path, target: str) -> list[tuple[Path, str]]:
    """Return each TODO-linked active task and status for an exact target."""

    matches: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.section not in LIVE_SECTIONS:
            continue
        path = resolve_task_path(root, task.task_file)
        if path is None or path in seen:
            continue
        seen.add(path)
        metadata = read_task_metadata(path, root)
        if metadata is None or metadata.status not in ACTIVE_STATUSES or not same_tmux_target(metadata.runat, target):
            continue
        matches.append((path, metadata.status))
    return matches


def infer_active_task(root: Path, target: str) -> Path:
    """Return the sole active task matching an exact tmux target."""

    matches = _active_task_matches(root, target)
    if len(matches) == 1:
        return matches[0][0]
    if not matches:
        raise TaskFrontmatterError("no active work queue matches the current agent")
    raise TaskFrontmatterError("multiple active work queues match the current agent")


# 🧑 “Long running simply means that the agent will not be closed if they have zero pending item.”
def infer_pending_task(root: Path, target: str) -> Path:
    """Return the sole queue owner, preferring one runnable task over blocked history."""

    matches = _active_task_matches(root, target)
    if len(matches) == 1:
        return matches[0][0]
    running = [path for path, status in matches if status in RUNNING_STATUSES]
    if len(running) == 1:
        return running[0]
    if not matches:
        raise TaskFrontmatterError("no active work queue matches the current agent")
    raise TaskFrontmatterError("multiple active work queues match the current agent")


def _current_task(root: Path, infer: Callable[[Path, str], Path], operation: str) -> Path:
    """Resolve and authenticate the current pane with one task-selection rule."""

    try:
        return infer(root, current_tmux_target())
    except TaskFrontmatterError as direct_error:
        if str(direct_error) != "current tmux pane cannot be identified":
            raise
        # A sandboxed owner may inherit the correct TMUX_PANE while being unable
        # to open tmux's socket.  The watcher actor authenticates the Unix peer,
        # pane process ancestry, and sole live task using its trusted connection.
        try:
            from omo_manager.omo_blocking_actor import request

            result = request(root, {"operation": operation})
            relative = result.get("task")
            target = result.get("target")
            task_sha256 = result.get("task_sha256")
            todo_sha256 = result.get("todo_sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(target, str)
                or not target
                or not isinstance(task_sha256, str)
                or not isinstance(todo_sha256, str)
            ):
                raise TaskFrontmatterError("current work queue actor returned an invalid task")
            path = (root / relative).resolve(strict=False)
            try:
                path.relative_to(root.resolve())
            except ValueError as exc:
                raise TaskFrontmatterError("current work queue actor returned an invalid task") from exc
            task_payload = path.read_bytes()
            todo_payload = (root / "TODO.md").read_bytes()
            metadata = read_task_metadata(path, root)
            if (
                hashlib.sha256(task_payload).hexdigest() != task_sha256
                or hashlib.sha256(todo_payload).hexdigest() != todo_sha256
                or metadata is None
                or metadata.status not in ACTIVE_STATUSES
                or infer(root, target) != path
            ):
                raise TaskFrontmatterError("current work queue actor returned an inactive task")
            return path
        except TaskFrontmatterError:
            raise
        except Exception as exc:
            raise direct_error from exc


def current_active_task(root: Path) -> Path:
    """Resolve the current pane to one strictly unambiguous active task."""

    return _current_task(root, infer_active_task, "active-task")


def current_pending_task(root: Path) -> Path:
    """Resolve the current pane to its queue-owning active task."""

    return _current_task(root, infer_pending_task, "pending-task")
