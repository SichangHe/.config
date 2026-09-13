#!/usr/bin/env python3
"""Restore one historical target label on an unchanged done TODO row."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import canonical_target, parse_task_metadata


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
MAX_BYTES = 64 * 1024 * 1024


class RestoreError(RuntimeError):
    """The exact historical-label restoration is unsafe or inapplicable."""


@dataclass(frozen=True)
class Snapshot:
    data: bytes
    state: os.stat_result


@dataclass(frozen=True)
class Args:
    root: Path
    task: Path
    target: str
    task_sha256: str
    todo_sha256: str
    expected_previous_count: int


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task: Path = Path()
    target: str = ""
    task_sha256: str = ""
    todo_sha256: str = ""
    expected_previous_count: int = -1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_snapshot(path: Path, label: str) -> Snapshot:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RestoreError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, MAX_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                break
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    finally:
        os.close(descriptor)

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
        )
    data = b"".join(chunks)
    if (
        identity(before) != identity(after)
        or identity(after) != identity(current)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or len(data) != before.st_size
        or len(data) > MAX_BYTES
    ):
        raise RestoreError(f"{label} changed or has an unsafe identity")
    return Snapshot(data, before)


def task_path(root: Path, value: Path) -> tuple[Path, str]:
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise RestoreError("task must be one canonical relative Markdown path")
    relative = value.as_posix()
    if not relative.endswith(".md"):
        raise RestoreError("task must be one canonical relative Markdown path")
    parent = root
    for part in value.parts[:-1]:
        parent /= part
        try:
            info = parent.lstat()
        except OSError as exc:
            raise RestoreError("task parent is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise RestoreError("task parent is not one real directory")
    return root / value, relative


def restored_todo(data: bytes, relative: str, target: str, expected_previous_count: int) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreError("TODO is not UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise RestoreError("TODO must use canonical LF-terminated text")
    lines = text.splitlines(keepends=True)
    section = ""
    matches: list[int] = []
    previous_rows = 0
    previous_headers = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(("#", "-")):
            section = stripped[:-1].casefold()
            if section == "previous":
                previous_headers += 1
            continue
        fields = stripped.split()
        if section == "previous" and fields and fields[0].endswith(".md"):
            previous_rows += 1
        if fields and fields[0] == relative:
            matches.append(index)
    if previous_headers != 1 or previous_rows != expected_previous_count:
        raise RestoreError("TODO does not have the required previous-row count")
    if len(matches) != 1:
        raise RestoreError("TODO must contain exactly one matching task row")
    index = matches[0]
    fields = lines[index].strip().split()
    if section_for_line(lines, index) != "previous" or fields != [relative]:
        raise RestoreError("task must have one canonical targetless previous row")
    lines[index] = f"{relative} {target}\n"
    return "".join(lines).encode()


def section_for_line(lines: list[str], target_index: int) -> str:
    for line in reversed(lines[:target_index]):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(("#", "-")):
            return stripped[:-1].casefold()
    return ""


def replace_todo(path: Path, before: Snapshot, updated: bytes) -> None:
    temporary: Path | None = None
    descriptor = -1
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, stat.S_IMODE(before.state.st_mode))
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            _ = stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current = read_snapshot(path, "TODO")
        if sha256(current.data) != sha256(before.data) or current.state.st_ino != before.state.st_ino:
            raise RestoreError("TODO changed before restoration")
        os.replace(temporary, path)
        temporary = None
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(args: Args) -> None:
    root = args.root.expanduser().resolve(strict=True)
    if not stat.S_ISDIR(root.stat().st_mode):
        raise RestoreError("root is not a directory")
    path, relative = task_path(root, args.task)
    todo_path = root / "TODO.md"
    with ExitStack() as locks:
        for locked in sorted((path, todo_path), key=str):
            locks.enter_context(task_file_lock(locked))
        task = read_snapshot(path, "task")
        todo = read_snapshot(todo_path, "TODO")
        if sha256(task.data) != args.task_sha256 or sha256(todo.data) != args.todo_sha256:
            raise RestoreError("task or TODO differs from the reviewed bytes")
        try:
            task_text = task.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RestoreError("task is not UTF-8") from exc
        metadata = parse_task_metadata(task_text, root)
        if metadata is None or metadata.status != "done" or metadata.runat != args.target:
            raise RestoreError("task is not one done record with the exact historical target")
        updated = restored_todo(todo.data, relative, args.target, args.expected_previous_count)
        # 🧑 “Preserve `config:24` on that previous row; because the task status is done, this label does not create active ownership.”
        replace_todo(todo_path, todo, updated)
        if read_snapshot(path, "task").data != task.data or read_snapshot(todo_path, "TODO").data != updated:
            raise RestoreError("post-restoration verification failed")


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task", type=Path, required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--expected-previous-count", type=int, required=True)
    namespace = parser.parse_args(argv, namespace=ParsedArgs())
    root = namespace.root
    task = namespace.task
    target = namespace.target
    task_sha256 = namespace.task_sha256
    todo_sha256 = namespace.todo_sha256
    expected_previous_count = namespace.expected_previous_count
    if TARGET_RE.fullmatch(target) is None or canonical_target(target) != target:
        parser.error("--target must be one canonical tmux target")
    if SHA256_RE.fullmatch(task_sha256) is None or SHA256_RE.fullmatch(todo_sha256) is None:
        parser.error("task and TODO SHA-256 values must be lowercase hexadecimal")
    if expected_previous_count < 0:
        parser.error("--expected-previous-count must be nonnegative")
    return Args(root, task, target, task_sha256, todo_sha256, expected_previous_count)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        restore(args)
    except (OSError, RestoreError, ValueError) as exc:
        print(f"omo_done_todo_target.py: {exc}", file=sys.stderr)
        return 2
    print(f"restored historical target {args.target} on {args.task.as_posix()}; task bytes unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
