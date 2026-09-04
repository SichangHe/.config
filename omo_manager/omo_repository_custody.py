#!/usr/bin/env python3
"""Prepare and commit an immutable ledger for untracked-file custody transfer."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from omo_manager.omo_report_receipt import TRANSACTION_COMMITMENT_SCHEMA, bound_receipt_id
from omo_manager.omo_task_lock import process_start_ticks
from omo_manager.omo_task_metadata import parse_task_metadata

SCHEMA = "omo-repository-custody-binding/v1"
ACCEPTANCE_SCHEMA = "omo-repository-custody-acceptance/v1"
REVIEW_SCHEMA = "omo-repository-custody-review/v1"
JOURNAL_SCHEMA = "omo-repository-custody-journal/v1"
LEDGER_SCHEMA = "omo-repository-custody-ledger/v1"
COMMIT_SCHEMA = "omo-repository-custody-commit/v1"
SHA256_LENGTH = 64
GIT_EXECUTABLE = Path("/usr/bin/git")
GIT_EXECUTABLE_SHA256 = "587ef21868c948b883993e23209b86a72a6ddc06aab1545c697ffc31075acd4a"
TMUX_EXECUTABLE = Path("/nix/store/v8kr4i8c12fjrsgh1r7v52vcpyfqy160-tmux-3.6a/bin/tmux")
TMUX_EXECUTABLE_SHA256 = "89c688f10e06baf9fe49724d6bef64c56354978b03e90414ccebbe0c80993b4d"
TMUX_SOCKET = Path("/tmp/tmux-30033/default")


class CustodyError(RuntimeError):
    """A custody invariant failed without changing repository or source state."""


@dataclass(frozen=True)
class FileIdentity:
    path: str
    mode: int
    device: int
    inode: int
    uid: int
    gid: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    mode: int
    device: int
    inode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class HeldSource:
    directories: tuple[int, ...]
    directory_identities: tuple[DirectoryIdentity, ...]
    leaf_name: str
    descriptor: int
    identity: FileIdentity


@dataclass(frozen=True)
class HeldAbsolute:
    directories: tuple[int, ...]
    directory_identities: tuple[DirectoryIdentity, ...]
    leaf_name: str
    descriptor: int
    identity: FileIdentity


@dataclass(frozen=True)
class TaskIdentity:
    path: str
    sha256: str
    mode: int
    device: int
    inode: int
    uid: int
    gid: int
    size_bytes: int
    status: str
    runat: str
    managerat: str
    session_id: str


@dataclass(frozen=True)
class TodoIdentity:
    path: str
    sha256: str
    mode: int
    device: int
    inode: int
    uid: int
    gid: int
    size_bytes: int
    source_row: str
    destination_row: str


@dataclass(frozen=True)
class TargetIdentity:
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    command: str


@dataclass(frozen=True)
class RepositoryIdentity:
    path: str
    device: int
    inode: int
    branch: str
    head: str
    upstream: str
    upstream_ref: str
    upstream_remote: str
    remote_url: str
    ahead: int
    behind: int
    index_path: str
    index_mode: int
    index_device: int
    index_inode: int
    index_uid: int
    index_gid: int
    index_size_bytes: int
    index_sha256: str
    porcelain_size_bytes: int
    porcelain_sha256: str


@dataclass(frozen=True)
class PrepareArgs:
    repository: Path
    paths: tuple[str, ...]
    expected_path_sha256: tuple[str, ...]
    source_owner: str
    destination_owner: str
    source_task: Path
    source_task_sha256: str
    source_target: str
    destination_task: Path
    destination_task_sha256: str
    destination_target: str
    todo_file: Path
    todo_sha256: str
    authority_file: Path
    authority_sha256: str
    source_receipts: tuple[Path, ...]
    source_receipt_sha256s: tuple[str, ...]
    acceptance_file: Path
    acceptance_sha256: str
    output: Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def object_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CustodyError(f"{label} must be an object with string keys")
    return {str(key): item for key, item in value.items()}


def string_field(value: dict[str, object], key: str, label: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        raise CustodyError(f"{label} has an invalid {key}")
    return field


def integer_field(value: dict[str, object], key: str, label: str) -> int:
    field = value.get(key)
    if not isinstance(field, int) or isinstance(field, bool):
        raise CustodyError(f"{label} has an invalid {key}")
    return field


def require_sha256(value: str, label: str) -> None:
    if len(value) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise CustodyError(f"{label} must be one lowercase SHA-256")


def read_regular_nofollow(path: Path, label: str, *, private: bool = False) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CustodyError(f"cannot open {label} without following links: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CustodyError(f"{label} must be one unlinked-elsewhere regular file")
        if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077):
            raise CustodyError(f"{label} must be owner-private")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if identity_tuple(before) != identity_tuple(after):
            raise CustodyError(f"{label} changed while read")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def absolute_file_binding(
    path: Path,
    label: str,
    *,
    private: bool = False,
) -> tuple[bytes, FileIdentity, tuple[DirectoryIdentity, ...]]:
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise CustodyError(f"{label} must be one normalized absolute path")
    components = path.parts[1:]
    directories: list[int] = []
    ancestors: list[DirectoryIdentity] = []
    descriptor = -1
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        directories.append(parent)
        ancestors.append(directory_identity("/", os.fstat(parent)))
        current = Path("/")
        for component in components[:-1]:
            parent = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            directories.append(parent)
            current /= component
            ancestors.append(directory_identity(str(current), os.fstat(parent)))
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        before = os.fstat(descriptor)
        linked = os.stat(components[-1], dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or identity_tuple(before) != identity_tuple(linked)
            or (private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077))
        ):
            raise CustodyError(f"{label} must be one unambiguous regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(components[-1], dir_fd=parent, follow_symlinks=False)
        if identity_tuple(before) != identity_tuple(after) or identity_tuple(before) != identity_tuple(rebound):
            raise CustodyError(f"{label} changed while read")
        data = b"".join(chunks)
        return data, FileIdentity(
            str(path),
            stat.S_IMODE(before.st_mode),
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_gid,
            before.st_size,
            digest(data),
        ), tuple(ancestors)
    except OSError as exc:
        raise CustodyError(f"cannot open {label} through its no-follow path chain: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for opened in reversed(directories):
            os.close(opened)


def identity_tuple(details: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        details.st_mode,
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_gid,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
    )


def private_output_parent(path: Path) -> Path:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CustodyError("output must be one absolute file path")
    parent = path.parent.resolve(strict=True)
    details = parent.stat()
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise CustodyError("output parent must be owner-private")
    return parent


def link_descriptor_noreplace(descriptor: int, parent_descriptor: int, name: str) -> None:
    linkat = ctypes.CDLL(None, use_errno=True).linkat
    linkat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    linkat.restype = ctypes.c_int
    descriptor_path = os.fsencode(f"/proc/self/fd/{descriptor}")
    if linkat(-100, descriptor_path, parent_descriptor, os.fsencode(name), 0x400) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise CustodyError(f"refusing reused output: {name}")
    raise CustodyError(f"descriptor publication failed for {name}: {os.strerror(error)}")


def write_new_private(path: Path, data: bytes) -> None:
    parent = private_output_parent(path)
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        descriptor = os.open(
            ".",
            os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_TMPFILE", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
        link_descriptor_noreplace(descriptor, parent_descriptor, path.name)
        details = os.fstat(descriptor)
        linked = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if identity_tuple(details) != identity_tuple(linked) or details.st_nlink != 1:
            raise CustodyError(f"indeterminate descriptor publication for {path}")
        os.fsync(parent_descriptor)
    except OSError as exc:
        raise CustodyError(f"descriptor publication failed for {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def git(repository: Path, *arguments: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(GIT_EXECUTABLE, flags)
    except OSError as exc:
        raise CustodyError(f"cannot open the trusted Git executable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o755
        ):
            raise CustodyError("trusted Git executable identity is invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if identity_tuple(before) != identity_tuple(after) or digest(b"".join(chunks)) != GIT_EXECUTABLE_SHA256:
            raise CustodyError("trusted Git executable changed or has the wrong digest")
        executable = f"/proc/self/fd/{descriptor}"
        result = subprocess.run(
            [
                executable,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(repository),
                *arguments,
            ],
            check=False,
            capture_output=True,
            env={
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
            executable=executable,
            pass_fds=(descriptor,),
        )
    finally:
        os.close(descriptor)
    if result.returncode:
        raise CustodyError(f"Git identity command failed: {' '.join(arguments)}")
    return result.stdout


def repository_identity(repository: Path) -> tuple[RepositoryIdentity, tuple[DirectoryIdentity, ...]]:
    root = Path(os.fsdecode(git(repository, "rev-parse", "--show-toplevel").strip())).resolve(strict=True)
    if root != repository.resolve(strict=True):
        raise CustodyError("repository must be the exact Git worktree root")
    details = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode):
        raise CustodyError("repository must be a directory")
    def ref_state() -> tuple[str, str, str, str, str, str, int, int]:
        branch = os.fsdecode(git(root, "branch", "--show-current").strip())
        head = os.fsdecode(git(root, "rev-parse", "HEAD").strip())
        upstream = os.fsdecode(git(root, "rev-parse", "@{upstream}").strip())
        upstream_ref = os.fsdecode(git(root, "rev-parse", "--symbolic-full-name", "@{upstream}").strip())
        upstream_remote = os.fsdecode(git(root, "config", "--get", f"branch.{branch}.remote").strip())
        remote_url = os.fsdecode(git(root, "remote", "get-url", upstream_remote).strip())
        divergence = os.fsdecode(git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}").strip()).split()
        if len(divergence) != 2 or any(not value.isdecimal() for value in divergence):
            raise CustodyError("repository upstream divergence is invalid")
        return branch, head, upstream, upstream_ref, upstream_remote, remote_url, int(divergence[0]), int(divergence[1])

    refs = ref_state()
    index_raw = git(root, "rev-parse", "--git-path", "index").strip()
    index_path = Path(os.fsdecode(index_raw))
    if not index_path.is_absolute():
        index_path = root / index_path
    porcelain = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
    index_data, index_identity, index_ancestors = absolute_file_binding(index_path.resolve(strict=True), "Git index")
    porcelain_after = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
    index_after, index_details_after = read_regular_nofollow(index_path.resolve(strict=True), "Git index")
    if (
        refs != ref_state()
        or porcelain != porcelain_after
        or index_data != index_after
        or (
            index_identity.mode,
            index_identity.device,
            index_identity.inode,
            index_identity.uid,
            index_identity.gid,
            index_identity.size_bytes,
        )
        != (
            stat.S_IMODE(index_details_after.st_mode),
            index_details_after.st_dev,
            index_details_after.st_ino,
            index_details_after.st_uid,
            index_details_after.st_gid,
            index_details_after.st_size,
        )
    ):
        raise CustodyError("repository index or porcelain state changed while bound")
    return RepositoryIdentity(
        str(root),
        details.st_dev,
        details.st_ino,
        *refs,
        str(index_path.resolve(strict=True)),
        index_identity.mode,
        index_identity.device,
        index_identity.inode,
        index_identity.uid,
        index_identity.gid,
        index_identity.size_bytes,
        digest(index_data),
        len(porcelain),
        digest(porcelain),
    ), index_ancestors


def directory_identity(path: str, details: os.stat_result) -> DirectoryIdentity:
    if not stat.S_ISDIR(details.st_mode):
        raise CustodyError(f"source ancestor is not a directory: {path}")
    return DirectoryIdentity(
        path,
        stat.S_IMODE(details.st_mode),
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_gid,
    )


def relative_regular_identity(repository: Path, relative: str) -> tuple[FileIdentity, tuple[DirectoryIdentity, ...]]:
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.as_posix() != relative or ".." in candidate.parts or not candidate.parts:
        raise CustodyError(f"unsafe repository-relative path: {relative}")
    root_fd = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    opened: list[int] = [root_fd]
    ancestors = [directory_identity(".", os.fstat(root_fd))]
    try:
        parent_fd = root_fd
        for component in candidate.parts[:-1]:
            parent_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened.append(parent_fd)
            ancestors.append(
                directory_identity(Path(*candidate.parts[: len(ancestors)]).as_posix(), os.fstat(parent_fd))
            )
        descriptor = os.open(
            candidate.parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened.append(descriptor)
        before = os.fstat(descriptor)
        path_details = os.stat(candidate.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or identity_tuple(before) != identity_tuple(path_details):
            raise CustodyError(f"source path is not one unambiguous regular file: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(candidate.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if identity_tuple(before) != identity_tuple(after) or identity_tuple(before) != identity_tuple(rebound):
            raise CustodyError(f"source path changed while read: {relative}")
        return (
            FileIdentity(
                relative,
                stat.S_IMODE(before.st_mode),
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_gid,
                before.st_size,
                digest(b"".join(chunks)),
            ),
            tuple(ancestors),
        )
    except OSError as exc:
        raise CustodyError(f"cannot bind source path {relative}: {exc}") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def task_identity(
    path: Path,
    expected_sha256: str,
    expected_target: str,
    label: str,
) -> tuple[TaskIdentity, tuple[DirectoryIdentity, ...]]:
    data, file_identity, ancestors = absolute_file_binding(path, f"{label} task")
    if digest(data) != expected_sha256:
        raise CustodyError(f"{label} task digest changed")
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise CustodyError(f"{label} task is not UTF-8") from exc
    metadata = parse_task_metadata(text, path.parent)
    if metadata is None or metadata.runat != expected_target:
        raise CustodyError(f"{label} task target changed")
    return TaskIdentity(
        file_identity.path,
        expected_sha256,
        file_identity.mode,
        file_identity.device,
        file_identity.inode,
        file_identity.uid,
        file_identity.gid,
        file_identity.size_bytes,
        metadata.status,
        metadata.runat,
        metadata.managerat,
        metadata.session_id or "",
    ), ancestors


def todo_identity(
    path: Path,
    expected_sha256: str,
    source_task: Path,
    source_target: str,
    destination_task: Path,
    destination_target: str,
) -> tuple[TodoIdentity, tuple[DirectoryIdentity, ...]]:
    data, file_identity, ancestors = absolute_file_binding(path, "TODO index")
    if digest(data) != expected_sha256:
        raise CustodyError("TODO index digest changed")
    try:
        lines = data.decode().splitlines()
        source_name = source_task.relative_to(path.parent).as_posix()
        destination_name = destination_task.relative_to(path.parent).as_posix()
    except (UnicodeDecodeError, ValueError) as exc:
        raise CustodyError("TODO index or task path is invalid") from exc
    source_row = f"{source_name} {source_target}"
    destination_row = f"{destination_name} {destination_target}"
    if lines.count(source_row) != 1 or lines.count(destination_row) != 1:
        raise CustodyError("TODO index does not contain each exact custody row once")
    return TodoIdentity(
        file_identity.path,
        expected_sha256,
        file_identity.mode,
        file_identity.device,
        file_identity.inode,
        file_identity.uid,
        file_identity.gid,
        file_identity.size_bytes,
        source_row,
        destination_row,
    ), ancestors


def target_identity(target: str) -> TargetIdentity:
    try:
        socket_before = os.stat(TMUX_SOCKET, follow_symlinks=False)
    except OSError as exc:
        raise CustodyError(f"cannot inspect the trusted tmux socket: {exc}") from exc
    if not stat.S_ISSOCK(socket_before.st_mode) or socket_before.st_uid != os.getuid() or socket_before.st_nlink != 1:
        raise CustodyError("trusted tmux socket identity is invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(TMUX_EXECUTABLE, flags)
    except OSError as exc:
        raise CustodyError(f"cannot open the trusted tmux executable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o555
        ):
            raise CustodyError("trusted tmux executable identity is invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if identity_tuple(before) != identity_tuple(after) or digest(b"".join(chunks)) != TMUX_EXECUTABLE_SHA256:
            raise CustodyError("trusted tmux executable changed or has the wrong digest")
        executable = f"/proc/self/fd/{descriptor}"
        result = subprocess.run(
            [
                executable,
                "-N",
                "-S",
                str(TMUX_SOCKET),
                "list-panes",
                "-a",
                "-F",
                r"#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "TMPDIR": "/tmp"},
            executable=executable,
            pass_fds=(descriptor,),
        )
    finally:
        os.close(descriptor)
    try:
        socket_after = os.stat(TMUX_SOCKET, follow_symlinks=False)
    except OSError as exc:
        raise CustodyError(f"trusted tmux socket disappeared during identity lookup: {exc}") from exc
    if identity_tuple(socket_before) != identity_tuple(socket_after):
        raise CustodyError("trusted tmux socket changed during identity lookup")
    if result.returncode:
        raise CustodyError("cannot inspect tmux target identity")
    canonical = target if "." in target.partition(":")[2] else f"{target}.0"
    rows = [line.split(r"\t") for line in result.stdout.splitlines() if line.split(r"\t", 1)[0] == canonical]
    if len(rows) != 1 or len(rows[0]) != 4:
        raise CustodyError(f"target must resolve to exactly one live pane: {target}")
    _, pane_id, raw_pid, command = rows[0]
    try:
        pane_pid = int(raw_pid)
    except ValueError as exc:
        raise CustodyError(f"target has an invalid pane PID: {target}") from exc
    ticks = process_start_ticks(pane_pid)
    if ticks is None:
        raise CustodyError(f"target process identity is unavailable: {target}")
    return TargetIdentity(canonical, pane_id, pane_pid, ticks, command)


def canonical_target(target: str) -> str:
    return target if "." in target.partition(":")[2] else f"{target}.0"


def executor_belongs_to_target(pane_pid: int) -> bool:
    current = os.getpid()
    visited: set[int] = set()
    while current > 1 and current not in visited:
        if current == pane_pid:
            return True
        visited.add(current)
        try:
            status_text = Path(f"/proc/{current}/status").read_text()
        except (OSError, UnicodeError):
            return False
        parent_lines = [line for line in status_text.splitlines() if line.startswith("PPid:")]
        if len(parent_lines) != 1:
            return False
        try:
            current = int(parent_lines[0].split()[1])
        except (IndexError, ValueError):
            return False
    return current == pane_pid


def authenticated_file(
    path: Path,
    expected_sha256: str,
    label: str,
) -> tuple[bytes, FileIdentity, tuple[DirectoryIdentity, ...]]:
    require_sha256(expected_sha256, label)
    data, identity, ancestors = absolute_file_binding(path, label, private=True)
    if digest(data) != expected_sha256:
        raise CustodyError(f"{label} digest changed")
    return data, identity, ancestors


def authenticated_report(data: bytes, label: str) -> dict[str, object]:
    lines = data.splitlines()
    hash_lines = [line for line in lines if line.startswith(b"[message-sha256: ") and line.endswith(b"]")]
    transfer_lines = [line for line in lines if line.startswith(b"[omo-transfer: ") and line.endswith(b"]")]
    marker = b"message:\n"
    if len(hash_lines) != 1 or len(transfer_lines) != 1 or data.count(marker) != 1:
        raise CustodyError(f"{label} is not one authenticated report envelope")
    message_sha256 = hash_lines[0][len(b"[message-sha256: ") : -1].decode()
    require_sha256(message_sha256, f"{label} message")
    if digest(data.split(marker, 1)[1]) != message_sha256:
        raise CustodyError(f"{label} body digest is invalid")
    try:
        transfer_value: object = json.loads(transfer_lines[0][len(b"[omo-transfer: ") : -1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(f"{label} transfer receipt is invalid") from exc
    transfer = object_map(transfer_value, f"{label} transfer receipt")
    transfer_id = transfer.pop("transfer_id", None)
    if not isinstance(transfer_id, str) or transfer_id != bound_receipt_id(transfer):
        raise CustodyError(f"{label} transfer receipt identity is invalid")
    commitment_id = transfer.get("commitment_id")
    commitment_path = transfer.get("commitment_path")
    if not isinstance(commitment_id, str) or not isinstance(commitment_path, str):
        raise CustodyError(f"{label} lacks its transaction commitment")
    commitment_data, commitment_identity, commitment_ancestors = absolute_file_binding(
        Path(commitment_path),
        f"{label} transaction commitment",
        private=True,
    )
    try:
        commitment_value: object = json.loads(commitment_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(f"{label} transaction commitment is invalid") from exc
    commitment = object_map(commitment_value, f"{label} transaction commitment")
    if canonical_json(commitment) != commitment_data:
        raise CustodyError(f"{label} transaction commitment is not canonical")
    recorded_commitment_id = commitment.pop("commitment_id", None)
    expected_transfer = {key: value for key, value in transfer.items() if key != "commitment_id"}
    if (
        commitment.get("schema") != TRANSACTION_COMMITMENT_SCHEMA
        or recorded_commitment_id != commitment_id
        or recorded_commitment_id != bound_receipt_id(commitment)
        or commitment.get("transfer") != expected_transfer
    ):
        raise CustodyError(f"{label} transaction commitment does not authenticate the report")
    authority = object_map(transfer.get("authority"), f"{label} report authority")
    routing = object_map(transfer.get("routing"), f"{label} report routing")
    if authority.get("producer_target") != routing.get("producer_target") or authority.get("source_task") != routing.get("task"):
        raise CustodyError(f"{label} producer identity is inconsistent")
    return {
        "message_sha256": message_sha256,
        "producer_target": authority["producer_target"],
        "source_task": authority["source_task"],
        "commitment_path": commitment_path,
        "commitment_file": asdict(commitment_identity),
        "commitment_ancestors": [asdict(ancestor) for ancestor in commitment_ancestors],
        "commitment_id": commitment_id,
        "transfer_id": transfer_id,
    }


def validate_acceptance(data: bytes, args: PrepareArgs, receipt_reports: tuple[dict[str, object], ...]) -> None:
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("destination acceptance is not JSON") from exc
    expected = {
        "schema": ACCEPTANCE_SCHEMA,
        "accepted": True,
        "source_owner": args.source_owner,
        "destination_owner": args.destination_owner,
        "source_task": str(args.source_task.resolve(strict=True)),
        "destination_task": str(args.destination_task.resolve(strict=True)),
        "source_target": args.source_target,
        "destination_target": args.destination_target,
        "repository": str(args.repository.resolve(strict=True)),
        "todo": {
            "path": str(args.todo_file.resolve(strict=True)),
            "sha256": args.todo_sha256,
            "source_row": f"{args.source_task.resolve(strict=True).relative_to(args.todo_file.parent.resolve(strict=True)).as_posix()} {args.source_target}",
            "destination_row": f"{args.destination_task.resolve(strict=True).relative_to(args.todo_file.parent.resolve(strict=True)).as_posix()} {args.destination_target}",
        },
        "files": [
            {"path": path, "sha256": expected}
            for path, expected in zip(args.paths, args.expected_path_sha256, strict=True)
        ],
        "source_receipts": [
            {
                "path": str(path.resolve(strict=True)),
                "sha256": expected,
                "producer_target": report["producer_target"],
                "source_task": report["source_task"],
            }
            for path, expected, report in zip(
                args.source_receipts,
                args.source_receipt_sha256s,
                receipt_reports,
                strict=True,
            )
        ],
    }
    if value != expected or data != canonical_json(value):
        raise CustodyError("destination acceptance does not exactly accept this transfer")


def receipt_binds_path(receipt: bytes, repository: Path, file_identity: FileIdentity) -> bool:
    absolute = str(repository / file_identity.path)
    relative = file_identity.path
    expected = {
        f"{path}: SHA-256 {file_identity.sha256}".encode()
        for path in (absolute, relative, f"`{absolute}`", f"`{relative}`")
    }
    for line in receipt.splitlines():
        normalized = line.strip()
        if normalized.startswith(b"- "):
            normalized = normalized[2:].strip()
        if any(normalized == marker or normalized.startswith(marker + b",") for marker in expected):
            return True
    return False


# 🧑 "Also close all cedit agents and move out their tasks"
def build_binding(args: PrepareArgs) -> dict[str, object]:
    if (
        args.source_owner == args.destination_owner
        or args.source_task.resolve() == args.destination_task.resolve()
        or args.source_target == args.destination_target
    ):
        raise CustodyError("source and destination custody must differ")
    if len(args.paths) != len(set(args.paths)) or not args.paths:
        raise CustodyError("source paths must be one nonempty unique ordered set")
    if len(args.expected_path_sha256) != len(args.paths):
        raise CustodyError("every source path requires one expected digest")
    for expected in args.expected_path_sha256:
        require_sha256(expected, "source path digest")
    authority, authority_identity, authority_ancestors = authenticated_file(
        args.authority_file,
        args.authority_sha256,
        "Human authority",
    )
    if not args.source_receipts or len(args.source_receipts) != len(args.source_receipt_sha256s):
        raise CustodyError("source custody receipts require matching nonempty paths and digests")
    receipt_records = tuple(
        authenticated_file(path, expected, f"source custody receipt {index}")
        for index, (path, expected) in enumerate(
            zip(args.source_receipts, args.source_receipt_sha256s, strict=True),
            start=1,
        )
    )
    receipts = tuple(record[0] for record in receipt_records)
    receipt_identities = tuple(record[1] for record in receipt_records)
    receipt_ancestors = tuple(record[2] for record in receipt_records)
    receipt_reports = tuple(
        authenticated_report(receipt, f"source custody receipt {index}")
        for index, receipt in enumerate(receipts, start=1)
    )
    expected_source_task = str(args.source_task.resolve(strict=True))
    expected_source_target = canonical_target(args.source_target)
    if any(
        report["source_task"] != expected_source_task
        or canonical_target(str(report["producer_target"])) != expected_source_target
        for report in receipt_reports
    ):
        raise CustodyError("source custody receipt is not from the bound source owner")
    acceptance, acceptance_identity, acceptance_ancestors = authenticated_file(
        args.acceptance_file,
        args.acceptance_sha256,
        "destination acceptance",
    )
    validate_acceptance(acceptance, args, receipt_reports)
    repository, index_ancestors = repository_identity(args.repository)
    porcelain = git(
        args.repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if len(porcelain) != repository.porcelain_size_bytes or digest(porcelain) != repository.porcelain_sha256:
        raise CustodyError("repository porcelain drifted before path classification")
    status_records = set(porcelain.rstrip(b"\0").split(b"\0")) if porcelain else set()
    for path in args.paths:
        if f"?? {path}".encode() not in status_records:
            raise CustodyError(f"source path is not exactly untracked: {path}")
    source_bindings = tuple(relative_regular_identity(args.repository, path) for path in args.paths)
    files = tuple(binding[0] for binding in source_bindings)
    source_ancestors = tuple(binding[1] for binding in source_bindings)
    for file_identity, expected in zip(files, args.expected_path_sha256, strict=True):
        if file_identity.sha256 != expected:
            raise CustodyError(f"source path digest changed: {file_identity.path}")
        if not any(receipt_binds_path(receipt, args.repository, file_identity) for receipt in receipts):
            raise CustodyError(f"source custody receipt does not bind path and digest: {file_identity.path}")
    source_task, source_task_ancestors = task_identity(
        args.source_task,
        args.source_task_sha256,
        args.source_target,
        "source",
    )
    destination_task, destination_task_ancestors = task_identity(
        args.destination_task,
        args.destination_task_sha256,
        args.destination_target,
        "destination",
    )
    todo, todo_ancestors = todo_identity(
        args.todo_file,
        args.todo_sha256,
        args.source_task,
        args.source_target,
        args.destination_task,
        args.destination_target,
    )
    source_target = target_identity(args.source_target)
    destination_target = target_identity(args.destination_target)
    if (
        source_target.pane_id == destination_target.pane_id
        or (source_target.pane_pid, source_target.pane_start_ticks)
        == (destination_target.pane_pid, destination_target.pane_start_ticks)
    ):
        raise CustodyError("source and destination resolve to the same live owner")
    if not executor_belongs_to_target(destination_target.pane_pid):
        raise CustodyError("destination acceptance was not invoked by the destination target")
    index_identity = FileIdentity(
        repository.index_path,
        repository.index_mode,
        repository.index_device,
        repository.index_inode,
        repository.index_uid,
        repository.index_gid,
        repository.index_size_bytes,
        repository.index_sha256,
    )
    commitment_inputs: list[tuple[FileIdentity, tuple[DirectoryIdentity, ...]]] = []
    for report in receipt_reports:
        raw_commitment_ancestors = report.get("commitment_ancestors")
        if not isinstance(raw_commitment_ancestors, list):
            raise CustodyError("source commitment lacks its ancestor binding")
        commitment_inputs.append(
            (
                file_identity_from(report.get("commitment_file"), "source commitment binding"),
                tuple(
                    directory_identity_from(item, "source commitment ancestor binding")
                    for item in raw_commitment_ancestors
                ),
            )
        )
    absolute_inputs = (
        (authority_identity, authority_ancestors),
        (acceptance_identity, acceptance_ancestors),
        (file_identity_from(asdict(source_task), "source task binding"), source_task_ancestors),
        (
            file_identity_from(asdict(destination_task), "destination task binding"),
            destination_task_ancestors,
        ),
        (file_identity_from(asdict(todo), "TODO binding"), todo_ancestors),
        *zip(receipt_identities, receipt_ancestors, strict=True),
        *commitment_inputs,
        (index_identity, index_ancestors),
    )
    return {
        "schema": SCHEMA,
        "authority": asdict(authority_identity),
        "source_receipts": [
            {"file": asdict(identity), "report": report}
            for identity, report in zip(receipt_identities, receipt_reports, strict=True)
        ],
        "destination_acceptance": asdict(acceptance_identity),
        "source_owner": args.source_owner,
        "destination_owner": args.destination_owner,
        "source_task": asdict(source_task),
        "destination_task": asdict(destination_task),
        "todo": asdict(todo),
        "source_target": asdict(source_target),
        "destination_target": asdict(destination_target),
        "repository": asdict(repository),
        "files": [asdict(file_identity) for file_identity in files],
        "source_ancestors": [
            [asdict(ancestor) for ancestor in ancestors]
            for ancestors in source_ancestors
        ],
        "input_ancestors": [
            {
                "path": identity.path,
                "ancestors": [asdict(ancestor) for ancestor in ancestors],
            }
            for identity, ancestors in absolute_inputs
        ],
    }


def prepare(args: PrepareArgs) -> str:
    binding = build_binding(args)
    data = canonical_json(binding)
    write_new_private(args.output, data)
    return digest(data)


def read_canonical_private_json(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    data, _ = read_regular_nofollow(path, label, private=True)
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(f"{label} is not JSON") from exc
    parsed = object_map(value, label)
    if canonical_json(parsed) != data:
        raise CustodyError(f"{label} is not canonical JSON")
    return parsed, data


def args_from_binding(binding: dict[str, object], output: Path) -> PrepareArgs:
    try:
        files_raw = binding["files"]
        receipts_raw = binding["source_receipts"]
        authority = object_map(binding["authority"], "authority binding")
        acceptance = object_map(binding["destination_acceptance"], "acceptance binding")
        source_task = object_map(binding["source_task"], "source task binding")
        destination_task = object_map(binding["destination_task"], "destination task binding")
        todo = object_map(binding["todo"], "TODO binding")
        repository = object_map(binding["repository"], "repository binding")
        if (
            not isinstance(files_raw, list)
            or not isinstance(receipts_raw, list)
            or not receipts_raw
        ):
            raise TypeError
        files = tuple(object_map(item, "file binding") for item in files_raw)
        receipt_entries = tuple(object_map(item, "source receipt binding") for item in receipts_raw)
        receipts = tuple(
            object_map(item.get("file"), "source receipt file binding") for item in receipt_entries
        )
        return PrepareArgs(
            Path(string_field(repository, "path", "repository binding")),
            tuple(string_field(item, "path", "file binding") for item in files),
            tuple(string_field(item, "sha256", "file binding") for item in files),
            string_field(binding, "source_owner", "binding"),
            string_field(binding, "destination_owner", "binding"),
            Path(string_field(source_task, "path", "source task binding")),
            string_field(source_task, "sha256", "source task binding"),
            string_field(source_task, "runat", "source task binding"),
            Path(string_field(destination_task, "path", "destination task binding")),
            string_field(destination_task, "sha256", "destination task binding"),
            string_field(destination_task, "runat", "destination task binding"),
            Path(string_field(todo, "path", "TODO binding")),
            string_field(todo, "sha256", "TODO binding"),
            Path(string_field(authority, "path", "authority binding")),
            string_field(authority, "sha256", "authority binding"),
            tuple(Path(string_field(item, "path", "source receipt binding")) for item in receipts),
            tuple(string_field(item, "sha256", "source receipt binding") for item in receipts),
            Path(string_field(acceptance, "path", "acceptance binding")),
            string_field(acceptance, "sha256", "acceptance binding"),
            output,
        )
    except (KeyError, TypeError) as exc:
        raise CustodyError("binding lacks required custody identity") from exc


def validate_review(report_data: bytes, binding: dict[str, object], binding_sha256: str) -> dict[str, object]:
    report = authenticated_report(report_data, "custody review")
    try:
        body_value: object = json.loads(report_data.split(b"message:\n", 1)[1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("custody review body is not JSON") from exc
    body = object_map(body_value, "custody review body")
    if (
        set(body) != {"schema", "verdict", "binding_sha256", "notes"}
        or body.get("schema") != REVIEW_SCHEMA
        or body.get("verdict") != "PASS"
        or body.get("binding_sha256") != binding_sha256
        or not isinstance(body.get("notes"), str)
        or not body["notes"]
    ):
        raise CustodyError("review does not independently pass this exact binding")
    source_task = object_map(binding.get("source_task"), "source task binding")
    destination_task = object_map(binding.get("destination_task"), "destination task binding")
    source_target = object_map(binding.get("source_target"), "source target binding")
    destination_target = object_map(binding.get("destination_target"), "destination target binding")
    if report["source_task"] in {source_task.get("path"), destination_task.get("path")} or canonical_target(
        str(report["producer_target"])
    ) in {source_target.get("target"), destination_target.get("target")}:
        raise CustodyError("custody review is not independent of source and destination")
    return report


def existing_exact(path: Path, expected: bytes, label: str) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    data, _ = read_regular_nofollow(path, label, private=True)
    if data != expected:
        raise CustodyError(f"indeterminate custody transaction: {label} conflicts")
    return True


def publish_or_validate(path: Path, data: bytes, label: str) -> None:
    try:
        write_new_private(path, data)
    except CustodyError as exc:
        if "reused output" not in str(exc):
            raise
        for attempt in range(100):
            try:
                if existing_exact(path, data, label):
                    return
            except CustodyError as retry:
                if "unlinked-elsewhere" not in str(retry) or attempt == 99:
                    raise
                time.sleep(0.001)
        raise CustodyError(f"indeterminate custody transaction: {label} did not stabilize")


def default_state_root() -> Path:
    return Path.home() / ".local" / "state" / "omo-manager" / "repository-custody"


def transaction_key(binding: dict[str, object]) -> str:
    repository = object_map(binding.get("repository"), "repository binding")
    source_task = object_map(binding.get("source_task"), "source task binding")
    raw_files = binding.get("files")
    if not isinstance(raw_files, list):
        raise CustodyError("binding lacks its source file set")
    files = tuple(object_map(item, "file binding") for item in raw_files)
    source_identity = {
        "repository": {
            key: repository.get(key)
            for key in ("path", "device", "inode")
        },
        "source_task": {
            key: source_task.get(key)
            for key in ("path", "sha256", "runat")
        },
        "files": [
            {
                key: file_identity.get(key)
                for key in ("path", "device", "inode", "sha256")
            }
            for file_identity in files
        ],
    }
    return digest(canonical_json(source_identity))


def file_identity_from(value: object, label: str) -> FileIdentity:
    record = object_map(value, label)
    return FileIdentity(
        string_field(record, "path", label),
        integer_field(record, "mode", label),
        integer_field(record, "device", label),
        integer_field(record, "inode", label),
        integer_field(record, "uid", label),
        integer_field(record, "gid", label),
        integer_field(record, "size_bytes", label),
        string_field(record, "sha256", label),
    )


def directory_identity_from(value: object, label: str) -> DirectoryIdentity:
    record = object_map(value, label)
    return DirectoryIdentity(
        string_field(record, "path", label),
        integer_field(record, "mode", label),
        integer_field(record, "device", label),
        integer_field(record, "inode", label),
        integer_field(record, "uid", label),
        integer_field(record, "gid", label),
    )


def hold_absolute(
    identity: FileIdentity,
    ancestors: tuple[DirectoryIdentity, ...] | None,
) -> HeldAbsolute:
    path = Path(identity.path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CustodyError(f"bound input is not an absolute file path: {identity.path}")
    components = path.parts[1:]
    descriptors: list[int] = []
    observed: list[DirectoryIdentity] = []
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        descriptors.append(parent)
        observed.append(directory_identity("/", os.fstat(parent)))
        current = Path("/")
        for component in components[:-1]:
            parent = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            descriptors.append(parent)
            current /= component
            observed.append(directory_identity(str(current), os.fstat(parent)))
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        expected = tuple(observed) if ancestors is None else ancestors
        if tuple(observed) != expected:
            os.close(descriptor)
            for opened in reversed(descriptors):
                os.close(opened)
            raise CustodyError(f"bound input ancestor identity drifted: {identity.path}")
        return HeldAbsolute(tuple(descriptors), expected, components[-1], descriptor, identity)
    except OSError as exc:
        for opened in reversed(descriptors):
            os.close(opened)
        raise CustodyError(f"cannot retain bound input path chain for {identity.path}: {exc}") from exc


def hold_source(
    repository: Path,
    identity: FileIdentity,
    ancestors: tuple[DirectoryIdentity, ...],
) -> HeldSource:
    candidate = Path(identity.path)
    if not ancestors or ancestors[0].path != "." or len(ancestors) != len(candidate.parts):
        raise CustodyError(f"source ancestor binding is incomplete: {identity.path}")
    descriptors: list[int] = []
    try:
        parent = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        descriptors.append(parent)
        for component in candidate.parts[:-1]:
            parent = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            descriptors.append(parent)
        descriptor = os.open(
            candidate.parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        return HeldSource(tuple(descriptors), ancestors, candidate.parts[-1], descriptor, identity)
    except OSError as exc:
        for opened in reversed(descriptors):
            os.close(opened)
        raise CustodyError(f"cannot retain source path chain for {identity.path}: {exc}") from exc


def directory_values(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino, details.st_uid, details.st_gid


def validate_held_source(source: HeldSource, repository: Path) -> None:
    for index, (descriptor, expected) in enumerate(
        zip(source.directories, source.directory_identities, strict=True)
    ):
        linked = os.stat(
            repository if index == 0 else Path(expected.path).name,
            dir_fd=None if index == 0 else source.directories[index - 1],
            follow_symlinks=False,
        )
        expected_values = expected.mode, expected.device, expected.inode, expected.uid, expected.gid
        if (
            directory_values(os.fstat(descriptor)) != expected_values
            or directory_values(linked) != expected_values
            or not stat.S_ISDIR(linked.st_mode)
        ):
            raise CustodyError(f"source ancestor identity drifted: {expected.path}")
    details = os.fstat(source.descriptor)
    linked = os.stat(source.leaf_name, dir_fd=source.directories[-1], follow_symlinks=False)
    expected = source.identity
    expected_values = expected.mode, expected.device, expected.inode, expected.uid, expected.gid, expected.size_bytes
    if (
        (
            stat.S_IMODE(details.st_mode),
            details.st_dev,
            details.st_ino,
            details.st_uid,
            details.st_gid,
            details.st_size,
        )
        != expected_values
        or (
            stat.S_IMODE(linked.st_mode),
            linked.st_dev,
            linked.st_ino,
            linked.st_uid,
            linked.st_gid,
            linked.st_size,
        )
        != expected_values
        or details.st_nlink != 1
        or linked.st_nlink != 1
        or not stat.S_ISREG(details.st_mode)
        or not stat.S_ISREG(linked.st_mode)
    ):
        raise CustodyError(f"held source identity drifted: {expected.path}")
    offset = 0
    chunks: list[bytes] = []
    while chunk := os.pread(source.descriptor, 1024 * 1024, offset):
        chunks.append(chunk)
        offset += len(chunk)
    if digest(b"".join(chunks)) != expected.sha256:
        raise CustodyError(f"held source bytes drifted: {expected.path}")


def validate_held_absolute(held: HeldAbsolute) -> None:
    for index, (descriptor, expected_directory) in enumerate(
        zip(held.directories, held.directory_identities, strict=True)
    ):
        linked = os.stat(
            "/" if index == 0 else Path(expected_directory.path).name,
            dir_fd=None if index == 0 else held.directories[index - 1],
            follow_symlinks=False,
        )
        expected_directory_values = (
            expected_directory.mode,
            expected_directory.device,
            expected_directory.inode,
            expected_directory.uid,
            expected_directory.gid,
        )
        if (
            directory_values(os.fstat(descriptor)) != expected_directory_values
            or directory_values(linked) != expected_directory_values
            or not stat.S_ISDIR(linked.st_mode)
        ):
            raise CustodyError(f"bound input ancestor identity drifted: {expected_directory.path}")
    details = os.fstat(held.descriptor)
    linked = os.stat(held.leaf_name, dir_fd=held.directories[-1], follow_symlinks=False)
    expected = held.identity
    values = (
        stat.S_IMODE(details.st_mode),
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_gid,
        details.st_size,
    )
    linked_values = (
        stat.S_IMODE(linked.st_mode),
        linked.st_dev,
        linked.st_ino,
        linked.st_uid,
        linked.st_gid,
        linked.st_size,
    )
    expected_values = (expected.mode, expected.device, expected.inode, expected.uid, expected.gid, expected.size_bytes)
    if (
        values != expected_values
        or linked_values != expected_values
        or details.st_nlink != 1
        or linked.st_nlink != 1
        or not stat.S_ISREG(details.st_mode)
        or not stat.S_ISREG(linked.st_mode)
    ):
        raise CustodyError(f"held input identity drifted: {expected.path}")
    offset = 0
    chunks: list[bytes] = []
    while chunk := os.pread(held.descriptor, 1024 * 1024, offset):
        chunks.append(chunk)
        offset += len(chunk)
    if digest(b"".join(chunks)) != expected.sha256:
        raise CustodyError(f"held input bytes drifted: {expected.path}")


@contextmanager
def held_binding_files(
    binding: dict[str, object],
    extra_bindings: tuple[tuple[FileIdentity, tuple[DirectoryIdentity, ...]], ...] = (),
) -> Iterator[Callable[[], None]]:
    repository_record = object_map(binding.get("repository"), "repository binding")
    repository = Path(string_field(repository_record, "path", "repository binding"))
    identities = [
        file_identity_from(binding.get("authority"), "authority binding"),
        file_identity_from(binding.get("destination_acceptance"), "acceptance binding"),
        file_identity_from(binding.get("source_task"), "source task binding"),
        file_identity_from(binding.get("destination_task"), "destination task binding"),
        file_identity_from(binding.get("todo"), "TODO binding"),
    ]
    raw_receipts = binding.get("source_receipts")
    raw_files = binding.get("files")
    raw_ancestors = binding.get("source_ancestors")
    raw_input_ancestors = binding.get("input_ancestors")
    if (
        not isinstance(raw_receipts, list)
        or not isinstance(raw_files, list)
        or not isinstance(raw_ancestors, list)
        or not isinstance(raw_input_ancestors, list)
        or len(raw_files) != len(raw_ancestors)
    ):
        raise CustodyError("binding lacks held receipt or source identities")
    source_holds: list[HeldSource] = []
    absolute_holds: list[HeldAbsolute] = []
    try:
        identities.extend(
            file_identity_from(object_map(item, "receipt binding").get("file"), "receipt file binding")
            for item in raw_receipts
        )
        identities.extend(
            file_identity_from(
                object_map(object_map(item, "receipt binding").get("report"), "receipt report binding").get(
                    "commitment_file"
                ),
                "receipt commitment binding",
            )
            for item in raw_receipts
        )
        for raw_file, raw_chain in zip(raw_files, raw_ancestors, strict=True):
            if not isinstance(raw_chain, list):
                raise CustodyError("source ancestor binding is not a list")
            source_holds.append(
                hold_source(
                    repository,
                    file_identity_from(raw_file, "source file binding"),
                    tuple(directory_identity_from(item, "source ancestor binding") for item in raw_chain),
                )
            )
        index_identity = FileIdentity(
            string_field(repository_record, "index_path", "repository binding"),
            integer_field(repository_record, "index_mode", "repository binding"),
            integer_field(repository_record, "index_device", "repository binding"),
            integer_field(repository_record, "index_inode", "repository binding"),
            integer_field(repository_record, "index_uid", "repository binding"),
            integer_field(repository_record, "index_gid", "repository binding"),
            integer_field(repository_record, "index_size_bytes", "repository binding"),
            string_field(repository_record, "index_sha256", "repository binding"),
        )
        identities.append(index_identity)
        ancestor_map: dict[str, tuple[DirectoryIdentity, ...]] = {}
        for item in raw_input_ancestors:
            record = object_map(item, "input ancestor binding")
            path = string_field(record, "path", "input ancestor binding")
            raw_chain = record.get("ancestors")
            if path in ancestor_map or not isinstance(raw_chain, list):
                raise CustodyError("input ancestor binding is duplicated or invalid")
            ancestor_map[path] = tuple(
                directory_identity_from(ancestor, "input ancestor binding") for ancestor in raw_chain
            )
        if set(ancestor_map) != {identity.path for identity in identities}:
            raise CustodyError("input ancestor binding does not exactly cover bound inputs")
        absolute_holds.extend(hold_absolute(identity, ancestor_map[identity.path]) for identity in identities)
        absolute_holds.extend(hold_absolute(identity, ancestors) for identity, ancestors in extra_bindings)

        def validate() -> None:
            for held in absolute_holds:
                validate_held_absolute(held)
            for source in source_holds:
                validate_held_source(source, repository)

        validate()
        yield validate
    except (KeyError, TypeError, ValueError) as exc:
        raise CustodyError("binding has an invalid held-file generation") from exc
    finally:
        for source in reversed(source_holds):
            os.close(source.descriptor)
            for descriptor in reversed(source.directories):
                os.close(descriptor)
        for held in reversed(absolute_holds):
            os.close(held.descriptor)
            for descriptor in reversed(held.directories):
                os.close(descriptor)


def execute(
    binding_path: Path,
    review_path: Path,
    *,
    state_root: Path | None = None,
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
    crash_after: str = "",
) -> str:
    binding, binding_data = read_canonical_private_json(binding_path, "custody binding")
    if binding.get("schema") != SCHEMA:
        raise CustodyError("binding has the wrong schema")
    review_data, review_identity, review_ancestors = absolute_file_binding(
        review_path,
        "custody review",
        private=True,
    )
    binding_sha256 = digest(binding_data)
    review_report = validate_review(review_data, binding, binding_sha256)
    review_commitment = file_identity_from(review_report.get("commitment_file"), "review commitment binding")
    raw_review_commitment_ancestors = review_report.get("commitment_ancestors")
    if not isinstance(raw_review_commitment_ancestors, list):
        raise CustodyError("review commitment lacks its ancestor binding")
    review_commitment_ancestors = tuple(
        directory_identity_from(item, "review commitment ancestor binding")
        for item in raw_review_commitment_ancestors
    )
    repository_binding = object_map(binding["repository"], "repository binding")
    repository = Path(string_field(repository_binding, "path", "repository binding"))
    registry = state_root or default_state_root()
    if not registry.exists():
        parent = registry.parent.resolve(strict=True)
        details = parent.stat()
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise CustodyError("custody state parent must be owner-private")
        try:
            registry.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    private_output_parent(registry / "probe")
    key = transaction_key(binding)
    foreign_directory = registry / key
    if foreign_directory.exists() or foreign_directory.is_symlink():
        raise CustodyError("indeterminate custody transaction: foreign source-key namespace exists")
    journal_path = registry / f"{key}.prepared.json"
    ledger_path = registry / f"{key}.ledger.json"
    committed_path = registry / f"{key}.committed.json"
    for output in (ledger_path, journal_path, committed_path):
        if repository == output.resolve(strict=False) or repository in output.resolve(strict=False).parents:
            raise CustodyError("transaction outputs must be outside the source repository")
    ledger = canonical_json(
        {
            "schema": LEDGER_SCHEMA,
            "binding_sha256": binding_sha256,
            "review_sha256": digest(review_data),
            "review_report": review_report,
            "source_owner": binding["source_owner"],
            "destination_owner": binding["destination_owner"],
            "files": binding["files"],
        }
    )
    journal = canonical_json(
        {
            "schema": JOURNAL_SCHEMA,
            "binding_sha256": binding_sha256,
            "review_sha256": digest(review_data),
            "ledger_sha256": digest(ledger),
            "ledger_path": str(ledger_path),
        }
    )
    committed = canonical_json(
        {
            "schema": COMMIT_SCHEMA,
            "binding_sha256": binding_sha256,
            "journal_sha256": digest(journal),
            "ledger_sha256": digest(ledger),
        }
    )
    with held_binding_files(
        binding,
        (
            (review_identity, review_ancestors),
            (review_commitment, review_commitment_ancestors),
        ),
    ) as validate_held:
        rebuilt = canonical_json(build_binding(args_from_binding(binding, binding_path)))
        if rebuilt != binding_data:
            raise CustodyError("custody binding drifted before transaction")
        validate_held()
        publish_or_validate(journal_path, journal, "prepared journal")
        journal_exists = existing_exact(journal_path, journal, "prepared journal")
        ledger_exists = existing_exact(ledger_path, ledger, "custody ledger")
        committed_exists = existing_exact(committed_path, committed, "commit receipt")
        if (ledger_exists or committed_exists) and not journal_exists:
            raise CustodyError("indeterminate custody transaction: output exists without prepared journal")
        if committed_exists and not ledger_exists:
            raise CustodyError("indeterminate custody transaction: commit exists without ledger")
        if committed_exists:
            validate_held()
            return digest(ledger)
        if crash_after == "prepared":
            raise CustodyError("injected crash after prepared journal")
        if before_publish is not None:
            before_publish()
        validate_held()
        rebuilt = canonical_json(build_binding(args_from_binding(binding, binding_path)))
        if rebuilt != binding_data:
            raise CustodyError("custody binding drifted at final publication boundary")
        if not ledger_exists:
            publish_or_validate(ledger_path, ledger, "custody ledger")
        if after_publish is not None:
            after_publish()
        try:
            validate_held()
            rebuilt = canonical_json(build_binding(args_from_binding(binding, binding_path)))
        except CustodyError as exc:
            raise CustodyError("indeterminate custody transaction: source generation changed after ledger publication") from exc
        if rebuilt != binding_data:
            raise CustodyError("indeterminate custody transaction: binding drifted after ledger publication")
        if crash_after == "ledger":
            raise CustodyError("injected crash after ledger publication")
        publish_or_validate(committed_path, committed, "commit receipt")
        return digest(ledger)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    _ = prepare_parser.add_argument("--repository", type=Path, required=True)
    _ = prepare_parser.add_argument("--path", action="append", required=True)
    _ = prepare_parser.add_argument("--path-sha256", action="append", required=True)
    _ = prepare_parser.add_argument("--source-owner", required=True)
    _ = prepare_parser.add_argument("--destination-owner", required=True)
    for owner in ("source", "destination"):
        _ = prepare_parser.add_argument(f"--{owner}-task", type=Path, required=True)
        _ = prepare_parser.add_argument(f"--{owner}-task-sha256", required=True)
        _ = prepare_parser.add_argument(f"--{owner}-target", required=True)
    _ = prepare_parser.add_argument("--todo", type=Path, required=True)
    _ = prepare_parser.add_argument("--todo-sha256", required=True)
    for evidence in ("authority", "acceptance"):
        _ = prepare_parser.add_argument(f"--{evidence}", type=Path, required=True)
        _ = prepare_parser.add_argument(f"--{evidence}-sha256", required=True)
    _ = prepare_parser.add_argument("--source-receipt", type=Path, action="append", required=True)
    _ = prepare_parser.add_argument("--source-receipt-sha256", action="append", required=True)
    _ = prepare_parser.add_argument("--output", type=Path, required=True)
    execute_parser = commands.add_parser("execute")
    _ = execute_parser.add_argument("--binding", type=Path, required=True)
    _ = execute_parser.add_argument("--review", type=Path, required=True)
    return result


def parse_prepare(namespace: argparse.Namespace) -> PrepareArgs:
    return PrepareArgs(
        namespace.repository,
        tuple(namespace.path),
        tuple(namespace.path_sha256),
        namespace.source_owner,
        namespace.destination_owner,
        namespace.source_task,
        namespace.source_task_sha256,
        namespace.source_target,
        namespace.destination_task,
        namespace.destination_task_sha256,
        namespace.destination_target,
        namespace.todo,
        namespace.todo_sha256,
        namespace.authority,
        namespace.authority_sha256,
        tuple(namespace.source_receipt),
        tuple(namespace.source_receipt_sha256),
        namespace.acceptance,
        namespace.acceptance_sha256,
        namespace.output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    namespace = parser().parse_args(argv)
    try:
        if namespace.command == "prepare":
            print(prepare(parse_prepare(namespace)))
        else:
            print(execute(namespace.binding, namespace.review))
    except CustodyError as exc:
        print(f"omo_repository_custody.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
