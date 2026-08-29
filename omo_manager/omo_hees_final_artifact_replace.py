#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Atomically replace the one stale hees artifact owner without launching it."""
from __future__ import annotations

import argparse
import base64
import ctypes
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_agent_status import TASK_RE
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_V1, TaskFrontmatterError, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import TODO_ROW_RE, has_pending_marker, root_membership_lock

STALE_TASK = "hees_1170_policy.md"
SUCCESSOR_TASK = "hees_final_artifact.md"
STALE_TARGET = "guest_hees:5"
MANAGER_TARGET = "guest_hees:0"
SUCCESSOR_BLOCKER = "awaiting separate supported launch and delivery by guest_hees:0"
JOURNAL_NAME = ".omo-hees-final-artifact-replace.transaction"
JOURNAL_VERSION = "v1.0.0"
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_DISPLACED_RECEIPTS = 8
MAX_TRANSACTION_EXCHANGES = 4
TRUSTED_WRITER_GROUP = "sichanghe"
TRUSTED_HUMAN_USER = "sichanghe"
POSIX_ACL_XATTRS = {"system.posix_acl_access", "system.posix_acl_default"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGE_RE = re.compile(r"^\.(hees_1170_policy\.md|TODO\.md)\.omo-stage-[0-9a-f]{32}$")
TMUX_TARGET_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?$")
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400
RENAME_EXCHANGE = 2
LIBC = ctypes.CDLL(None, use_errno=True)


class ReplaceError(RuntimeError):
    """The pinned replacement failed closed."""


class RollbackError(ReplaceError):
    """A failed write could not be completely removed."""


class CommittedWriteError(ReplaceError):
    """A namespace replacement committed but its directory sync failed."""

    def __init__(self, snapshot: Snapshot, error: OSError) -> None:
        super().__init__(f"{snapshot.path.name} committed but directory sync failed: {error}")
        self.snapshot = snapshot


class NamespaceMutationError(ReplaceError):
    """An exchanged namespace could not be restored with proven identities."""


@dataclass(frozen=True)
class Args:
    root: Path
    stale_task: str
    successor_task: str
    stale_target: str
    manager_target: str
    stale_sha256: str
    todo_sha256: str
    pending_item_sha256: str


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    state: os.stat_result


@dataclass(frozen=True)
class Plan:
    stale: Snapshot
    todo: Snapshot
    successor_path: Path
    stale_after: bytes
    successor_data: bytes
    todo_after: bytes


@dataclass(frozen=True)
class Journal:
    snapshot: Snapshot
    stale_before: bytes
    stale_after: bytes
    successor_data: bytes
    todo_before: bytes
    todo_after: bytes
    stale_mode: int
    todo_mode: int


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    stale_task: str = ""
    successor_task: str = ""
    stale_target: str = ""
    manager_target: str = ""
    stale_sha256: str = ""
    todo_sha256: str = ""
    pending_item_sha256: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--stale-task", required=True)
    _ = parser.add_argument("--successor-task", required=True)
    _ = parser.add_argument("--stale-target", required=True)
    _ = parser.add_argument("--manager-target", required=True)
    _ = parser.add_argument("--stale-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--pending-item-sha256", required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    pinned = (
        (parsed.stale_task, STALE_TASK, "stale task"),
        (parsed.successor_task, SUCCESSOR_TASK, "successor task"),
        (parsed.stale_target, STALE_TARGET, "stale target"),
        (parsed.manager_target, MANAGER_TARGET, "manager target"),
    )
    for value, expected, label in pinned:
        if value != expected:
            parser.error(f"{label} must be exactly {expected}")
    for value, label in (
        (parsed.stale_sha256, "stale task"),
        (parsed.todo_sha256, "TODO"),
        (parsed.pending_item_sha256, "pending item"),
    ):
        if SHA256_RE.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    return Args(
        parsed.root.expanduser().resolve(strict=False),
        parsed.stale_task,
        parsed.successor_task,
        parsed.stale_target,
        parsed.manager_target,
        parsed.stale_sha256,
        parsed.todo_sha256,
        parsed.pending_item_sha256,
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_target(target: str) -> str | None:
    match = TMUX_TARGET_RE.fullmatch(target)
    if match is None:
        return None
    session, window, pane = match.groups()
    return f"{session}:{int(window)}.{int(pane or '0')}"


def same_target(left: str, right: str) -> bool:
    return canonical_target(left) is not None and canonical_target(left) == canonical_target(right)


def file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def directory_generation(fd: int) -> tuple[int, int, int, int]:
    state = os.fstat(fd)
    return state.st_dev, state.st_ino, state.st_mtime_ns, state.st_ctime_ns


def require_safe_file(fd: int, state: os.stat_result, label: str, trust: TrustedGroup) -> None:
    if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & stat.S_IWOTH:
        raise ReplaceError(f"{label} must be one owner-owned authenticated regular file")
    if state.st_mode & stat.S_IWGRP and state.st_gid != trust.gid:
        raise ReplaceError(f"{label} must be one owner-owned authenticated regular file")
    try:
        attributes = set(os.listxattr(fd))
    except OSError as error:
        raise ReplaceError(f"{label} ACLs are unreadable: {error}") from error
    if attributes & POSIX_ACL_XATTRS:
        raise ReplaceError(f"{label} has an extended POSIX ACL")


def read_snapshot(path: Path, label: str, trust: TrustedGroup | None = None) -> Snapshot:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        raise ReplaceError(f"{label} is unavailable: {error}") from error
    try:
        before = os.fstat(fd)
        require_safe_file(fd, before, label, trusted_writer_group() if trust is None else trust)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if file_identity(before) != file_identity(after):
        raise ReplaceError(f"{label} changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def read_snapshot_at(parent_fd: int, name: str, path: Path, label: str, trust: TrustedGroup | None = None) -> Snapshot:
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
    except OSError as error:
        raise ReplaceError(f"{label} is unavailable: {error}") from error
    try:
        before = os.fstat(fd)
        require_safe_file(fd, before, label, trusted_writer_group() if trust is None else trust)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if file_identity(before) != file_identity(after):
        raise ReplaceError(f"{label} changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def require_snapshot(expected: Snapshot, label: str) -> None:
    current = read_snapshot(expected.path, label)
    if file_identity(current.state) != file_identity(expected.state) or current.data != expected.data:
        raise ReplaceError(f"{label} changed while replacement was being prepared")


def require_snapshot_at(expected: Snapshot, label: str, parent_fd: int) -> None:
    current = read_snapshot_at(parent_fd, expected.path.name, expected.path, label)
    if file_identity(current.state) != file_identity(expected.state) or current.data != expected.data:
        raise ReplaceError(f"{label} changed while replacement was being prepared")


def task_body(data: bytes) -> tuple[bytes, bytes]:
    lines = data.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise ReplaceError("stale task has no frontmatter opening marker")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip(b"\r\n") != b"---":
            continue
        if not line.endswith((b"\n", b"\r")) and index + 1 < len(lines):
            raise ReplaceError("stale task frontmatter closing marker is not line-delimited")
        newline = b"\r\n" if lines[0].endswith(b"\r\n") else b"\n"
        return newline, b"".join(lines[index + 1 :])
    raise ReplaceError("stale task has no frontmatter closing marker")


def task_text(metadata: TaskMetadata, body: bytes, newline: bytes, *, status: str, blocked_on: str, pending: tuple[str, ...]) -> bytes:
    lines = [
        "---",
        f"version: {metadata.version}",
        f"status: {status}",
        *([f"blocked_on: {blocked_on}"] if blocked_on else []),
        f"runat: {metadata.runat}",
        f"tool: {metadata.tool}",
        f"managerat: {metadata.managerat}",
        f"is_manager: {str(metadata.is_manager).lower()}",
        *(("pending_task_items: []",) if not pending else ("pending_task_items:", *(f"  - {item}" for item in pending))),
        "---",
    ]
    return newline.join(line.encode() for line in lines) + newline + body


def todo_ref(root: Path, value: str) -> Path | None:
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve(strict=False)
    return resolved if resolved == root or root in resolved.parents else None


def todo_plan(root: Path, data: bytes, stale_path: Path, successor_path: Path) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError as error:
        raise ReplaceError(f"TODO is not UTF-8: {error}") from error
    lines = text.splitlines(keepends=True)
    contents = [line.rstrip("\r\n") for line in lines]
    headings = {name: [index for index, line in enumerate(contents) if line == f"{name}:"] for name in ("current", "human pending", "low priority", "previous")}
    if any(len(indexes) != 1 for indexes in headings.values()):
        raise ReplaceError("TODO must contain exactly one canonical lifecycle section of each kind")
    if not headings["current"][0] < headings["human pending"][0] < headings["low priority"][0] < headings["previous"][0]:
        raise ReplaceError("TODO lifecycle sections are out of canonical order")
    referenced: list[tuple[int, re.Match[str], Path]] = []
    for index, content in enumerate(contents):
        task_matches = list(TASK_RE.finditer(content))
        if not task_matches:
            continue
        strict = TODO_ROW_RE.fullmatch(content)
        if strict is None or len(task_matches) != 1:
            if any(todo_ref(root, match.group(1)) in {stale_path, successor_path} for match in task_matches):
                raise ReplaceError("TODO contains a malformed stale or successor task row")
            continue
        resolved = todo_ref(root, strict.group(1))
        token = content.strip().split(maxsplit=1)[0]
        if resolved in {stale_path, successor_path} and (token.startswith("`") != token.endswith("`") or token.count("`") not in {0, 2}):
            raise ReplaceError("TODO contains a malformed stale or successor task row")
        if resolved is not None:
            referenced.append((index, task_matches[0], resolved))
    stale_refs = [(index, match) for index, match, resolved in referenced if resolved == stale_path]
    successor_refs = [(index, match) for index, match, resolved in referenced if resolved == successor_path]
    eligible = tuple((headings[name][0], headings[next_name][0]) for name, next_name in (("current", "human pending"), ("human pending", "low priority")))
    if len(stale_refs) != 1 or not any(start < stale_refs[0][0] < end for start, end in eligible):
        raise ReplaceError("TODO must contain the exact stale row once under current or human pending")
    if successor_refs:
        raise ReplaceError("TODO already contains a successor row")
    stale_index, stale_match = stale_refs[0]
    content = contents[stale_index]
    suffix = content[stale_match.end() :].strip()
    if suffix != STALE_TARGET:
        raise ReplaceError("TODO stale row must name only the exact stale target")
    stale_ref = stale_match.group(1)
    if not stale_ref.endswith(STALE_TASK):
        raise ReplaceError("TODO stale row alias is not replaceable without changing its path form")
    successor_ref = stale_ref.removesuffix(STALE_TASK) + SUCCESSOR_TASK
    ending = lines[stale_index][len(content) :]
    lines[stale_index] = content[: stale_match.start(1)] + successor_ref + content[stale_match.end(1) :] + ending
    previous_index = headings["previous"][0]
    previous_ending = lines[previous_index][len(contents[previous_index]) :] or ("\r\n" if "\r\n" in text else "\n")
    if not lines[previous_index].endswith(("\n", "\r")):
        lines[previous_index] += previous_ending
    lines.insert(previous_index + 1, f"{stale_ref} {STALE_TARGET}{previous_ending}")
    return "".join(lines).encode()


def raw_target_claim(text: str, target: str) -> bool:
    lines = text.splitlines()
    inside = False
    for index, line in enumerate(lines):
        if line.strip() == "---":
            if inside:
                return False
            inside = True
            continue
        if not inside:
            continue
        key, separator, value = line.partition(":")
        if key.strip() != "runat" or not separator:
            continue
        field = [value]
        for continuation in lines[index + 1 :]:
            if continuation.strip() == "---" or (continuation and not continuation[0].isspace()):
                break
            field.append(continuation)
        if any(same_target(match.group(1), target) for match in TARGET_RE.finditer("\n".join(field))):
            return True
    return False


def checked_pane_id(target: str) -> str:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReplaceError(f"cannot prove stale-target absence: {error}") from error
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())
        raise ReplaceError(f"cannot prove stale-target absence because tmux pane inventory failed: {detail or result.returncode}")
    canonical = canonical_target(target)
    if canonical is None:
        raise ReplaceError("stale target is not one canonicalizable tmux target")
    matches: list[str] = []
    for line in result.stdout.splitlines():
        resolved, separator, pane_id = line.partition("\t")
        resolved_canonical = canonical_target(resolved)
        if resolved_canonical is None:
            raise ReplaceError("cannot prove stale-target absence because a tmux inventory target is malformed")
        if resolved_canonical != canonical:
            continue
        if not separator or re.fullmatch(r"%[1-9]\d*", pane_id) is None:
            raise ReplaceError("cannot prove stale-target absence because its tmux inventory row is malformed")
        matches.append(pane_id)
    if len(matches) > 1:
        raise ReplaceError("cannot prove stale-target identity because pane inventory is ambiguous")
    return matches[0] if matches else ""


@dataclass(frozen=True)
class ScannedDirectory:
    fd: int
    path: Path
    state: os.stat_result
    parent_fd: int | None
    name: str


@dataclass(frozen=True)
class ScannedRecord:
    snapshot: Snapshot
    parent_fd: int
    name: str


@dataclass(frozen=True)
class TrustedGroup:
    gid: int
    members: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RootIdentity:
    path: Path
    device: int
    inode: int


def trusted_writer_group() -> TrustedGroup:
    try:
        group = grp.getgrnam(TRUSTED_WRITER_GROUP)
        current = pwd.getpwuid(os.getuid())
        human = pwd.getpwnam(TRUSTED_HUMAN_USER)
        accounts = tuple(pwd.getpwall())
    except KeyError as error:
        raise ReplaceError(f"trusted writer identity is unavailable: {error}") from error
    if group.gr_gid not in os.getgroups() or current.pw_name == human.pw_name or len(group.gr_mem) != len(set(group.gr_mem)):
        raise ReplaceError("trusted writer group identity is invalid")
    members: dict[str, int] = {account.pw_name: account.pw_uid for account in accounts if account.pw_gid == group.gr_gid}
    for name in group.gr_mem:
        try:
            account = pwd.getpwnam(name)
        except KeyError as error:
            raise ReplaceError(f"trusted writer group member {name} is unavailable") from error
        members[name] = account.pw_uid
    expected = {current.pw_name: current.pw_uid, human.pw_name: human.pw_uid}
    if members != expected:
        raise ReplaceError("trusted writer group contains an unauthorized principal")
    return TrustedGroup(group.gr_gid, tuple(sorted(members.items())))


def require_safe_directory(fd: int, state: os.stat_result, path: Path, trust: TrustedGroup) -> None:
    required = stat.S_IRUSR | stat.S_IXUSR
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & required != required or state.st_mode & stat.S_IWOTH:
        raise ReplaceError(f"cannot prove target ownership because directory {path} is unsafe or unreadable")
    if state.st_mode & stat.S_IWGRP and (state.st_gid != trust.gid or not state.st_mode & stat.S_ISGID):
        raise ReplaceError(f"cannot prove target ownership because directory {path} is unsafe or unreadable")
    try:
        attributes = set(os.listxattr(fd))
    except OSError as error:
        raise ReplaceError(f"cannot prove target ownership because directory {path} ACLs are unreadable: {error}") from error
    if attributes & POSIX_ACL_XATTRS:
        raise ReplaceError(f"cannot prove target ownership because directory {path} has an extended POSIX ACL")


def capture_root_identity(root: Path) -> RootIdentity:
    fd = directory_fd(root)
    try:
        state = os.fstat(fd)
        require_safe_directory(fd, state, root, trusted_writer_group())
        return RootIdentity(root, state.st_dev, state.st_ino)
    finally:
        os.close(fd)


def require_root_identity(expected: RootIdentity) -> None:
    current = capture_root_identity(expected.path)
    if (current.device, current.inode) != (expected.device, expected.inode):
        raise ReplaceError("work-log root identity changed during replacement")


def markdown_records(root: Path, existing_root_fd: int | None = None) -> tuple[Snapshot, ...]:
    fds: list[int] = []
    try:
        trust = trusted_writer_group()
        root_fd = directory_fd(root) if existing_root_fd is None else os.dup(existing_root_fd)
        fds.append(root_fd)
        root_state = os.fstat(root_fd)
        require_safe_directory(root_fd, root_state, root, trust)
        directories = [ScannedDirectory(root_fd, root, root_state, None, root.name)]
        records: list[ScannedRecord] = []
        for directory in directories:
            try:
                entries = tuple(sorted(os.scandir(directory.fd), key=lambda entry: entry.name))
            except OSError as error:
                raise ReplaceError(f"cannot prove target ownership because directory {directory.path} is unreadable: {error}") from error
            for entry in entries:
                path = directory.path / entry.name
                try:
                    entry_state = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise ReplaceError(f"cannot prove target ownership because {path} is unreadable: {error}") from error
                if stat.S_ISLNK(entry_state.st_mode):
                    if path.suffix == ".md":
                        raise ReplaceError(f"cannot prove target ownership because {path.name} is a symlink")
                    continue
                if stat.S_ISDIR(entry_state.st_mode):
                    try:
                        child_fd = os.open(entry.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory.fd)
                    except OSError as error:
                        raise ReplaceError(f"cannot prove target ownership because directory {path} changed or became unreadable: {error}") from error
                    fds.append(child_fd)
                    child_state = os.fstat(child_fd)
                    require_safe_directory(child_fd, child_state, path, trust)
                    if file_identity(child_state) != file_identity(entry_state):
                        raise ReplaceError(f"cannot prove target ownership because directory {path} changed during traversal")
                    directories.append(ScannedDirectory(child_fd, path, child_state, directory.fd, entry.name))
                elif path.suffix == ".md":
                    if not stat.S_ISREG(entry_state.st_mode):
                        raise ReplaceError(f"cannot prove target ownership because {path.name} is not a regular file")
                    snapshot = read_snapshot_at(directory.fd, entry.name, path, f"task record {path.name}", trust)
                    if file_identity(snapshot.state) != file_identity(entry_state):
                        raise ReplaceError(f"cannot prove target ownership because {path.name} changed before it was read")
                    records.append(ScannedRecord(snapshot, directory.fd, entry.name))
        root_current = os.stat(root, follow_symlinks=False)
        if file_identity(root_current) != file_identity(root_state):
            raise ReplaceError("cannot prove target ownership because the root directory changed during traversal")
        for directory in directories[1:]:
            if directory.parent_fd is None:
                raise AssertionError("child directory has no parent descriptor")
            current = os.stat(directory.name, dir_fd=directory.parent_fd, follow_symlinks=False)
            if file_identity(current) != file_identity(directory.state):
                raise ReplaceError(f"cannot prove target ownership because directory {directory.path} changed during traversal")
        snapshots: list[Snapshot] = []
        for record in records:
            current = read_snapshot_at(record.parent_fd, record.name, record.snapshot.path, f"task record {record.snapshot.path.name}", trust)
            if file_identity(current.state) != file_identity(record.snapshot.state) or current.data != record.snapshot.data:
                raise ReplaceError(f"cannot prove target ownership because {record.snapshot.path.name} changed during traversal")
            snapshots.append(current)
        if trusted_writer_group() != trust:
            raise ReplaceError("trusted writer group changed during traversal")
        return tuple(snapshots)
    except OSError as error:
        raise ReplaceError(f"cannot prove target ownership because traversal state changed: {error}") from error
    finally:
        for fd in reversed(fds):
            os.close(fd)


def active_owners(root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None) -> tuple[Path, ...]:
    owners: list[Path] = []
    records = {snapshot.path: snapshot.data for snapshot in markdown_records(root, root_fd)}
    records.update(overrides)
    for path, data in sorted(records.items()):
        try:
            text = data.decode()
        except (OSError, UnicodeDecodeError) as error:
            raise ReplaceError(f"cannot prove target ownership because {path.name} is unreadable: {error}") from error
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError as error:
            if raw_target_claim(text, target):
                raise ReplaceError(f"cannot prove target ownership because {path.name} has invalid frontmatter: {error}") from error
            continue
        if metadata is not None and metadata.status != "done" and same_target(metadata.runat, target):
            owners.append(path)
    return tuple(owners)


# 🧑 "Treat tmux sessions whose names begin with `h` as human-owned."
def require_nonhuman_target(target: str) -> None:
    if target.partition(":")[0].startswith("h"):
        raise ReplaceError("refusing a human-owned h* target")


def replacement_bytes(args: Args, stale_data: bytes, todo_data: bytes) -> tuple[bytes, bytes, bytes]:
    root = args.root
    stale_path = root / args.stale_task
    successor_path = root / args.successor_task
    try:
        stale_text = stale_data.decode()
        metadata = parse_task_metadata(stale_text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as error:
        raise ReplaceError(f"stale task is invalid: {error}") from error
    if metadata is None or metadata.version != TASK_FRONTMATTER_V1:
        raise ReplaceError("stale task must have v1 frontmatter")
    if metadata.status != "blocked" or metadata.is_manager or metadata.tool != "codex" or metadata.session_id:
        raise ReplaceError("stale task status, role, or tool does not match the pinned worker")
    if not same_target(metadata.runat, args.stale_target) or not same_target(metadata.managerat, args.manager_target):
        raise ReplaceError("stale task ownership does not match the pinned target and manager")
    if has_pending_marker(stale_text):
        raise ReplaceError("stale task still has a live pending-delivery marker")
    matches = tuple(item for item in metadata.pending_task_items if digest(item.encode()) == args.pending_item_sha256)
    if len(metadata.pending_task_items) != 1 or len(matches) != 1:
        raise ReplaceError("stale task must contain exactly one pending item matching the supplied digest")
    newline, body = task_body(stale_data)
    stale_after = task_text(metadata, body, newline, status="done", blocked_on="", pending=())
    successor_data = task_text(metadata, body, newline, status="blocked", blocked_on=SUCCESSOR_BLOCKER, pending=(matches[0],))
    todo_after = todo_plan(root, todo_data, stale_path.resolve(strict=False), successor_path.resolve(strict=False))
    return stale_after, successor_data, todo_after


def entry_exists(parent_fd: int, name: str) -> bool:
    try:
        _ = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def prepare(args: Args, root_fd: int | None = None) -> Plan:
    require_nonhuman_target(args.stale_target)
    root = args.root.resolve(strict=True)
    if root != args.root or not root.is_dir():
        raise ReplaceError("root must be one existing canonical directory")
    stale_path = root / args.stale_task
    successor_path = root / args.successor_task
    todo_path = root / "TODO.md"
    if entry_exists(root_fd, successor_path.name) if root_fd is not None else successor_path.exists() or successor_path.is_symlink():
        raise ReplaceError("successor task already exists")
    stale = read_snapshot_at(root_fd, stale_path.name, stale_path, "stale task") if root_fd is not None else read_snapshot(stale_path, "stale task")
    todo = read_snapshot_at(root_fd, todo_path.name, todo_path, "TODO") if root_fd is not None else read_snapshot(todo_path, "TODO")
    if digest(stale.data) != args.stale_sha256:
        raise ReplaceError("stale task digest changed")
    if digest(todo.data) != args.todo_sha256:
        raise ReplaceError("TODO digest changed")
    stale_resolved = stale_path.resolve(strict=True)
    successor_resolved = successor_path.resolve(strict=False)
    stale_after, successor_data, todo_after = replacement_bytes(args, stale.data, todo.data)
    current = active_owners(root, args.stale_target, {}, root_fd)
    if current != (stale_resolved,):
        raise ReplaceError(f"stale task is not the sole active owner: {', '.join(path.name for path in current) or 'none'}")
    candidate = active_owners(root, args.stale_target, {stale_resolved: stale_after, successor_resolved: successor_data}, root_fd)
    if candidate != (successor_resolved,):
        raise ReplaceError(f"candidate does not have exactly one active successor owner: {', '.join(path.name for path in candidate) or 'none'}")
    return Plan(stale, todo, successor_resolved, stale_after, successor_data, todo_after)


def directory_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))


def anonymous_file(parent_fd: int, data: bytes, mode: int) -> tuple[int, os.stat_result]:
    flags = os.O_RDWR | getattr(os, "O_TMPFILE", 0) | getattr(os, "O_CLOEXEC", 0)
    if not getattr(os, "O_TMPFILE", 0):
        raise ReplaceError("atomic publication requires Linux O_TMPFILE support")
    fd = os.open(".", flags, mode, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short anonymous-file write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        return fd, os.fstat(fd)
    except Exception:
        os.close(fd)
        raise


def link_fd(fd: int, parent_fd: int, name: str) -> None:
    source = os.fsencode(f"/proc/self/fd/{fd}")
    result = LIBC.linkat(AT_FDCWD, ctypes.c_char_p(source), parent_fd, ctypes.c_char_p(os.fsencode(name)), AT_SYMLINK_FOLLOW)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def rename_exchange(parent_fd: int, left: str, right: str) -> None:
    result = LIBC.renameat2(parent_fd, ctypes.c_char_p(os.fsencode(left)), parent_fd, ctypes.c_char_p(os.fsencode(right)), RENAME_EXCHANGE)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{left}<->{right}")


def replace_existing(expected: Snapshot, data: bytes, bound_parent_fd: int | None = None) -> Snapshot:
    parent_fd = directory_fd(expected.path.parent) if bound_parent_fd is None else os.dup(bound_parent_fd)
    require_snapshot_at(expected, expected.path.name, parent_fd)
    fd = -1
    stage_name = f".{expected.path.name}.omo-stage-{os.urandom(16).hex()}"
    try:
        fd, _prepared = anonymous_file(parent_fd, data, stat.S_IMODE(expected.state.st_mode))
        link_fd(fd, parent_fd, stage_name)
        prepared = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        os.close(fd)
        fd = -1
        require_snapshot_at(expected, expected.path.name, parent_fd)
        rename_exchange(parent_fd, stage_name, expected.path.name)
        try:
            target_state = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
            stage_state = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise NamespaceMutationError(f"{expected.path.name} replacement committed but its exchanged namespace could not be inspected") from error
        if (target_state.st_dev, target_state.st_ino) != (prepared.st_dev, prepared.st_ino) or (stage_state.st_dev, stage_state.st_ino) != (expected.state.st_dev, expected.state.st_ino):
            current_target = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
            current_stage = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
            if (current_target.st_dev, current_target.st_ino) == (target_state.st_dev, target_state.st_ino) and (current_stage.st_dev, current_stage.st_ino) == (stage_state.st_dev, stage_state.st_ino):
                try:
                    rename_exchange(parent_fd, stage_name, expected.path.name)
                    restored_target = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
                    preserved_stage = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
                    if (restored_target.st_dev, restored_target.st_ino) != (expected.state.st_dev, expected.state.st_ino) or (preserved_stage.st_dev, preserved_stage.st_ino) != (target_state.st_dev, target_state.st_ino):
                        raise NamespaceMutationError("transaction-stage restoration identities changed")
                    os.fsync(parent_fd)
                except (OSError, NamespaceMutationError) as error:
                    raise NamespaceMutationError(f"{expected.path.name} transaction stage changed and its original namespace could not be durably restored; foreign state preserved") from error
            raise ReplaceError(f"{expected.path.name} transaction stage changed; original target restored when identity remained provable")
        try:
            displaced = read_snapshot_at(parent_fd, stage_name, expected.path.parent / stage_name, f"{expected.path.name} displaced-inode receipt")
            if file_identity(displaced.state) != file_identity(stage_state) or displaced.data != expected.data:
                raise NamespaceMutationError(f"{expected.path.name} displaced-inode receipt changed")
            snapshot = read_snapshot_at(parent_fd, expected.path.name, expected.path, expected.path.name)
            if file_identity(snapshot.state) != file_identity(target_state) or snapshot.data != data:
                raise NamespaceMutationError(f"{expected.path.name} was rebound after committed replacement")
            try:
                os.fsync(parent_fd)
            except OSError as error:
                raise CommittedWriteError(snapshot, error) from error
            require_snapshot_at(displaced, f"{expected.path.name} displaced-inode receipt", parent_fd)
            return snapshot
        except (CommittedWriteError, NamespaceMutationError):
            raise
        except Exception as error:
            raise NamespaceMutationError(f"{expected.path.name} replacement committed but its namespace verification failed; state preserved for manual recovery") from error
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def create_successor(path: Path, data: bytes, mode: int, bound_parent_fd: int | None = None) -> Snapshot:
    parent_fd = directory_fd(path.parent) if bound_parent_fd is None else os.dup(bound_parent_fd)
    fd = -1
    try:
        fd, prepared = anonymous_file(parent_fd, data, mode)
        link_fd(fd, parent_fd, path.name)
        os.close(fd)
        fd = -1
        os.fsync(parent_fd)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (prepared.st_dev, prepared.st_ino):
            raise RollbackError("published successor path was rebound; foreign object preserved and manual recovery required")
        return Snapshot(path, data, current)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def fsync_directory(path: Path, bound_fd: int | None = None) -> None:
    fd = directory_fd(path) if bound_fd is None else os.dup(bound_fd)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ReplaceError(f"transaction journal {label} is not text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ReplaceError(f"transaction journal {label} is not canonical base64") from error


def journal_data(plan: Plan, args: Args) -> bytes:
    values: dict[str, str | int] = {
        "version": JOURNAL_VERSION,
        "root": str(args.root),
        "stale_task": args.stale_task,
        "successor_task": args.successor_task,
        "stale_target": args.stale_target,
        "manager_target": args.manager_target,
        "stale_sha256": args.stale_sha256,
        "todo_sha256": args.todo_sha256,
        "pending_item_sha256": args.pending_item_sha256,
        "stale_before": encoded(plan.stale.data),
        "stale_after": encoded(plan.stale_after),
        "successor_data": encoded(plan.successor_data),
        "todo_before": encoded(plan.todo.data),
        "todo_after": encoded(plan.todo_after),
        "stale_mode": stat.S_IMODE(plan.stale.state.st_mode),
        "todo_mode": stat.S_IMODE(plan.todo.state.st_mode),
    }
    values["commitment_sha256"] = digest(json.dumps(values, sort_keys=True, separators=(",", ":")).encode())
    data = (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_JOURNAL_BYTES:
        raise ReplaceError("prospective transaction journal exceeds the recovery size bound")
    return data


def read_journal(path: Path, args: Args, root_fd: int | None = None) -> Journal:
    snapshot = read_snapshot_at(root_fd, path.name, path, "transaction journal") if root_fd is not None else read_snapshot(path, "transaction journal")
    if snapshot.state.st_mode & 0o077 or len(snapshot.data) > MAX_JOURNAL_BYTES:
        raise ReplaceError("transaction journal must be owner-private and bounded")
    try:
        loaded: object = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplaceError(f"transaction journal is invalid JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise ReplaceError("transaction journal must be one object")
    expected = {
        "version", "root", "stale_task", "successor_task", "stale_target", "manager_target", "stale_sha256", "todo_sha256", "pending_item_sha256",
        "stale_before", "stale_after", "successor_data", "todo_before", "todo_after", "stale_mode", "todo_mode", "commitment_sha256",
    }
    binding = {
        "version": JOURNAL_VERSION,
        "root": str(args.root),
        "stale_task": args.stale_task,
        "successor_task": args.successor_task,
        "stale_target": args.stale_target,
        "manager_target": args.manager_target,
        "stale_sha256": args.stale_sha256,
        "todo_sha256": args.todo_sha256,
        "pending_item_sha256": args.pending_item_sha256,
    }
    if set(loaded) != expected or any(loaded.get(key) != value for key, value in binding.items()):
        raise ReplaceError("transaction journal identity or fields do not match this replacement")
    commitment = loaded.pop("commitment_sha256", None)
    if commitment != digest(json.dumps(loaded, sort_keys=True, separators=(",", ":")).encode()):
        raise ReplaceError("transaction journal integrity commitment does not match")
    stale_mode = loaded.get("stale_mode")
    todo_mode = loaded.get("todo_mode")
    if not isinstance(stale_mode, int) or not isinstance(todo_mode, int) or stale_mode & ~0o7777 or todo_mode & ~0o7777:
        raise ReplaceError("transaction journal file modes are invalid")
    return Journal(
        snapshot,
        decoded(loaded.get("stale_before"), "stale_before"),
        decoded(loaded.get("stale_after"), "stale_after"),
        decoded(loaded.get("successor_data"), "successor_data"),
        decoded(loaded.get("todo_before"), "todo_before"),
        decoded(loaded.get("todo_after"), "todo_after"),
        stale_mode,
        todo_mode,
    )


def stage_paths(root: Path, root_fd: int | None = None) -> tuple[Path, ...]:
    try:
        names = tuple(entry.name for entry in os.scandir(root_fd)) if root_fd is not None else tuple(path.name for path in root.iterdir())
        paths = tuple(sorted((root / name for name in names if STAGE_RE.fullmatch(name)), key=str))
    except OSError as error:
        raise ReplaceError(f"cannot inspect retained displaced-inode receipts: {error}") from error
    if len(paths) > MAX_DISPLACED_RECEIPTS:
        raise ReplaceError("too many retained displaced-inode receipts")
    return paths


def validate_displaced_receipts(root: Path, journal: Journal | None, root_fd: int | None = None) -> tuple[Snapshot, ...]:
    paths = stage_paths(root, root_fd)
    if paths and journal is None:
        raise ReplaceError("retained displaced-inode receipts exist without this transaction journal")
    if journal is None:
        return ()
    allowed = {
        STALE_TASK: ({journal.stale_before, journal.stale_after}, journal.stale_mode),
        "TODO.md": ({journal.todo_before, journal.todo_after}, journal.todo_mode),
    }
    receipts: list[Snapshot] = []
    for path in paths:
        match = STAGE_RE.fullmatch(path.name)
        if match is None:
            raise AssertionError("stage path was not matched by its selector")
        receipt = read_snapshot_at(root_fd, path.name, path, f"{match.group(1)} displaced-inode receipt") if root_fd is not None else read_snapshot(path, f"{match.group(1)} displaced-inode receipt")
        contents, mode = allowed[match.group(1)]
        if receipt.data not in contents or stat.S_IMODE(receipt.state.st_mode) != mode:
            raise ReplaceError(f"retained displaced-inode receipt {path.name} is not a canonical transaction state")
        receipts.append(receipt)
    for receipt in receipts:
        if root_fd is None:
            require_snapshot(receipt, f"{receipt.path.name} displaced-inode receipt")
        else:
            require_snapshot_at(receipt, f"{receipt.path.name} displaced-inode receipt", root_fd)
    return tuple(receipts)


def require_receipt_capacity(receipts: tuple[Snapshot, ...], additional: int) -> None:
    if additional < 0 or len(receipts) + additional > MAX_DISPLACED_RECEIPTS:
        raise ReplaceError("retained displaced-inode receipt capacity is insufficient for an atomic transaction")


def canonical_journal_after_state(args: Args, journal: Journal) -> tuple[bytes, bytes, bytes]:
    if digest(journal.stale_before) != args.stale_sha256 or digest(journal.todo_before) != args.todo_sha256:
        raise ReplaceError("transaction journal before-state digests do not match the invocation")
    expected = replacement_bytes(args, journal.stale_before, journal.todo_before)
    if expected != (journal.stale_after, journal.successor_data, journal.todo_after):
        raise ReplaceError("transaction journal after-state is not the canonical reconstruction")
    return expected


def prove_committed_state(
    *,
    root: Path,
    root_fd: int,
    root_identity: RootIdentity,
    target: str,
    journal: Snapshot,
    stale: Snapshot,
    todo: Snapshot,
    successor: Snapshot,
    receipts: tuple[Snapshot, ...],
) -> None:
    generation = directory_generation(root_fd)
    require_snapshot_at(journal, "transaction journal", root_fd)
    require_snapshot_at(stale, "stale task", root_fd)
    require_snapshot_at(todo, "TODO", root_fd)
    require_snapshot_at(successor, "successor task", root_fd)
    for receipt in receipts:
        require_snapshot_at(receipt, f"{receipt.path.name} displaced-inode receipt", root_fd)
    owners = active_owners(root, target, {}, root_fd)
    if owners != (successor.path,):
        raise ReplaceError("final state does not have exactly one committed successor owner")
    if checked_pane_id(target):
        raise ReplaceError("stale target became live at the final commit boundary")
    require_root_identity(root_identity)
    if directory_generation(root_fd) != generation:
        raise ReplaceError("work-log root namespace changed during the final commit proof")


def recover_journal(args: Args, path: Path, root_identity: RootIdentity, root_fd: int) -> Journal | bool | None:
    root = args.root
    target = args.stale_target
    if not entry_exists(root_fd, path.name):
        _ = validate_displaced_receipts(root, None, root_fd)
        return None
    journal = read_journal(path, args, root_fd)
    _ = canonical_journal_after_state(args, journal)
    receipts = validate_displaced_receipts(root, journal, root_fd)
    if checked_pane_id(target):
        raise ReplaceError("stale target is live; transaction recovery refused")
    stale_path = root / STALE_TASK
    successor_path = root / SUCCESSOR_TASK
    todo_path = root / "TODO.md"
    stale = read_snapshot_at(root_fd, stale_path.name, stale_path, "stale task")
    todo = read_snapshot_at(root_fd, todo_path.name, todo_path, "TODO")
    successor = read_snapshot_at(root_fd, successor_path.name, successor_path, "successor task") if entry_exists(root_fd, successor_path.name) else None
    if stale.data not in {journal.stale_before, journal.stale_after}:
        raise ReplaceError("transaction recovery found unrecognized stale-task bytes")
    if todo.data not in {journal.todo_before, journal.todo_after}:
        raise ReplaceError("transaction recovery found unrecognized TODO bytes")
    if successor is not None and successor.data != journal.successor_data:
        raise ReplaceError("transaction recovery found an unrecognized successor")
    if successor is not None and stat.S_IMODE(successor.state.st_mode) != journal.stale_mode:
        raise ReplaceError("transaction recovery found changed successor mode")
    if stat.S_IMODE(stale.state.st_mode) != journal.stale_mode or stat.S_IMODE(todo.state.st_mode) != journal.todo_mode:
        raise ReplaceError("transaction recovery found changed task or TODO modes")
    committed = stale.data == journal.stale_after and todo.data == journal.todo_after and successor is not None
    if committed and successor is not None:
        owners = active_owners(root, target, {}, root_fd)
        if owners != (successor_path.resolve(strict=True),):
            raise ReplaceError("transaction recovery cannot prove one committed successor owner")
        try:
            fsync_directory(root, root_fd)
        except OSError as error:
            raise ReplaceError("committed transaction directory sync failed; retry required") from error
        if active_owners(root, target, {}, root_fd) != owners:
            raise ReplaceError("committed owner set changed during recovery synchronization")
        prove_committed_state(root=root, root_fd=root_fd, root_identity=root_identity, target=target, journal=journal.snapshot, stale=stale, todo=todo, successor=successor, receipts=receipts)
        return True
    if successor is not None:
        raise ReplaceError("transaction recovery found a successor before a complete committed state; foreign object preserved")
    expected_owners = (stale_path.resolve(strict=True),) if stale.data == journal.stale_before else ()
    if active_owners(root, target, {}, root_fd) != expected_owners:
        raise ReplaceError("transaction recovery cannot prove the expected pre-rollback owner set")
    require_snapshot_at(journal.snapshot, "transaction journal", root_fd)
    n_recovery_exchanges = int(todo.data == journal.todo_after) + int(stale.data == journal.stale_after)
    require_receipt_capacity(receipts, n_recovery_exchanges + MAX_TRANSACTION_EXCHANGES)
    failures: list[str] = []
    if todo.data == journal.todo_after:
        try:
            require_root_identity(root_identity)
            todo = replace_existing(todo, journal.todo_before, root_fd)
        except Exception as error:
            failures.append(f"TODO: {error}")
    if stale.data == journal.stale_after:
        if active_owners(root, target, {}, root_fd) != ():
            raise ReplaceError("transaction recovery owner set changed before stale-task rollback")
        try:
            require_root_identity(root_identity)
            stale = replace_existing(stale, journal.stale_before, root_fd)
        except Exception as error:
            failures.append(f"stale task: {error}")
    if failures:
        raise ReplaceError(f"transaction recovery rollback failed ({'; '.join(failures)}); journal retained for manual recovery")
    if stale.data != journal.stale_before or todo.data != journal.todo_before or entry_exists(root_fd, successor_path.name):
        raise ReplaceError("transaction recovery rollback verification failed; journal retained")
    owners = active_owners(root, target, {}, root_fd)
    if owners != (stale_path.resolve(strict=True),):
        raise ReplaceError("transaction recovery cannot prove restored stale ownership; journal retained")
    _ = validate_displaced_receipts(root, journal, root_fd)
    return journal


def rollback(plan: Plan, stale_after: Snapshot | None, successor: Snapshot | None, todo_after: Snapshot | None, root_identity: RootIdentity, root_fd: int) -> tuple[str, ...]:
    failures: list[str] = []
    if successor is not None:
        return ("successor was published; transaction journal retained for recovery",)
    restored_todo = plan.todo
    restored_stale = plan.stale
    if todo_after is not None:
        try:
            require_root_identity(root_identity)
            restored_todo = replace_existing(todo_after, plan.todo.data, root_fd)
        except Exception as error:
            failures.append(f"TODO: {error}")
    if stale_after is not None:
        try:
            require_root_identity(root_identity)
            restored_stale = replace_existing(stale_after, plan.stale.data, root_fd)
        except Exception as error:
            failures.append(f"stale task: {error}")
    if not failures:
        try:
            require_root_identity(root_identity)
            if todo_after is not None:
                require_snapshot_at(restored_todo, "restored TODO", root_fd)
            if stale_after is not None:
                require_snapshot_at(restored_stale, "restored stale task", root_fd)
            owners = active_owners(plan.stale.path.parent, STALE_TARGET, {}, root_fd)
            if owners != (plan.stale.path,):
                raise ReplaceError("rollback did not restore the sole stale owner")
        except Exception as error:
            failures.append(f"verification: {error}")
    return tuple(failures)


def replace(args: Args) -> str:
    root = args.root
    todo_path = root / "TODO.md"
    stale_path = root / args.stale_task
    successor_path = root / args.successor_task
    journal_path = root / JOURNAL_NAME
    root_fd = directory_fd(root)
    root_state = os.fstat(root_fd)
    require_safe_directory(root_fd, root_state, root, trusted_writer_group())
    root_identity = RootIdentity(root, root_state.st_dev, root_state.st_ino)
    with ExitStack() as locks:
        locks.callback(os.close, root_fd)
        locks.enter_context(root_membership_lock(root))
        locks.enter_context(task_target_lock(root, args.stale_target))
        for path in sorted((todo_path, stale_path, successor_path, journal_path), key=str):
            locks.enter_context(task_file_lock(path))
        require_root_identity(root_identity)
        recovery = recover_journal(args, journal_path, root_identity, root_fd)
        if recovery is True:
            return f"recovered committed replacement of {args.stale_task} by unlaunched {args.successor_task}; separate supported launch/delivery remains required"
        plan = prepare(args, root_fd)
        require_snapshot_at(plan.stale, "stale task", root_fd)
        require_snapshot_at(plan.todo, "TODO", root_fd)
        if checked_pane_id(args.stale_target):
            raise ReplaceError("stale target is live")
        if isinstance(recovery, Journal):
            journal = recovery.snapshot
            if recovery.stale_after != plan.stale_after or recovery.successor_data != plan.successor_data or recovery.todo_after != plan.todo_after:
                raise ReplaceError("retained transaction journal candidate bytes no longer match preparation")
        else:
            require_receipt_capacity((), MAX_TRANSACTION_EXCHANGES)
            require_root_identity(root_identity)
            journal = create_successor(journal_path, journal_data(plan, args), 0o600, root_fd)
        retained_before = validate_displaced_receipts(root, read_journal(journal_path, args, root_fd), root_fd)
        require_receipt_capacity(retained_before, MAX_TRANSACTION_EXCHANGES)
        stale_after: Snapshot | None = None
        successor: Snapshot | None = None
        todo_after: Snapshot | None = None
        try:
            if checked_pane_id(args.stale_target):
                raise ReplaceError("stale target became live before replacement")
            require_root_identity(root_identity)
            stale_after = replace_existing(plan.stale, plan.stale_after, root_fd)
            if checked_pane_id(args.stale_target):
                raise ReplaceError("stale target became live during replacement")
            require_snapshot_at(plan.todo, "TODO", root_fd)
            if checked_pane_id(args.stale_target):
                raise ReplaceError("stale target became live during replacement")
            require_root_identity(root_identity)
            todo_after = replace_existing(plan.todo, plan.todo_after, root_fd)
            if checked_pane_id(args.stale_target):
                raise ReplaceError("stale target became live before successor publication")
            require_root_identity(root_identity)
            successor = create_successor(plan.successor_path, plan.successor_data, stat.S_IMODE(plan.stale.state.st_mode), root_fd)
            owners = active_owners(root, args.stale_target, {}, root_fd)
            if owners != (plan.successor_path,):
                raise ReplaceError(f"committed state does not have exactly one active successor owner: {', '.join(path.name for path in owners) or 'none'}")
            require_snapshot_at(stale_after, "stale task", root_fd)
            require_snapshot_at(successor, "successor task", root_fd)
            require_snapshot_at(todo_after, "TODO", root_fd)
            if checked_pane_id(args.stale_target):
                raise ReplaceError("stale target became live before commit verification")
        except Exception as error:
            if isinstance(error, CommittedWriteError):
                if error.snapshot.path == stale_path:
                    stale_after = error.snapshot
                elif error.snapshot.path == todo_path:
                    todo_after = error.snapshot
            foreign_successor = entry_exists(root_fd, successor_path.name)
            if isinstance(error, NamespaceMutationError):
                failures = ("an exchanged namespace could not be restored with proven identities; transaction journal retained",)
            elif foreign_successor and successor is None:
                failures = ("successor absence became unknown; committed predecessor state and transaction journal retained",)
            else:
                failures = rollback(plan, stale_after, successor, todo_after, root_identity, root_fd)
            if failures:
                raise ReplaceError(f"replacement failed and rollback failed ({'; '.join(failures)}); manual recovery required") from error
            if isinstance(error, RollbackError):
                raise error
            if isinstance(error, ReplaceError):
                raise ReplaceError(f"replacement failed; all changes rolled back: {error}") from error
            raise ReplaceError(f"replacement failed; all changes rolled back: {error}") from error
        require_snapshot_at(journal, "transaction journal", root_fd)
        require_root_identity(root_identity)
        retained = validate_displaced_receipts(root, read_journal(journal_path, args, root_fd), root_fd)
        if stale_after is None or todo_after is None or successor is None:
            raise AssertionError("successful replacement lacks a committed lifecycle snapshot")
        prove_committed_state(root=root, root_fd=root_fd, root_identity=root_identity, target=args.stale_target, journal=journal, stale=stale_after, todo=todo_after, successor=successor, receipts=retained)
        return f"replaced {args.stale_task} with unlaunched {args.successor_task} at {args.stale_target}; separate supported launch/delivery remains required"


def main(argv: list[str] | None = None) -> int:
    try:
        print(replace(parse_args(sys.argv[1:] if argv is None else argv)))
    except (OSError, ReplaceError, TaskFrontmatterError) as error:
        print(f"omo_hees_final_artifact_replace.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
