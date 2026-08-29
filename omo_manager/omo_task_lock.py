#!/usr/bin/env python3
"""Serialize task ownership changes for one existing tmux target."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TMUX_TARGET_RE = re.compile(r"^([^:\s]+):(\d+)(?:\.(\d+))?$")


def canonical_target(target: str) -> str:
    match = TMUX_TARGET_RE.fullmatch(target)
    if match is None:
        return target
    session, window, pane = match.groups()
    canonical = f"{session}:{int(window)}"
    return canonical if pane is None or int(pane) == 0 else f"{canonical}.{int(pane)}"


def task_target_lock_path(root: Path, target: str) -> Path:
    """Return the deterministic per-user lock path for `root` and `target`."""

    key = hashlib.sha256(f"{root.resolve(strict=False)}\0{canonical_target(target)}".encode()).hexdigest()
    return Path("/tmp") / f"omo-task-target-locks-{os.getuid()}" / key


def task_file_lock_path(path: Path) -> Path:
    """Return the deterministic cross-process lock path for one task file."""

    key = hashlib.sha256(str(path.resolve(strict=False)).encode()).hexdigest()
    return Path("/tmp") / f"omo-task-file-locks-{os.getuid()}" / key


def watcher_report_manager_temporary(path: Path, key: str) -> Path:
    """Return the sole manager replacement temporary for one report receipt key."""

    digest = hashlib.sha256(key.encode()).hexdigest()
    return path.resolve(strict=False).parent / f".{path.name}.omo-watch-{digest}.tmp"


def watcher_report_state_temporary(state: Path, key: str) -> Path:
    """Return the sole acknowledgment replacement temporary for one report receipt key."""

    digest = hashlib.sha256(key.encode()).hexdigest()
    return state.resolve(strict=False).parent / f".{state.name}.omo-watch-{digest}.tmp"


def watcher_report_state_maintenance_temporary(state: Path) -> Path:
    """Return the sole acknowledgment replacement temporary for bounded maintenance."""

    return state.resolve(strict=False).parent / f".{state.name}.omo-watch-maintenance.tmp"


@contextmanager
def task_target_lock(root: Path, target: str) -> Iterator[None]:
    """Hold the cross-process ownership lock for `target` until the operation ends."""

    with task_file_lock_at_path(task_target_lock_path(root, target)):
        yield


@contextmanager
def task_file_lock_at_path(lock_path: Path) -> Iterator[None]:
    """Hold one already-resolved task-file lock path."""

    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(lock_path.parent, directory_flags)
    lock_fd = -1
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
            raise OSError("task-file lock directory is unsafe")
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path.name, lock_flags, 0o600, dir_fd=directory_fd)
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o022:
            raise OSError("task-file lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            bound_info = os.stat(lock_path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (bound_info.st_dev, bound_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise OSError("task-file lock entry changed during acquisition")
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


@contextmanager
def task_file_lock(path: Path) -> Iterator[None]:
    """Serialize compare-and-replace writers for one canonical task path."""

    with task_file_lock_at_path(task_file_lock_path(path)):
        yield


def process_start_ticks(pid: int) -> int | None:
    try:
        payload = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    separator = payload.rfind(") ")
    if separator < 0:
        return None
    fields = payload[separator + 2 :].split()
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def process_arguments(pid: int) -> tuple[str, ...] | None:
    try:
        payload = Path(f"/proc/{pid}/cmdline").read_bytes()
        if len(payload) > 64 * 1024:
            return None
        return tuple(item.decode("utf-8") for item in payload.split(b"\0") if item)
    except (OSError, UnicodeDecodeError):
        return None


def process_has_file(pid: int, *, device: int, inode: int) -> bool:
    try:
        process_directory = Path(f"/proc/{pid}")
        if process_directory.stat().st_uid != os.getuid():
            return False
        descriptors = tuple((process_directory / "fd").iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            info = descriptor.stat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (device, inode):
            return True
    return False


def argument_resolves_to(arguments: tuple[str, ...], expected: Path) -> bool:
    for argument in arguments:
        if not argument.startswith("/"):
            continue
        try:
            if Path(argument).resolve(strict=False) == expected:
                return True
        except OSError:
            continue
    return False


def watcher_report_authority_is_live(
    *,
    pid: int,
    start_ticks: int,
    lock_path: Path,
    lock_dev: int,
    lock_inode: int,
    source_path: Path,
    source_sha256: str,
    token_sha256: str,
) -> bool:
    """Verify the exact live helper lease for one watcher-consumed report."""

    if (
        pid <= 1
        or start_ticks <= 0
        or lock_dev < 0
        or lock_inode <= 0
        or source_path.name != "omo_task_lock.py"
        or HASH_RE.fullmatch(source_sha256) is None
        or HASH_RE.fullmatch(token_sha256) is None
        or process_start_ticks(pid) != start_ticks
    ):
        return False
    try:
        source_info = source_path.lstat()
        source = source_path.read_bytes()
    except OSError:
        return False
    if (
        not stat.S_ISREG(source_info.st_mode)
        or source_info.st_uid != os.getuid()
        or len(source) > 4 * 1024 * 1024
        or hashlib.sha256(source).hexdigest() != source_sha256
    ):
        return False
    arguments = process_arguments(pid)
    if arguments is None or not argument_resolves_to(arguments, source_path):
        return False
    if (
        "--hold-watcher-report-authority" not in arguments
        or str(lock_path) not in arguments
        or str(lock_path.with_name(f"{lock_path.name}.complete")) not in arguments
    ):
        return False
    if not any(HASH_RE.fullmatch(argument) is not None and hashlib.sha256(argument.encode()).hexdigest() == token_sha256 for argument in arguments):
        return False
    try:
        lock_info = lock_path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISREG(lock_info.st_mode)
        or lock_info.st_uid != os.getuid()
        or stat.S_IMODE(lock_info.st_mode) != 0o600
        or (lock_info.st_dev, lock_info.st_ino) != (lock_dev, lock_inode)
        or not process_has_file(pid, device=lock_dev, inode=lock_inode)
    ):
        return False
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags)
    except OSError:
        return False
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return not acquired


def hold_watcher_report_authority(argv: list[str]) -> int:
    """Keep an inherited watcher-consumption lock alive for a bounded replay window."""

    if len(argv) != 5:
        return 2
    fd_text, raw_path, token, ttl_text, raw_completion_path = argv
    try:
        fd = int(fd_text)
        ttl_s = float(ttl_text)
    except ValueError:
        return 2
    if fd < 0 or not 0 < ttl_s <= 3600 or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        return 2
    path = Path(raw_path).resolve(strict=False)
    completion_path = Path(raw_completion_path).resolve(strict=False)
    if completion_path != path.with_name(f"{path.name}.complete"):
        return 2
    info: os.stat_result | None = None
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        ):
            return 2
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + ttl_s
        completion_deadline: float | None = None
        while True:
            now_s = time.monotonic()
            remaining_s = min(deadline, completion_deadline or deadline) - now_s
            if remaining_s <= 0:
                return 0
            try:
                current = path.lstat()
            except FileNotFoundError:
                return 0
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                return 0
            try:
                completion_info = completion_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    not stat.S_ISREG(completion_info.st_mode)
                    or completion_info.st_uid != os.getuid()
                    or stat.S_IMODE(completion_info.st_mode) != 0o600
                ):
                    return 2
                if completion_deadline is None:
                    completion_deadline = now_s + 2.0
            time.sleep(min(0.2, remaining_s))
    except (OSError, ValueError):
        return 2
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if info is not None:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    path.unlink()
            except OSError:
                pass
        try:
            completion_info = completion_path.lstat()
            if stat.S_ISREG(completion_info.st_mode) and completion_info.st_uid == os.getuid():
                completion_path.unlink()
        except OSError:
            pass
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--hold-watcher-report-authority":
        raise SystemExit(hold_watcher_report_authority(sys.argv[2:]))
    raise SystemExit(2)
