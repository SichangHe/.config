#!/usr/bin/env python3
"""Serialize task ownership changes for one existing tmux target."""
from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def task_target_lock_path(root: Path, target: str) -> Path:
    """Return the deterministic per-user lock path for `root` and `target`."""

    key = hashlib.sha256(f"{root.resolve(strict=False)}\0{canonical_target(target)}".encode()).hexdigest()
    return Path("/tmp") / f"omo-task-target-locks-{os.getuid()}" / key


@contextmanager
def task_target_lock(root: Path, target: str) -> Iterator[None]:
    """Hold the cross-process ownership lock for `target` until the operation ends."""

    lock_path = task_target_lock_path(root, target)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def task_file_lock(path: Path) -> Iterator[None]:
    """Serialize compare-and-replace writers for one canonical task path."""

    key = hashlib.sha256(str(path.resolve(strict=False)).encode()).hexdigest()
    lock_path = Path("/tmp") / f"omo-task-file-locks-{os.getuid()}" / key
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
