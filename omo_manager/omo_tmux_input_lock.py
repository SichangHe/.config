#!/usr/bin/env python3
"""Cross-process lock for one exact tmux target's interactive input."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    from omo_manager.omo_task_lock import canonical_target, task_file_lock_at_path
except ModuleNotFoundError:
    from omo_task_lock import canonical_target, task_file_lock_at_path


def tmux_input_lock_path(target: str) -> Path:
    key = hashlib.sha256(canonical_target(target).encode()).hexdigest()
    return Path("/tmp") / f"omo-tmux-input-locks-{os.getuid()}" / key


@contextmanager
def tmux_input_lock(target: str) -> Iterator[None]:
    """Serialize supported senders and lifecycle replacement for one pane."""

    with task_file_lock_at_path(tmux_input_lock_path(target)):
        yield
