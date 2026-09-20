#!/usr/bin/env python3
"""Cross-process lock for one exact tmux target's interactive input."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    from omo_manager.omo_task_lock import canonical_target, process_start_ticks, task_file_lock_at_path
except ModuleNotFoundError:
    from omo_task_lock import canonical_target, process_start_ticks, task_file_lock_at_path


class _ThreadLockState(threading.local):
    pid: int
    depths: dict[Path, int]

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.depths = {}


_THREAD_LOCK_STATE = _ThreadLockState()


@dataclass(frozen=True)
class TmuxRuntimeBinding:
    """One complete tmux pane and kernel process identity."""

    target: str
    pane_id: str
    window_id: str
    pane_pid: int
    pane_command: str
    pane_cwd: Path
    pane_start_ticks: int


def capture_tmux_runtime_binding(target: str) -> TmuxRuntimeBinding:
    """Resolve one exact target and bind its pane process start identity."""

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{window_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    fields = (result.stdout or "").rstrip("\r\n").split("\t") if result.returncode == 0 else []
    if (
        len(fields) != 6
        or (
            fields[1] != target
            if re.fullmatch(r"%[0-9]+", target) is not None
            else canonical_target(fields[0]) != canonical_target(target)
        )
        or re.fullmatch(r"%[0-9]+", fields[1]) is None
        or re.fullmatch(r"@[0-9]+", fields[2]) is None
        or not fields[3].isdigit()
        or int(fields[3]) <= 1
        or not fields[4]
        or not Path(fields[5]).is_absolute()
    ):
        raise RuntimeError("tmux runtime identity cannot be authenticated")
    pane_pid = int(fields[3])
    start_ticks = process_start_ticks(pane_pid)
    if start_ticks is None or start_ticks <= 1:
        raise RuntimeError("tmux runtime process start identity cannot be authenticated")
    return TmuxRuntimeBinding(fields[0], fields[1], fields[2], pane_pid, fields[4], Path(fields[5]), start_ticks)


def exact_tmux_runtime_condition(binding: TmuxRuntimeBinding) -> str:
    """Return one tmux-format predicate for every tmux-visible field."""

    values = (
        binding.pane_id,
        binding.window_id,
        binding.target,
        str(binding.pane_pid),
        binding.pane_command,
        str(binding.pane_cwd),
    )
    if any(any(character in value for character in "#,{}") for value in values):
        raise RuntimeError("tmux runtime identity cannot be represented safely")
    return "#{&&:#{==:#{pane_id},%s},#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s},#{==:#{pane_current_path},%s}}" % values


def guarded_tmux_runtime_command(
    binding: TmuxRuntimeBinding,
    action: str,
    rejected: str = "run-shell 'exit 1'",
) -> list[str]:
    """Build one tmux action guarded by pane fields and `/proc` start ticks."""

    start_guard = start_ticks_guarded_tmux_action(binding, action, rejected)
    return [
        "tmux",
        "if-shell",
        "-F",
        "-t",
        binding.pane_id,
        exact_tmux_runtime_condition(binding),
        start_guard,
        rejected,
    ]


def start_ticks_guarded_tmux_action(binding: TmuxRuntimeBinding, action: str, rejected: str) -> str:
    """Nest one action behind the binding's exact `/proc` start identity."""

    predicate = (
        f"test {binding.pane_pid} -gt 1 && test {binding.pane_start_ticks} -gt 1 && "
        f"IFS= read -r stat < /proc/{binding.pane_pid}/stat && "
        "rest=${stat##*) } && set -- $rest && "
        f'test "${{20:-}}" = {binding.pane_start_ticks}'
    )
    return f"if-shell {shlex.quote(predicate)} {shlex.quote(action)} {shlex.quote(rejected)}"


def require_same_tmux_runtime(binding: TmuxRuntimeBinding) -> None:
    """Reject target, pane, cwd, or process reuse since the original capture."""

    if capture_tmux_runtime_binding(binding.target) != binding:
        raise RuntimeError("tmux runtime identity changed")


def tmux_input_lock_path(target: str) -> Path:
    key = hashlib.sha256(canonical_target(target).encode()).hexdigest()
    return Path("/tmp") / f"omo-tmux-input-locks-{os.getuid()}" / key


def _thread_lock_depths() -> dict[Path, int]:
    """Return this process thread's lock depths, resetting after `fork`."""

    pid = os.getpid()
    if _THREAD_LOCK_STATE.pid != pid:
        _THREAD_LOCK_STATE.pid = pid
        _THREAD_LOCK_STATE.depths = {}
    return _THREAD_LOCK_STATE.depths


@contextmanager
def tmux_input_lock(target: str) -> Iterator[None]:
    """Serialize pane input mutations; allow same-thread nested public APIs."""

    path = tmux_input_lock_path(target)
    depths = _thread_lock_depths()
    depth = depths.get(path, 0)
    if depth:
        depths[path] = depth + 1
        try:
            yield
        finally:
            if depths[path] == 1:
                del depths[path]
            else:
                depths[path] -= 1
        return
    with task_file_lock_at_path(path):
        depths[path] = 1
        try:
            yield
        finally:
            del depths[path]
