#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Close one pinned manager and publish one unlaunched successor transactionally."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import has_bound_close_proof, stop
from omo_manager.omo_codex_start import PCODX_ENV_KEYS as START_PCODX_ENV_KEYS
from omo_manager.omo_codex_start import Pane as StartPane
from omo_manager.omo_codex_start import pcodx_state
from omo_manager.omo_task import manager_owner_migration_text
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskFrontmatterError, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import (
    TODO_ROW_RE,
    active_child_task_refs,
    authoritative_active_target_task_paths,
    root_membership_lock,
    update_frontmatter_status,
)

AUDIT_VERSION = "v1.0.0"
AUDIT_OPERATION = "manager-replace"
SUCCESSOR_BLOCKER = "awaiting separate supported launch after atomic manager replacement ownership proof"
PCODX_ENV_KEYS = ("PCODX_POC_ROOT", "PCODX_RUN_DIR", "PCODX_LEDGER_PATH", "PCODX_SESSION_ID")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# tmux allocates pane ids from zero; `%0` is a valid first pane.
PANE_ID_RE = re.compile(r"^%[0-9]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TASK_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+\.md$")
AUTHORITY_REF_RE = re.compile(r"^(?:[0-9]{6}/)?manager_mail/[A-Za-z0-9_.-]+\.txt$")
HUMAN_ENVELOPE_RE = re.compile(
    r'(?ms)^<human_instruction[ \t]+authoritative="true"[ \t]+source="(?P<source>[^"\r\n]+)">\r?\n'
    r'(?P<body>.*?)\r?\n</human_instruction>[ \t]*(?:\r?\n|$)'
)
FAILED_MANAGER_EVIDENCE_RE = re.compile(
    r"(?is)\b(?:agent|manager)\s+(?:has\s+)?failed\b.*\b(?:did\s+not|didn't|has\s+not|hasn't)\b.*\breplace\b"
)
# 🧑 Source `manager_mail/85c5dff58359-1269.txt:3-9`: "The guest has reported that they do not receive response ... previous responsible agents ... completely failed. Replace them."
GUEST1269_REPLACEMENT = (
    "manager_mail/85c5dff58359-1269.txt",
    (3, 9),
    "guest_hees_mail_mgr.md",
    "guest_hees:0.0",
    "\n".join(
        (
            "The guest has reported that they do not receive response for emails sent to",
            "you guys. Whatever the previous responsible agents were doing, they",
            "completely failed. Replace them. The new agent should be skeptical of",
            "anything done previously and make sure that in the future replies get sent",
            "to the guest also It was not like the guest received nothing. They report",
            "receiving empty emails. Investigate this with the new agents. Completing",
            "overhaul any garbage that's left.",
        )
    ),
)
PCODX_REPLACE_EVIDENCE_RE = re.compile(
    r"(?m)^Replace the failed PCODX manager (?P<task>[A-Za-z0-9_./-]+\.md) at "
    r"(?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) with one fresh plain-Codex manager "
    r"inheriting all tasks and comments\.[ \t]*$"
)
PCODX_REPLACE_DIRECTIVE_RE = re.compile(r"(?m)^Replace the failed PCODX manager\b.*$")
MAX_AUDIT_BYTES = 8 * 1024 * 1024
POSIX_ACL_XATTRS = {"system.posix_acl_access", "system.posix_acl_default"}
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400
RENAME_EXCHANGE = 2
RENAME_NOREPLACE = 1
LIBC = ctypes.CDLL(None, use_errno=True)


class ReplaceError(RuntimeError):
    """The manager replacement failed closed."""


class CommittedMutationError(ReplaceError):
    """A namespace mutation committed, but its durability sync failed."""

    def __init__(self, label: str, snapshot: Snapshot, error: OSError) -> None:
        super().__init__(f"{label} committed but directory sync failed: {error}")
        self.label = label
        self.snapshot = snapshot


class NamespaceMutationError(ReplaceError):
    """An atomic namespace operation could not be resolved safely."""


@dataclass(frozen=True)
class ChildPin:
    task: str
    sha256: str


@dataclass(frozen=True, order=True)
class LineRange:
    start: int
    end: int


@dataclass(frozen=True)
class Args:
    root: Path
    old_task: str
    successor_task: str
    old_target: str
    new_target: str
    parent_target: str
    old_sha256: str
    todo_sha256: str
    children: tuple[ChildPin, ...]
    old_pane_id: str
    old_pane_pid: int
    old_pane_start_ticks: int
    old_session_id: str
    authority_file: str
    authority_lines: LineRange
    authority_sha256: str
    authority_envelope_task: str
    authority_envelope_sha256: str
    successor_item_lines: tuple[LineRange, ...]
    protected_targets: tuple[str, ...]
    audit_output: Path
    preparer: str
    reviewer: str
    old_queue_sha256: str = ""
    old_pcodx_state_sha256: str = ""
    old_pcodx_ledger_sha256: str = ""
    old_pcodx_wrapper_sha256: str = ""
    protected_targets_sha256: str = ""
    authority_envelope_file_sha256: str = ""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    state: os.stat_result


@dataclass(frozen=True)
class PaneIdentity:
    target: str
    pane_id: str
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class Plan:
    old: Snapshot
    todo: Snapshot
    authority: Snapshot
    authority_envelope: Snapshot
    children: tuple[Snapshot, ...]
    successor_path: Path
    old_after: bytes
    child_after: tuple[bytes, ...]
    todo_after: bytes
    successor_data: bytes
    successor_queue: tuple[str, ...]
    child_queues: tuple[tuple[str, ...], ...]
    initial_markdown_paths: tuple[Path, ...]
    protected_identities: tuple[PaneIdentity, ...]


@dataclass(frozen=True)
class AuditEntry:
    task: str
    before: bytes | None
    after: bytes
    mode: int
    gid: int


@dataclass(frozen=True)
class Recovery:
    plan: Plan
    record: dict[str, object]
    audit_bytes: bytes
    entries: tuple[AuditEntry, ...]
    owner_stopped: bool
    result: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    old_task: str = ""
    successor_task: str = ""
    old_target: str = ""
    new_target: str = ""
    parent_target: str = ""
    old_sha256: str = ""
    todo_sha256: str = ""
    child: list[str] = []
    old_pane_id: str = ""
    old_pane_pid: int = 0
    old_pane_start_ticks: int = 0
    old_session_id: str = ""
    authority_file: str = ""
    authority_lines: str = ""
    authority_sha256: str = ""
    authority_envelope_task: str = ""
    authority_envelope_sha256: str = ""
    successor_item_lines: list[str] = []
    protected_target: list[str] = []
    audit_output: Path = Path()
    preparer: str = ""
    reviewer: str = ""
    old_queue_sha256: str = ""
    old_pcodx_state_sha256: str = ""
    old_pcodx_ledger_sha256: str = ""
    old_pcodx_wrapper_sha256: str = ""
    protected_targets_sha256: str = ""
    authority_envelope_file_sha256: str = ""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_digest(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def is_pcodx_replacement(args: Args) -> bool:
    return bool(args.old_pcodx_state_sha256)


def canonical_target(target: str) -> str:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?", target)
    if match is None:
        raise ReplaceError(f"invalid tmux target: {target}")
    session, window, pane = match.groups()
    return f"{session}:{int(window)}.{int(pane or '0')}"


def target_session(target: str) -> str:
    return canonical_target(target).partition(":")[0]


def parse_child(value: str) -> ChildPin:
    task, separator, sha256 = value.rpartition("=")
    if not separator or TASK_REF_RE.fullmatch(task) is None or SHA256_RE.fullmatch(sha256) is None:
        raise argparse.ArgumentTypeError("--child must be TASK.md=SHA256")
    return ChildPin(task, sha256)


def parse_line_range(value: str) -> LineRange:
    start, separator, end = value.partition("-")
    if not separator or not start.isdigit() or not end.isdigit() or int(start) <= 0 or int(end) < int(start):
        raise argparse.ArgumentTypeError("line range must be positive START-END")
    return LineRange(int(start), int(end))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--old-task", required=True)
    _ = parser.add_argument("--successor-task", required=True)
    _ = parser.add_argument("--old-target", required=True)
    _ = parser.add_argument("--new-target", required=True)
    _ = parser.add_argument("--parent-target", required=True)
    _ = parser.add_argument("--old-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--child", action="append", default=[], type=parse_child)
    _ = parser.add_argument("--old-pane-id", required=True)
    _ = parser.add_argument("--old-pane-pid", required=True, type=int)
    _ = parser.add_argument("--old-pane-start-ticks", required=True, type=int)
    _ = parser.add_argument("--old-session-id", required=True)
    _ = parser.add_argument("--authority-file", required=True)
    _ = parser.add_argument("--authority-lines", required=True, type=parse_line_range)
    _ = parser.add_argument("--authority-sha256", required=True)
    _ = parser.add_argument("--authority-envelope-task", required=True)
    _ = parser.add_argument("--authority-envelope-sha256", required=True)
    _ = parser.add_argument("--successor-item-lines", action="append", required=True, type=parse_line_range)
    _ = parser.add_argument("--protected-target", action="append", default=[])
    _ = parser.add_argument("--audit-output", required=True, type=Path)
    _ = parser.add_argument("--preparer", required=True)
    _ = parser.add_argument("--reviewer", required=True)
    _ = parser.add_argument("--old-queue-sha256", default="")
    _ = parser.add_argument("--old-pcodx-state-sha256", default="")
    _ = parser.add_argument("--old-pcodx-ledger-sha256", default="")
    _ = parser.add_argument("--old-pcodx-wrapper-sha256", default="")
    _ = parser.add_argument("--protected-targets-sha256", default="")
    _ = parser.add_argument("--authority-envelope-file-sha256", default="")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    for value, label in (
        (parsed.old_sha256, "old task"),
        (parsed.todo_sha256, "TODO"),
        (parsed.authority_sha256, "authority"),
        (parsed.authority_envelope_sha256, "authority envelope"),
    ):
        if SHA256_RE.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    pcodx_digests = (
        parsed.old_queue_sha256,
        parsed.old_pcodx_state_sha256,
        parsed.old_pcodx_ledger_sha256,
        parsed.old_pcodx_wrapper_sha256,
        parsed.protected_targets_sha256,
        parsed.authority_envelope_file_sha256,
    )
    if any(pcodx_digests) and not all(SHA256_RE.fullmatch(value) for value in pcodx_digests):
        parser.error("PCODX replacement requires all six exact lowercase SHA-256 bindings")
    if any(
        TASK_REF_RE.fullmatch(task) is None
        for task in (parsed.old_task, parsed.successor_task, parsed.authority_envelope_task)
    ):
        parser.error("task arguments must be canonical relative Markdown paths")
    if parsed.old_task == parsed.successor_task:
        parser.error("old and successor tasks must differ")
    if PANE_ID_RE.fullmatch(parsed.old_pane_id) is None or parsed.old_pane_pid <= 0 or parsed.old_pane_start_ticks <= 0:
        parser.error("old pane identity must contain a positive pane id, pid, and process start tick")
    if UUID_RE.fullmatch(parsed.old_session_id) is None:
        parser.error("--old-session-id must be one exact UUID")
    if AUTHORITY_REF_RE.fullmatch(parsed.authority_file) is None:
        parser.error("--authority-file must be one canonical manager_mail/*.txt reference")
    item_ranges = tuple(sorted(parsed.successor_item_lines))
    if len(set(item_ranges)) != len(item_ranges) or any(
        item.start < parsed.authority_lines.start or item.end > parsed.authority_lines.end for item in item_ranges
    ):
        parser.error("successor item line ranges must be unique and contained in --authority-lines")
    if not parsed.audit_output.is_absolute():
        parser.error("--audit-output must be absolute")
    if not parsed.preparer.strip() or not parsed.reviewer.strip() or parsed.preparer.strip() == parsed.reviewer.strip():
        parser.error("preparer and independent reviewer must be distinct nonempty identities")
    children = tuple(sorted(parsed.child, key=lambda child: child.task))
    if len({child.task for child in children}) != len(children):
        parser.error("--child task references must be unique")
    try:
        targets = (parsed.old_target, parsed.new_target, parsed.parent_target, *parsed.protected_target)
        _ = tuple(canonical_target(target) for target in targets)
    except ReplaceError as exc:
        parser.error(str(exc))
    return Args(
        root=parsed.root.expanduser().resolve(strict=False),
        old_task=parsed.old_task,
        successor_task=parsed.successor_task,
        old_target=parsed.old_target,
        new_target=parsed.new_target,
        parent_target=parsed.parent_target,
        old_sha256=parsed.old_sha256,
        todo_sha256=parsed.todo_sha256,
        children=children,
        old_pane_id=parsed.old_pane_id,
        old_pane_pid=parsed.old_pane_pid,
        old_pane_start_ticks=parsed.old_pane_start_ticks,
        old_session_id=parsed.old_session_id.lower(),
        authority_file=parsed.authority_file,
        authority_lines=parsed.authority_lines,
        authority_sha256=parsed.authority_sha256,
        authority_envelope_task=parsed.authority_envelope_task,
        authority_envelope_sha256=parsed.authority_envelope_sha256,
        successor_item_lines=item_ranges,
        protected_targets=tuple(parsed.protected_target),
        audit_output=parsed.audit_output.resolve(strict=False),
        preparer=parsed.preparer.strip(),
        reviewer=parsed.reviewer.strip(),
        old_queue_sha256=parsed.old_queue_sha256,
        old_pcodx_state_sha256=parsed.old_pcodx_state_sha256,
        old_pcodx_ledger_sha256=parsed.old_pcodx_ledger_sha256,
        old_pcodx_wrapper_sha256=parsed.old_pcodx_wrapper_sha256,
        protected_targets_sha256=parsed.protected_targets_sha256,
        authority_envelope_file_sha256=parsed.authority_envelope_file_sha256,
    )


def task_path(root: Path, task: str) -> Path:
    lexical = root.joinpath(*Path(task).parts)
    candidate = lexical.resolve(strict=False)
    if candidate != lexical or candidate == root or root not in candidate.parents:
        raise ReplaceError(f"task escapes work-log root: {task}")
    return candidate


def file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_snapshot(path: Path, label: str) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReplaceError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or not before.st_mode & stat.S_IWUSR or before.st_mode & 0o022:
            raise ReplaceError(f"{label} must be one owner-owned, owner-writable regular file")
        if set(os.listxattr(fd)) & POSIX_ACL_XATTRS:
            raise ReplaceError(f"{label} has a POSIX ACL that this transaction cannot preserve")
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
        raise ReplaceError(f"{label} changed during manager replacement")


def directory_fd(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )


def sync_directory(path: Path) -> None:
    fd = directory_fd(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_snapshot_at(parent_fd: int, name: str, path: Path, label: str) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ReplaceError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or not before.st_mode & stat.S_IWUSR or before.st_mode & 0o022:
            raise ReplaceError(f"{label} must be one owner-owned, owner-writable regular file")
        if set(os.listxattr(fd)) & POSIX_ACL_XATTRS:
            raise ReplaceError(f"{label} has a POSIX ACL that this transaction cannot preserve")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if file_identity(before) != file_identity(after):
        raise ReplaceError(f"{label} changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def require_snapshot_at(expected: Snapshot, label: str, parent_fd: int) -> None:
    current = read_snapshot_at(parent_fd, expected.path.name, expected.path, label)
    if file_identity(current.state) != file_identity(expected.state) or current.data != expected.data:
        raise ReplaceError(f"{label} changed during manager replacement")


def anonymous_file(parent_fd: int, data: bytes, mode: int, gid: int) -> tuple[int, os.stat_result]:
    if not getattr(os, "O_TMPFILE", 0):
        raise ReplaceError("atomic manager replacement requires Linux O_TMPFILE support")
    flags = os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(".", flags, mode, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short anonymous-file write")
            view = view[written:]
        os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
        return fd, os.fstat(fd)
    except Exception:
        os.close(fd)
        raise


def link_fd(fd: int, parent_fd: int, name: str) -> None:
    source = os.fsencode(f"/proc/self/fd/{fd}")
    result = LIBC.linkat(
        AT_FDCWD,
        ctypes.c_char_p(source),
        parent_fd,
        ctypes.c_char_p(os.fsencode(name)),
        AT_SYMLINK_FOLLOW,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def rename_at2(parent_fd: int, source: str, target: str, flags: int) -> None:
    result = LIBC.renameat2(
        parent_fd,
        ctypes.c_char_p(os.fsencode(source)),
        parent_fd,
        ctypes.c_char_p(os.fsencode(target)),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source}->{target}")


def replace_snapshot(expected: Snapshot, data: bytes, label: str) -> Snapshot:
    """Exchange a prepared inode with the exact expected inode; never clobber a rebound path."""

    parent_fd = directory_fd(expected.path.parent)
    fd = -1
    stage_name = f".{expected.path.name}.omo-manager-replace-stage-{os.urandom(16).hex()}"
    try:
        require_snapshot_at(expected, label, parent_fd)
        fd, _ = anonymous_file(parent_fd, data, stat.S_IMODE(expected.state.st_mode), expected.state.st_gid)
        link_fd(fd, parent_fd, stage_name)
        prepared = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        os.close(fd)
        fd = -1
        require_snapshot_at(expected, label, parent_fd)
        rename_at2(parent_fd, stage_name, expected.path.name, RENAME_EXCHANGE)
        try:
            target_state = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
            receipt_state = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise NamespaceMutationError(f"{label} exchange committed but its namespace cannot be inspected") from exc
        target_identity = (target_state.st_dev, target_state.st_ino)
        receipt_identity = (receipt_state.st_dev, receipt_state.st_ino)
        if target_identity != (prepared.st_dev, prepared.st_ino) or receipt_identity != (expected.state.st_dev, expected.state.st_ino):
            try:
                current_target = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
                current_receipt = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current_target.st_dev, current_target.st_ino) != target_identity or (
                    current_receipt.st_dev,
                    current_receipt.st_ino,
                ) != receipt_identity:
                    raise NamespaceMutationError(f"{label} exchange paths changed before safe restoration")
                rename_at2(parent_fd, stage_name, expected.path.name, RENAME_EXCHANGE)
                restored = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (restored.st_dev, restored.st_ino) != (expected.state.st_dev, expected.state.st_ino):
                    raise NamespaceMutationError(f"{label} original inode was not restored")
                os.fsync(parent_fd)
            except (OSError, NamespaceMutationError) as exc:
                raise NamespaceMutationError(f"{label} was concurrently rebound; foreign state was preserved for recovery") from exc
            raise ReplaceError(f"{label} was concurrently rebound; its original inode was restored")
        try:
            receipt = read_snapshot_at(parent_fd, stage_name, expected.path.parent / stage_name, f"{label} displaced-inode receipt")
            if file_identity(receipt.state) != file_identity(receipt_state) or receipt.data != expected.data:
                raise NamespaceMutationError(f"{label} displaced-inode receipt changed")
            snapshot = read_snapshot_at(parent_fd, expected.path.name, expected.path, f"updated {label}")
            if file_identity(snapshot.state) != file_identity(target_state) or snapshot.data != data:
                raise NamespaceMutationError(f"{label} was rebound after its committed exchange")
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise CommittedMutationError(label, snapshot, exc) from exc
            require_snapshot_at(receipt, f"{label} displaced-inode receipt", parent_fd)
            return snapshot
        except (CommittedMutationError, NamespaceMutationError):
            raise
        except Exception as exc:
            raise NamespaceMutationError(f"{label} committed but verification failed; receipts were preserved") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def create_snapshot(path: Path, data: bytes, mode: int, gid: int | None = None) -> Snapshot:
    """Publish one prepared inode with linkat's atomic no-replace semantics."""

    parent_fd = directory_fd(path.parent)
    fd = -1
    try:
        fd, prepared = anonymous_file(parent_fd, data, mode, os.getgid() if gid is None else gid)
        link_fd(fd, parent_fd, path.name)
        os.close(fd)
        fd = -1
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        snapshot = read_snapshot_at(parent_fd, path.name, path, f"published {path.name}")
        if (current.st_dev, current.st_ino) != (prepared.st_dev, prepared.st_ino) or snapshot.data != data:
            raise NamespaceMutationError(f"published {path.name} was rebound; foreign state was preserved")
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise CommittedMutationError(path.name, snapshot, exc) from exc
        return snapshot
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def remove_created(expected: Snapshot) -> None:
    """Quarantine the exact created inode atomically; never unlink a rebound path."""

    parent_fd = directory_fd(expected.path.parent)
    receipt_name = f".{expected.path.name}.omo-manager-replace-removed-{os.urandom(16).hex()}"
    try:
        require_snapshot_at(expected, "transaction-created successor", parent_fd)
        rename_at2(parent_fd, expected.path.name, receipt_name, RENAME_NOREPLACE)
        receipt_state = os.stat(receipt_name, dir_fd=parent_fd, follow_symlinks=False)
        if (receipt_state.st_dev, receipt_state.st_ino) != (expected.state.st_dev, expected.state.st_ino):
            try:
                rename_at2(parent_fd, receipt_name, expected.path.name, RENAME_NOREPLACE)
                os.fsync(parent_fd)
            except OSError as exc:
                raise NamespaceMutationError("successor path was rebound and the foreign inode could not be restored") from exc
            raise ReplaceError("successor path was rebound; the foreign inode was restored")
        receipt = read_snapshot_at(parent_fd, receipt_name, expected.path.parent / receipt_name, "removed-successor receipt")
        if receipt.data != expected.data:
            raise NamespaceMutationError("removed-successor receipt changed; state preserved for recovery")
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def metadata(data: bytes, root: Path, label: str) -> TaskMetadata:
    try:
        value = parse_task_metadata(data.decode(), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise ReplaceError(f"{label} has invalid task frontmatter: {exc}") from exc
    if value is None:
        raise ReplaceError(f"{label} has no task frontmatter")
    return value


def replace_v1_fields(
    text: str,
    *,
    status: str,
    runat: str,
    blocked_on: str,
    remove_session: bool,
    tool: str | None = None,
) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ReplaceError("manager task has no frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ReplaceError("manager task frontmatter is unterminated") from exc
    indexes: dict[str, list[int]] = {}
    for index, line in enumerate(lines[1:closing], start=1):
        key, separator, _value = line.rstrip("\r\n").partition(":")
        if separator:
            indexes.setdefault(key, []).append(index)
    required_fields = ("status", "runat", "tool") if tool is not None else ("status", "runat")
    for key in required_fields:
        if len(indexes.get(key, [])) != 1:
            raise ReplaceError(f"manager task requires exactly one {key} field")
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
    lines[indexes["status"][0]] = f"status: {status}{ending}"
    lines[indexes["runat"][0]] = f"runat: {runat}{ending}"
    if tool is not None:
        lines[indexes["tool"][0]] = f"tool: {tool}{ending}"
    removed = [*indexes.get("blocked_on", []), *(indexes.get("session_id", []) if remove_session else [])]
    for index in sorted(removed, reverse=True):
        del lines[index]
        closing -= 1
    status_index = next(index for index, line in enumerate(lines[1:closing], start=1) if line.rstrip("\r\n").partition(":")[0] == "status")
    if blocked_on:
        lines.insert(status_index + 1, f"blocked_on: {blocked_on}{ending}")
    return "".join(lines)


def authority_material(args: Args, snapshot: Snapshot, envelope: Snapshot) -> tuple[str, ...]:
    """Authenticate failed-manager evidence without copying private mail into task records."""

    try:
        authority_parent = snapshot.path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"replacement authority directory is unavailable: {exc}") from exc
    if (
        snapshot.path != args.root.joinpath(*Path(args.authority_file).parts)
        or not stat.S_ISDIR(authority_parent.st_mode)
        or authority_parent.st_uid != os.getuid()
        or stat.S_IMODE(authority_parent.st_mode) & 0o077
        or stat.S_IMODE(snapshot.state.st_mode) & 0o077
    ):
        raise ReplaceError("replacement authority source and directory must be owner-private without symlink traversal")
    if digest(snapshot.data) != args.authority_sha256:
        raise ReplaceError("authority digest changed")
    if is_pcodx_replacement(args) and digest(envelope.data) != args.authority_envelope_file_sha256:
        raise ReplaceError("authority envelope file bytes changed")
    try:
        lines = snapshot.data.decode().splitlines()
        envelope_text = envelope.data.decode()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"authority source or envelope is not UTF-8: {exc}") from exc
    if args.authority_lines.end > len(lines):
        raise ReplaceError("authority line range exceeds the bound source")
    excerpt = "\n".join(lines[args.authority_lines.start - 1 : args.authority_lines.end])
    if not excerpt.strip():
        raise ReplaceError("authority excerpt must not be empty")
    locator = f"{args.authority_file}:{args.authority_lines.start}-{args.authority_lines.end}"
    matches = list(HUMAN_ENVELOPE_RE.finditer(envelope_text))
    if len(matches) != 1 or matches[0].group("source") != locator:
        raise ReplaceError("authority envelope must contain exactly one block for the bound source locator")
    match = matches[0]
    if digest(match.group(0).encode()) != args.authority_envelope_sha256:
        raise ReplaceError("authority envelope block digest changed")
    if "\n".join(match.group("body").splitlines()) != excerpt:
        raise ReplaceError("authority envelope does not contain exactly the bound source excerpt")
    items: list[str] = []
    for line_range in args.successor_item_lines:
        if line_range.start < args.authority_lines.start or line_range.end > args.authority_lines.end:
            raise ReplaceError("successor item line range is outside the authenticated authority excerpt")
        evidence = "\n".join(lines[line_range.start - 1 : line_range.end])
        if not evidence.strip():
            raise ReplaceError("successor queue item source lines must not be empty")
        item_locator = f"{args.authority_file}:{line_range.start}-{line_range.end}"
        items.append(
            "Read and execute the private authenticated Human instruction at "
            f"{item_locator} (source-sha256={args.authority_sha256}; "
            f"envelope={args.authority_envelope_task}; envelope-block-sha256={args.authority_envelope_sha256})."
        )
    selected_evidence = "\n".join(
        "\n".join(lines[line_range.start - 1 : line_range.end]) for line_range in args.successor_item_lines
    )
    pcodx_replacements = list(PCODX_REPLACE_EVIDENCE_RE.finditer(selected_evidence))
    pcodx_directives = PCODX_REPLACE_DIRECTIVE_RE.findall(selected_evidence)
    selected_nonempty_lines = [line.strip() for line in selected_evidence.splitlines() if line.strip()]
    pcodx_replacement = pcodx_replacements[0] if len(pcodx_replacements) == 1 else None
    exact_pcodx_replacement = (
        len(pcodx_directives) == 1
        and pcodx_replacement is not None
        and selected_nonempty_lines == [pcodx_replacement.group(0).strip(), "Just do it"]
        and pcodx_replacement.group("task") == args.old_task
        and canonical_target(pcodx_replacement.group("target")) == canonical_target(args.old_target)
    )
    guest_source, guest_lines, guest_task, guest_target, guest_evidence = GUEST1269_REPLACEMENT
    exact_guest1269_replacement = (
        args.authority_file == guest_source
        and args.authority_lines == LineRange(*guest_lines)
        and args.successor_item_lines == (args.authority_lines,)
        and args.old_task == guest_task
        and canonical_target(args.old_target) == guest_target
        and selected_evidence == guest_evidence
    )
    if (
        FAILED_MANAGER_EVIDENCE_RE.search(selected_evidence) is None
        and not exact_guest1269_replacement
        and not exact_pcodx_replacement
    ):
        raise ReplaceError("authenticated authority does not explicitly prove failure, non-execution, and replacement")
    if is_pcodx_replacement(args) and not all(value in selected_evidence for value in (args.old_task, args.old_target)):
        raise ReplaceError("authenticated PCODX replacement authority must name the exact old task and protected target")
    if is_pcodx_replacement(args) and "pcodx" not in selected_evidence.lower():
        raise ReplaceError("authenticated PCODX replacement authority must explicitly identify PCODX")
    if is_pcodx_replacement(args):
        subject_token = re.compile(rf"(?im)^Subject:.*(?<![A-Za-z0-9_./-]){re.escape(args.old_task)}(?![A-Za-z0-9_./-])")
        close_directive = re.compile(
            rf"(?im)^\s*close\s+{re.escape(args.old_target)}(?=$|[\s,.;:])"
        )
        source_text = snapshot.data.decode()
        if (len(subject_token.findall(source_text)) != 1 and not exact_pcodx_replacement) or (
            len(close_directive.findall(selected_evidence)) != 1 and not exact_pcodx_replacement
        ):
            raise ReplaceError("authenticated PCODX authority must directly close the exact named task and protected target")
    return tuple(items)


def protected_inventory(args: Args, inventory: dict[str, PaneIdentity]) -> tuple[PaneIdentity, ...]:
    protected = tuple(canonical_target(target) for target in args.protected_targets)
    if len(set(protected)) != len(protected):
        raise ReplaceError("protected targets must be unique")
    identities = tuple(inventory.get(target) for target in protected)
    if any(identity is None for identity in identities):
        raise ReplaceError("every protected target must retain one exact live pane/process identity")
    return tuple(identity for identity in identities if identity is not None)


def protected_inventory_digest(args: Args, inventory: dict[str, PaneIdentity]) -> str:
    return json_digest(
        [
            {
                "target": identity.target,
                "pane_id": identity.pane_id,
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
            }
            for identity in protected_inventory(args, inventory)
        ]
    )


def pcodx_binding(args: Args, pane: PaneIdentity) -> dict[str, str]:
    if START_PCODX_ENV_KEYS != PCODX_ENV_KEYS:
        raise ReplaceError("installed PCODX custody schema changed")
    state = pcodx_state(StartPane(pane.target, pane.pane_id, "", "", Path(), pane.pid))
    if tuple(state) != PCODX_ENV_KEYS or json_digest(state) != args.old_pcodx_state_sha256:
        raise ReplaceError("live PCODX identity, session, or custody changed")
    ledger = read_snapshot(Path(state["PCODX_LEDGER_PATH"]), "live PCODX ledger")
    if digest(ledger.data) != args.old_pcodx_ledger_sha256:
        raise ReplaceError("live PCODX ledger bytes changed")
    wrapper = read_snapshot(Path(__file__).resolve().with_name("pcodx"), "installed PCODX wrapper")
    if digest(wrapper.data) != args.old_pcodx_wrapper_sha256:
        raise ReplaceError("installed PCODX wrapper bytes changed")
    return state


def validate_live_bindings(args: Args, inventory: dict[str, PaneIdentity]) -> None:
    old = inventory.get(canonical_target(args.old_target))
    expected = PaneIdentity(canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid, args.old_pane_start_ticks)
    if old != expected:
        raise ReplaceError(f"old manager pane identity changed: expected {expected}, found {old}")
    if canonical_target(args.new_target) in inventory:
        raise ReplaceError("successor target is already live; launch-before-singular-proof is rejected")
    if is_pcodx_replacement(args):
        if protected_inventory_digest(args, inventory) != args.protected_targets_sha256:
            raise ReplaceError("protected pane/process inventory changed")
        _ = pcodx_binding(args, expected)


def old_and_successor_text(
    old_data: bytes,
    root: Path,
    old_target: str,
    new_target: str,
    authority_items: tuple[str, ...],
) -> tuple[bytes, bytes, tuple[str, ...]]:
    old_text = old_data.decode()
    old_metadata = metadata(old_data, root, "old manager task")
    queue = (*old_metadata.pending_task_items, *authority_items)
    if len(set(queue)) != len(queue):
        raise ReplaceError("successor queue would contain duplicate open items")
    cleared = render_pending_items(old_text, ())
    old_after = update_frontmatter_status(cleared, "done", "", root)
    successor = replace_v1_fields(
        old_text,
        status="blocked",
        runat=new_target,
        blocked_on=SUCCESSOR_BLOCKER,
        remove_session=True,
        tool="codex",
    )
    successor = render_pending_items(successor, queue)
    successor_metadata = parse_task_metadata(successor, root)
    if successor_metadata is None or successor_metadata.pending_task_items != queue:
        raise ReplaceError("successor construction did not preserve the manager queue")
    if successor_metadata.runat != new_target or successor_metadata.status != "blocked" or not successor_metadata.is_manager:
        raise ReplaceError("successor construction did not produce one blocked manager")
    old_after_metadata = parse_task_metadata(old_after, root)
    if old_after_metadata is None or old_after_metadata.status != "done" or old_after_metadata.pending_task_items:
        raise ReplaceError("old-manager construction did not produce one empty done record")
    if old_metadata.runat != old_target:
        raise ReplaceError("old manager target drifted")
    return old_after.encode(), successor.encode(), queue


def todo_task_path(root: Path, value: str) -> Path | None:
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve(strict=False)
    return resolved if root in resolved.parents else None


def todo_replacement(data: bytes, root: Path, old_path: Path, successor_path: Path, old_target: str, new_target: str) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"TODO is not UTF-8: {exc}") from exc
    lines = text.splitlines(keepends=True)
    contents = [line.rstrip("\r\n") for line in lines]
    headings = {name: [index for index, value in enumerate(contents) if value == f"{name}:"] for name in ("current", "human pending", "low priority", "previous")}
    if any(len(indexes) != 1 for indexes in headings.values()):
        raise ReplaceError("TODO must contain one canonical lifecycle section of each kind")
    order = tuple(headings[name][0] for name in ("current", "human pending", "low priority", "previous"))
    if order != tuple(sorted(order)):
        raise ReplaceError("TODO lifecycle sections are out of order")
    old_rows: list[tuple[int, re.Match[str]]] = []
    successor_rows = 0
    for index, content in enumerate(contents):
        match = TODO_ROW_RE.fullmatch(content)
        if match is None:
            if old_path.name in content or successor_path.name in content:
                raise ReplaceError("TODO contains a malformed old or successor row")
            continue
        resolved = todo_task_path(root, match.group(1))
        if resolved == old_path:
            old_rows.append((index, match))
        elif resolved == successor_path:
            successor_rows += 1
    if len(old_rows) != 1 or successor_rows:
        raise ReplaceError("TODO must contain exactly one old-manager row and no successor row")
    index, match = old_rows[0]
    if not (headings["current"][0] < index < headings["human pending"][0] or headings["human pending"][0] < index < headings["low priority"][0]):
        raise ReplaceError("old-manager TODO row must be current or human pending")
    suffix = (match.group(2) or "").strip()
    if suffix != old_target:
        raise ReplaceError("old-manager TODO row must name only its exact target")
    old_ref = match.group(1)
    if contents[index].strip().split(maxsplit=1)[0] != old_ref:
        raise ReplaceError("old-manager TODO row must use one canonical unquoted task reference")
    successor_ref = successor_path.relative_to(root).as_posix()
    ending = lines[index][len(contents[index]) :]
    prefix = contents[index][: match.start(1)]
    lines[index] = f"{prefix}{successor_ref} {new_target}{ending}"
    previous_index = headings["previous"][0]
    default_ending = "\r\n" if "\r\n" in text else "\n"
    previous_ending = lines[previous_index][len(contents[previous_index]) :] or default_ending
    if not lines[previous_index].endswith(("\n", "\r")):
        lines[previous_index] += previous_ending
    lines.insert(previous_index + 1, f"{old_ref} {old_target}{previous_ending}")
    return "".join(lines).encode()


def pane_inventory() -> dict[str, PaneIdentity]:
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplaceError(f"cannot inspect tmux pane inventory: {exc}") from exc
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())
        raise ReplaceError(f"tmux pane inventory failed: {detail or result.returncode}")
    inventory: dict[str, PaneIdentity] = {}
    for row in result.stdout.splitlines():
        fields = row.split("\t")
        if (
            len(fields) != 4
            or PANE_ID_RE.fullmatch(fields[1]) is None
            or not fields[2].isdigit()
            or fields[3] not in {"0", "1"}
        ):
            raise ReplaceError("tmux pane inventory contains a malformed row")
        target = canonical_target(fields[0])
        if fields[3] == "1":
            continue
        pid = int(fields[2])
        ticks = process_start_ticks(pid)
        if ticks is None or target in inventory:
            raise ReplaceError("tmux pane inventory cannot prove one process identity per target")
        inventory[target] = PaneIdentity(target, fields[1], pid, ticks)
    return inventory


def validate_targets(args: Args) -> None:
    pcodx_digests = (
        args.old_queue_sha256,
        args.old_pcodx_state_sha256,
        args.old_pcodx_ledger_sha256,
        args.old_pcodx_wrapper_sha256,
        args.protected_targets_sha256,
        args.authority_envelope_file_sha256,
    )
    if any(pcodx_digests) and not all(SHA256_RE.fullmatch(value) for value in pcodx_digests):
        raise ReplaceError("PCODX replacement requires all six exact SHA-256 bindings")
    old = canonical_target(args.old_target)
    new = canonical_target(args.new_target)
    if old == new:
        raise ReplaceError("old and new manager targets must differ")
    # 🧑 "Treat tmux sessions whose names begin with `h` as human-owned. Change one only when authoritative human text explicitly requests that exact action and session."
    pcodx_human = is_pcodx_replacement(args) and target_session(old).startswith("h")
    if target_session(new).startswith("h") or (target_session(old).startswith("h") and not pcodx_human):
        raise ReplaceError("only an exactly bound Human-authorized PCODX old manager may use a human-owned h* target")
    protected = {canonical_target(target) for target in args.protected_targets}
    if new in protected or (old in protected and not pcodx_human):
        raise ReplaceError("old or new target aliases an explicitly protected pane")
    if pcodx_human and old not in protected:
        raise ReplaceError("Human-owned PCODX old target must be included in the exact protected target set")
    if is_pcodx_replacement(args) != pcodx_human:
        raise ReplaceError("PCODX replacement bindings are accepted only for one protected human-owned old target")


def markdown_paths(root: Path) -> tuple[Path, ...]:
    try:
        discovered = tuple(root.rglob("*.md"))
    except OSError as exc:
        raise ReplaceError(f"cannot enumerate work-log task records: {exc}") from exc
    if any(path.is_symlink() for path in discovered):
        raise ReplaceError("work-log Markdown inventory contains an unsafe path")
    paths = tuple(sorted((path.resolve(strict=False) for path in discovered), key=str))
    if any(path == root or root not in path.parents for path in paths) or len(set(paths)) != len(paths):
        raise ReplaceError("work-log Markdown inventory contains an unsafe or duplicate path")
    return paths


def prepare(args: Args, paths: tuple[Path, ...]) -> Plan:
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    todo_path = args.root / "TODO.md"
    authority_path = task_path(args.root, args.authority_file)
    authority_envelope_path = task_path(args.root, args.authority_envelope_task)
    if successor_path.exists() or successor_path.is_symlink():
        raise ReplaceError("successor task already exists; launch-before-proof is rejected")
    path_set = set(paths)
    if old_path not in path_set or todo_path not in path_set or authority_envelope_path not in path_set:
        raise ReplaceError("old manager, TODO, or authority envelope is absent from the locked Markdown inventory")
    old = read_snapshot(old_path, "old manager task")
    todo = read_snapshot(todo_path, "TODO")
    authority = read_snapshot(authority_path, "replacement authority")
    authority_envelope = read_snapshot(authority_envelope_path, "replacement authority envelope")
    if digest(old.data) != args.old_sha256 or digest(todo.data) != args.todo_sha256:
        raise ReplaceError("old manager or TODO digest changed")
    old_metadata = metadata(old.data, args.root, "old manager task")
    if (
        old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status != "long_running"
        or old_metadata.runat != args.old_target
        or old_metadata.managerat != args.parent_target
        or old_metadata.tool != ("pcodx" if is_pcodx_replacement(args) else "codex")
        or not old_metadata.is_manager
        or (not is_pcodx_replacement(args) and old_metadata.session_id.lower() != args.old_session_id)
    ):
        raise ReplaceError("old manager must be the exact live long-running failed-manager record bound by the invocation")
    if json_digest(list(old_metadata.pending_task_items)) != args.old_queue_sha256 and is_pcodx_replacement(args):
        raise ReplaceError("old manager full ordered queue changed")
    old_owners = authoritative_active_target_task_paths(args.root, args.old_target)
    if old_owners != (old_path.resolve(),):
        raise ReplaceError("old target does not have exactly one authoritative active owner")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("new target already has an authoritative active owner; launch-before-proof is rejected")
    expected_children = tuple(sorted(child.task for child in args.children))
    actual_children = active_child_task_refs(args.root, old_path, args.old_target)
    if actual_children != expected_children:
        raise ReplaceError(f"active child set changed: expected {expected_children}, found {actual_children}")
    if is_pcodx_replacement(args) and len(expected_children) != 4:
        raise ReplaceError("Human-owned PCODX manager replacement requires the exact four-child Source-1228 set")
    pins = {child.task: child.sha256 for child in args.children}
    children: list[Snapshot] = []
    child_after: list[bytes] = []
    child_queues: list[tuple[str, ...]] = []
    for task in expected_children:
        path = task_path(args.root, task)
        if path not in path_set:
            raise ReplaceError(f"active child disappeared: {task}")
        snapshot = read_snapshot(path, f"active child {task}")
        if digest(snapshot.data) != pins[task]:
            raise ReplaceError(f"active child digest changed: {task}")
        child_metadata = metadata(snapshot.data, args.root, f"active child {task}")
        if child_metadata.status == "done" or child_metadata.managerat != args.old_target:
            raise ReplaceError(f"active child ownership changed: {task}")
        try:
            updated = manager_owner_migration_text(snapshot.data.decode(), args.old_target, args.new_target, args.root).encode()
        except (UnicodeDecodeError, ValueError, TaskFrontmatterError) as exc:
            raise ReplaceError(f"cannot prepare active child migration for {task}: {exc}") from exc
        children.append(snapshot)
        child_after.append(updated)
        child_queues.append(child_metadata.pending_task_items)
    authority_items = authority_material(args, authority, authority_envelope)
    old_after, successor_data, successor_queue = old_and_successor_text(
        old.data,
        args.root,
        args.old_target,
        args.new_target,
        authority_items,
    )
    todo_after = todo_replacement(todo.data, args.root, old_path, successor_path, args.old_target, args.new_target)
    protected_identities: tuple[PaneIdentity, ...] = ()
    if is_pcodx_replacement(args):
        inventory = pane_inventory()
        validate_live_bindings(args, inventory)
        protected_identities = protected_inventory(args, inventory)
    return Plan(
        old,
        todo,
        authority,
        authority_envelope,
        tuple(children),
        successor_path,
        old_after,
        tuple(child_after),
        todo_after,
        successor_data,
        successor_queue,
        tuple(child_queues),
        paths,
        protected_identities,
    )


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def audit_record(args: Args, plan: Plan, secret: str, commitment: str) -> dict[str, object]:
    files = [
        {
            "task": args.old_task,
            "before": encoded(plan.old.data),
            "after": encoded(plan.old_after),
            "mode": stat.S_IMODE(plan.old.state.st_mode),
            "gid": plan.old.state.st_gid,
        },
        *(
            {
                "task": pin.task,
                "before": encoded(snapshot.data),
                "after": encoded(after),
                "mode": stat.S_IMODE(snapshot.state.st_mode),
                "gid": snapshot.state.st_gid,
            }
            for pin, snapshot, after in zip(args.children, plan.children, plan.child_after, strict=True)
        ),
        {
            "task": "TODO.md",
            "before": encoded(plan.todo.data),
            "after": encoded(plan.todo_after),
            "mode": stat.S_IMODE(plan.todo.state.st_mode),
            "gid": plan.todo.state.st_gid,
        },
        {
            "task": args.successor_task,
            "before": None,
            "after": encoded(plan.successor_data),
            "mode": stat.S_IMODE(plan.old.state.st_mode),
            "gid": plan.old.state.st_gid,
        },
    ]
    record: dict[str, object] = {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "state": "prepared",
        "root": str(args.root),
        "old_task": args.old_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "parent_target": args.parent_target,
        "old_sha256": args.old_sha256,
        "todo_sha256": args.todo_sha256,
        "children": [{"task": child.task, "sha256": child.sha256} for child in args.children],
        "old_pane": {"id": args.old_pane_id, "pid": args.old_pane_pid, "start_ticks": args.old_pane_start_ticks},
        "old_session_id": args.old_session_id,
        "authority_file": args.authority_file,
        "authority_lines": [args.authority_lines.start, args.authority_lines.end],
        "authority_sha256": args.authority_sha256,
        "authority_envelope_task": args.authority_envelope_task,
        "authority_envelope_sha256": args.authority_envelope_sha256,
        "successor_item_lines": [[item.start, item.end] for item in args.successor_item_lines],
        "protected_targets": list(args.protected_targets),
        "preparer": args.preparer,
        "reviewer": args.reviewer,
        "close_proof_secret": secret,
        "close_proof_commitment": commitment,
        "completed_writes": [],
        "markdown_membership": [path.relative_to(args.root).as_posix() for path in plan.initial_markdown_paths],
        "files": files,
    }
    if is_pcodx_replacement(args):
        record.update(pcodx_audit_binding(args))
        record["protected_inventory"] = [
            {
                "target": identity.target,
                "pane_id": identity.pane_id,
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
            }
            for identity in plan.protected_identities
        ]
    return record


def serialized_audit(record: dict[str, object]) -> bytes:
    value = dict(record)
    value.pop("record_sha256", None)
    value["record_sha256"] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_AUDIT_BYTES:
        raise ReplaceError("private replacement audit exceeds the size bound")
    return data


def reserve_audit(path: Path, record: dict[str, object]) -> bytes:
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"audit directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ReplaceError("audit directory must be owner-private")
    data = serialized_audit(record)
    try:
        published = create_snapshot(path, data, 0o600)
    except (OSError, ReplaceError) as exc:
        raise ReplaceError(f"cannot reserve private replacement audit: {exc}") from exc
    if published.data != data or stat.S_IMODE(published.state.st_mode) != 0o600:
        raise ReplaceError("private replacement audit publication could not be proved")
    return data


def close_authority_record(args: Args, audit_bytes: bytes, commitment: str) -> dict[str, object]:
    """Return the bounded capability record consumed inside the guarded tmux close."""

    return {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "state": "prepared",
        "audit_path": str(args.audit_output),
        "replacement_audit_sha256": digest(audit_bytes),
        "old_target": args.old_target,
        "old_pane_id": args.old_pane_id,
        "old_pane_pid": args.old_pane_pid,
        "old_pane_start_ticks": args.old_pane_start_ticks,
        "close_proof_commitment": commitment,
    }


def transition_audit(path: Path, expected: bytes, record: dict[str, object], state: str, *, completed: tuple[str, ...], error: str = "", rollback_failures: tuple[str, ...] = ()) -> tuple[dict[str, object], bytes]:
    current = read_snapshot(path, "private replacement audit")
    if current.data != expected or stat.S_IMODE(current.state.st_mode) != 0o600:
        raise ReplaceError("private replacement audit changed during transaction")
    updated = dict(record)
    updated["state"] = state
    updated["completed_writes"] = list(completed)
    updated.pop("error", None)
    updated.pop("rollback_failures", None)
    if error:
        updated["error"] = error
    if rollback_failures:
        updated["rollback_failures"] = list(rollback_failures)
    data = serialized_audit(updated)
    result = replace_snapshot(current, data, "private replacement audit")
    return updated, result.data


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ReplaceError(f"private replacement audit {label} is not text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ReplaceError(f"private replacement audit {label} is not canonical base64") from exc


def pcodx_audit_binding(args: Args) -> dict[str, object]:
    return {
        "old_queue_sha256": args.old_queue_sha256,
        "old_pcodx_state_sha256": args.old_pcodx_state_sha256,
        "old_pcodx_ledger_sha256": args.old_pcodx_ledger_sha256,
        "old_pcodx_wrapper_sha256": args.old_pcodx_wrapper_sha256,
        "protected_targets_sha256": args.protected_targets_sha256,
        "authority_envelope_file_sha256": args.authority_envelope_file_sha256,
    }


def audit_binding(args: Args) -> dict[str, object]:
    binding: dict[str, object] = {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "root": str(args.root),
        "old_task": args.old_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "parent_target": args.parent_target,
        "old_sha256": args.old_sha256,
        "todo_sha256": args.todo_sha256,
        "children": [{"task": child.task, "sha256": child.sha256} for child in args.children],
        "old_pane": {"id": args.old_pane_id, "pid": args.old_pane_pid, "start_ticks": args.old_pane_start_ticks},
        "old_session_id": args.old_session_id,
        "authority_file": args.authority_file,
        "authority_lines": [args.authority_lines.start, args.authority_lines.end],
        "authority_sha256": args.authority_sha256,
        "authority_envelope_task": args.authority_envelope_task,
        "authority_envelope_sha256": args.authority_envelope_sha256,
        "successor_item_lines": [[item.start, item.end] for item in args.successor_item_lines],
        "protected_targets": list(args.protected_targets),
        "preparer": args.preparer,
        "reviewer": args.reviewer,
    }
    if is_pcodx_replacement(args):
        binding.update(pcodx_audit_binding(args))
    return binding


def read_audit(args: Args) -> tuple[dict[str, object], bytes, tuple[AuditEntry, ...], tuple[Path, ...]]:
    snapshot = read_snapshot(args.audit_output, "private replacement audit")
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or len(snapshot.data) > MAX_AUDIT_BYTES:
        raise ReplaceError("private replacement audit must remain owner-private and bounded")
    try:
        loaded: object = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplaceError(f"private replacement audit is invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ReplaceError("private replacement audit must be one object")
    record = dict(loaded)
    commitment = record.pop("record_sha256", None)
    if commitment != digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()):
        raise ReplaceError("private replacement audit integrity commitment changed")
    allowed = {
        *audit_binding(args),
        "state",
        "close_proof_secret",
        "close_proof_commitment",
        "completed_writes",
        "markdown_membership",
        "files",
        "error",
        "rollback_failures",
    }
    if is_pcodx_replacement(args):
        allowed.add("protected_inventory")
    required = allowed - {"error", "rollback_failures"}
    if not required.issubset(record) or not set(record).issubset(allowed):
        raise ReplaceError("private replacement audit fields are incomplete or unrecognized")
    if any(record.get(key) != value for key, value in audit_binding(args).items()):
        raise ReplaceError("private replacement audit is bound to a different invocation")
    state = record.get("state")
    completed = record.get("completed_writes")
    close_commitment = record.get("close_proof_commitment")
    close_secret = record.get("close_proof_secret")
    if not isinstance(state, str) or not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
        raise ReplaceError("private replacement audit state is malformed")
    if state not in {"prepared", "owner_stopped", "mutating", "proving", "committed", "stop_failed", "rolled_back", "rollback_failed"}:
        raise ReplaceError("private replacement audit lifecycle state is unrecognized")
    if not isinstance(close_commitment, str) or SHA256_RE.fullmatch(close_commitment) is None:
        raise ReplaceError("private replacement audit close commitment is malformed")
    if (
        not isinstance(close_secret, str)
        or SHA256_RE.fullmatch(close_secret) is None
        or digest(close_secret.encode()) != close_commitment
    ):
        raise ReplaceError("private replacement audit close capability is malformed")
    membership_value = record.get("markdown_membership")
    if not isinstance(membership_value, list) or not membership_value or not all(isinstance(item, str) for item in membership_value):
        raise ReplaceError("private replacement audit Markdown membership is malformed")
    membership = tuple(task_path(args.root, item) for item in membership_value)
    if len(set(membership)) != len(membership) or tuple(sorted(membership, key=str)) != membership:
        raise ReplaceError("private replacement audit Markdown membership is not canonical")
    file_values = record.get("files")
    if not isinstance(file_values, list):
        raise ReplaceError("private replacement audit file images are malformed")
    expected_tasks = (args.old_task, *(child.task for child in args.children), "TODO.md", args.successor_task)
    entries: list[AuditEntry] = []
    for index, value in enumerate(file_values):
        if not isinstance(value, dict) or set(value) != {"task", "before", "after", "mode", "gid"}:
            raise ReplaceError("private replacement audit file entry is malformed")
        task = value.get("task")
        mode = value.get("mode")
        gid = value.get("gid")
        if index >= len(expected_tasks) or task != expected_tasks[index]:
            raise ReplaceError("private replacement audit file order changed")
        if not isinstance(mode, int) or mode < 0 or mode & ~0o7777 or not isinstance(gid, int) or gid < 0:
            raise ReplaceError("private replacement audit file ownership metadata is malformed")
        before_value = value.get("before")
        before = None if before_value is None else decoded(before_value, f"{task} before")
        after = decoded(value.get("after"), f"{task} after")
        if (task == args.successor_task) != (before is None):
            raise ReplaceError("private replacement audit successor image is malformed")
        entries.append(AuditEntry(task, before, after, mode, gid))
    if len(entries) != len(expected_tasks):
        raise ReplaceError("private replacement audit file set changed")
    if digest(entries[0].before or b"") != args.old_sha256 or digest(entries[-2].before or b"") != args.todo_sha256:
        raise ReplaceError("private replacement audit before-image digest changed")
    for pin, entry in zip(args.children, entries[1:-2], strict=True):
        if digest(entry.before or b"") != pin.sha256:
            raise ReplaceError(f"private replacement audit child before-image changed: {pin.task}")
    if is_pcodx_replacement(args):
        protected_value = record.get("protected_inventory")
        if not isinstance(protected_value, list) or json_digest(protected_value) != args.protected_targets_sha256:
            raise ReplaceError("private replacement audit protected inventory binding changed")
    return dict(loaded), snapshot.data, tuple(entries), membership


def recovery_plan(
    args: Args,
    entries: tuple[AuditEntry, ...],
    membership: tuple[Path, ...],
    record: dict[str, object],
) -> Plan:
    old_entry = entries[0]
    child_entries = entries[1:-2]
    todo_entry = entries[-2]
    old_path = task_path(args.root, args.old_task)
    todo_path = args.root / "TODO.md"
    snapshots = [read_snapshot(old_path, "recovery old manager")]
    snapshots.extend(read_snapshot(task_path(args.root, pin.task), f"recovery child {pin.task}") for pin in args.children)
    snapshots.append(read_snapshot(todo_path, "recovery TODO"))
    if any(stat.S_IMODE(snapshot.state.st_mode) != entry.mode or snapshot.state.st_gid != entry.gid for snapshot, entry in zip(snapshots, entries[:-1], strict=True)):
        raise ReplaceError("recovery found changed lifecycle file mode or group")
    authority = read_snapshot(task_path(args.root, args.authority_file), "recovery replacement authority")
    envelope = read_snapshot(task_path(args.root, args.authority_envelope_task), "recovery authority envelope")
    authority_items = authority_material(args, authority, envelope)
    if old_entry.before is None or todo_entry.before is None or any(entry.before is None for entry in child_entries):
        raise ReplaceError("private replacement audit lost a required before image")
    old_before = Snapshot(old_path, old_entry.before, snapshots[0].state)
    old_after, successor_after, successor_queue = old_and_successor_text(
        old_entry.before,
        args.root,
        args.old_target,
        args.new_target,
        authority_items,
    )
    child_before: list[Snapshot] = []
    child_after: list[bytes] = []
    child_queues: list[tuple[str, ...]] = []
    for pin, entry, current in zip(args.children, child_entries, snapshots[1:-1], strict=True):
        assert entry.before is not None
        before = Snapshot(task_path(args.root, pin.task), entry.before, current.state)
        before_metadata = metadata(entry.before, args.root, f"recovery child {pin.task}")
        if before_metadata.status == "done" or before_metadata.managerat != args.old_target:
            raise ReplaceError(f"recovery child before image has invalid ownership: {pin.task}")
        try:
            migrated = manager_owner_migration_text(entry.before.decode(), args.old_target, args.new_target, args.root).encode()
        except (UnicodeDecodeError, ValueError, TaskFrontmatterError) as exc:
            raise ReplaceError(f"recovery cannot reconstruct child migration: {pin.task}: {exc}") from exc
        child_before.append(before)
        child_after.append(migrated)
        child_queues.append(before_metadata.pending_task_items)
    successor_path = task_path(args.root, args.successor_task)
    todo_after = todo_replacement(todo_entry.before, args.root, old_path, successor_path, args.old_target, args.new_target)
    canonical_after = (old_after, *child_after, todo_after, successor_after)
    if tuple(entry.after for entry in entries) != canonical_after:
        raise ReplaceError("private replacement audit after images are not the canonical reconstruction")
    old_metadata = metadata(old_entry.before, args.root, "recovery old manager before image")
    if (
        old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status != "long_running"
        or old_metadata.runat != args.old_target
        or old_metadata.managerat != args.parent_target
        or old_metadata.tool != ("pcodx" if is_pcodx_replacement(args) else "codex")
        or not old_metadata.is_manager
        or (not is_pcodx_replacement(args) and old_metadata.session_id.lower() != args.old_session_id)
    ):
        raise ReplaceError("private replacement audit does not describe the exact failed manager")
    if is_pcodx_replacement(args) and json_digest(list(old_metadata.pending_task_items)) != args.old_queue_sha256:
        raise ReplaceError("private replacement audit old manager ordered queue changed")
    protected: list[PaneIdentity] = []
    for value in record.get("protected_inventory", []):
        if not isinstance(value, dict) or set(value) != {"target", "pane_id", "pid", "start_ticks"}:
            raise ReplaceError("private replacement audit protected pane identity is malformed")
        try:
            identity = PaneIdentity(value["target"], value["pane_id"], value["pid"], value["start_ticks"])
        except TypeError as exc:
            raise ReplaceError("private replacement audit protected pane identity has invalid types") from exc
        if (
            canonical_target(identity.target) != identity.target
            or PANE_ID_RE.fullmatch(identity.pane_id) is None
            or not isinstance(identity.pid, int)
            or not isinstance(identity.start_ticks, int)
            or identity.pid <= 0
            or identity.start_ticks <= 0
        ):
            raise ReplaceError("private replacement audit protected pane identity is invalid")
        protected.append(identity)
    return Plan(
        old_before,
        Snapshot(todo_path, todo_entry.before, snapshots[-1].state),
        authority,
        envelope,
        tuple(child_before),
        successor_path,
        old_after,
        tuple(child_after),
        todo_after,
        successor_after,
        successor_queue,
        tuple(child_queues),
        membership,
        tuple(protected),
    )


def current_entry_state(args: Args, entry: AuditEntry) -> tuple[str, Snapshot | None]:
    path = task_path(args.root, entry.task)
    if entry.before is None and not path.exists() and not path.is_symlink():
        return "before", None
    try:
        current = read_snapshot(path, f"current transaction state {entry.task}")
    except ReplaceError:
        return "unknown", None
    if stat.S_IMODE(current.state.st_mode) != entry.mode or current.state.st_gid != entry.gid:
        return "unknown", current
    if entry.before is not None and current.data == entry.before:
        return "before", current
    if current.data == entry.after:
        return "after", current
    return "unknown", current


def rollback_record(args: Args, entries: tuple[AuditEntry, ...], completed: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    failures: list[str] = []
    preserved: list[str] = []
    completed_set = set(completed)
    for entry in reversed(entries):
        state, current = current_entry_state(args, entry)
        try:
            if state == "after" and current is not None:
                if entry.before is None:
                    remove_created(current)
                else:
                    _ = replace_snapshot(current, entry.before, f"rollback {entry.task}")
            elif state == "unknown":
                if entry.task in completed_set or entry.before is None:
                    failures.append(f"{entry.task}: unrecognized current state preserved")
                else:
                    preserved.append(entry.task)
        except Exception as exc:  # rollback must retain every exact failure in durable evidence
            failures.append(f"{entry.task}: {exc}")
    for entry in entries:
        state, _current = current_entry_state(args, entry)
        if state == "before" or entry.task in preserved:
            continue
        if not any(failure.startswith(f"{entry.task}:") for failure in failures):
            failures.append(f"{entry.task}: rollback verification failed")
    return tuple(failures), tuple(preserved)


def after_snapshots(args: Args, entries: tuple[AuditEntry, ...]) -> tuple[Snapshot, ...]:
    snapshots: list[Snapshot] = []
    for entry in entries:
        state, current = current_entry_state(args, entry)
        if state != "after" or current is None:
            raise ReplaceError(f"committed recovery state is incomplete at {entry.task}")
        snapshots.append(current)
    return tuple(snapshots)


def recover_existing(
    args: Args,
    proof_path: Path,
    authority_path: Path,
) -> Recovery:
    record, audit_bytes, entries, membership = read_audit(args)
    plan = recovery_plan(args, entries, membership, record)
    states = tuple(current_entry_state(args, entry)[0] for entry in entries)
    if "unknown" in states:
        completed_value = record.get("completed_writes")
        completed = tuple(completed_value) if isinstance(completed_value, list) else ()
        failures, preserved = rollback_record(args, entries, completed)
        state = "rollback_failed" if failures else "rolled_back"
        record, audit_bytes = transition_audit(
            args.audit_output,
            audit_bytes,
            record,
            state,
            completed=completed,
            error="crash recovery found unrecognized concurrent lifecycle bytes",
            rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
        )
        detail = "; ".join((*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)))
        raise ReplaceError(f"crash recovery preserved unrecognized concurrent state; replacement remains closed: {detail}")
    state = record.get("state")
    commitment = record.get("close_proof_commitment")
    if not isinstance(state, str) or not isinstance(commitment, str):
        raise ReplaceError("private replacement audit state is malformed")
    inventory = pane_inventory()
    old = inventory.get(canonical_target(args.old_target))
    new = inventory.get(canonical_target(args.new_target))
    expected_old = PaneIdentity(canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid, args.old_pane_start_ticks)
    if new is not None:
        raise ReplaceError("successor target is live during crash recovery; launch-before-singular-proof is rejected")
    proof = has_bound_close_proof(proof_path, commitment)
    all_after = all(value == "after" for value in states)
    all_before = all(value == "before" for value in states)
    completed_value = record.get("completed_writes")
    completed = tuple(completed_value) if isinstance(completed_value, list) else ()
    if state == "committed":
        if not all_after or old is not None or not proof:
            raise ReplaceError("committed replacement audit no longer has its exact committed state and close proof")
        snapshots = after_snapshots(args, entries)
        prove_committed(args, plan, snapshots[0], snapshots[1:-2], snapshots[-2], snapshots[-1])
        return Recovery(
            plan,
            record,
            audit_bytes,
            entries,
            True,
            f"recovered committed manager replacement; sole blocked successor ownership remains proved; audit={args.audit_output}",
        )
    if state == "stop_failed":
        raise ReplaceError("prior guarded manager stop failed; use a freshly reviewed audit path after inspecting its durable evidence")
    if state == "prepared" and all_before and old == expected_old and not proof:
        if not authority_path.exists():
            _ = reserve_audit(authority_path, close_authority_record(args, audit_bytes, commitment))
        authority = read_snapshot(authority_path, "bound close authority")
        if authority.data != serialized_audit(close_authority_record(args, audit_bytes, commitment)):
            raise ReplaceError("prepared recovery close-authority record changed")
        return Recovery(plan, record, audit_bytes, entries, False)
    if old is not None or not proof:
        raise ReplaceError("crash recovery cannot prove that the exact old manager was durably closed")
    if all_after:
        snapshots = after_snapshots(args, entries)
        try:
            prove_committed(args, plan, snapshots[0], snapshots[1:-2], snapshots[-2], snapshots[-1])
        except Exception as exc:
            failures, preserved = rollback_record(args, entries, completed)
            rollback_state = "rollback_failed" if failures else "rolled_back"
            record, audit_bytes = transition_audit(
                args.audit_output,
                audit_bytes,
                record,
                rollback_state,
                completed=completed,
                error=f"commit recovery proof failed: {exc}",
                rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
            )
            if failures or preserved:
                raise ReplaceError("commit recovery proof failed and exact rollback could not be completed") from exc
        else:
            record, audit_bytes = transition_audit(
                args.audit_output,
                audit_bytes,
                record,
                "committed",
                completed=tuple(entry.task for entry in entries),
            )
            return Recovery(
                plan,
                record,
                audit_bytes,
                entries,
                True,
                f"recovered committed manager replacement; sole blocked successor ownership proved; audit={args.audit_output}",
            )
    if not all_before:
        failures, preserved = rollback_record(args, entries, completed)
        rollback_state = "rollback_failed" if failures else "rolled_back"
        record, audit_bytes = transition_audit(
            args.audit_output,
            audit_bytes,
            record,
            rollback_state,
            completed=completed,
            error="recovered interrupted lifecycle transaction",
            rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
        )
        if failures or preserved:
            raise ReplaceError("interrupted transaction could not be rolled back to its exact before state")
    if markdown_paths(args.root) != membership:
        raise ReplaceError("Markdown membership changed during crash recovery; exact before state retained")
    restored = recovery_plan(args, entries, membership, record)
    if authoritative_active_target_task_paths(args.root, args.old_target) != (restored.old.path.resolve(),):
        raise ReplaceError("crash recovery cannot prove the restored old manager as sole before-state owner")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("crash recovery found an unexpected successor owner")
    if active_child_task_refs(args.root, restored.old.path, args.old_target) != tuple(child.task for child in args.children):
        raise ReplaceError("crash recovery cannot prove the exact restored child set")
    record, audit_bytes = transition_audit(
        args.audit_output,
        audit_bytes,
        record,
        "owner_stopped",
        completed=(),
    )
    return Recovery(restored, record, audit_bytes, entries, True)


def validate_panes_before_close(args: Args) -> None:
    inventory = pane_inventory()
    validate_live_bindings(args, inventory)


def require_preclose_eligibility(args: Args, plan: Plan) -> None:
    """Recheck every prepared lifecycle and authority binding before pane input."""

    if markdown_paths(args.root) != plan.initial_markdown_paths:
        raise ReplaceError("Markdown membership changed before guarded manager close")
    if plan.successor_path.exists() or plan.successor_path.is_symlink():
        raise ReplaceError("successor appeared before guarded manager close")
    require_snapshot(plan.old, "pre-close old manager")
    require_snapshot(plan.todo, "pre-close TODO")
    require_snapshot(plan.authority, "pre-close replacement authority")
    require_snapshot(plan.authority_envelope, "pre-close replacement authority envelope")
    for child in plan.children:
        require_snapshot(child, f"pre-close active child {child.path.name}")
    if authoritative_active_target_task_paths(args.root, args.old_target) != (plan.old.path.resolve(),):
        raise ReplaceError("old target ownership changed before guarded manager close")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("successor target ownership appeared before guarded manager close")
    if active_child_task_refs(args.root, plan.old.path, args.old_target) != tuple(child.task for child in args.children):
        raise ReplaceError("active child set changed before guarded manager close")


def stop_old_manager(
    args: Args,
    plan: Plan,
    proof_path: Path,
    authority_path: Path,
    secret: str,
    commitment: str,
) -> None:
    protected_before: tuple[PaneIdentity, ...] = ()
    require_preclose_eligibility(args, plan)
    if is_pcodx_replacement(args):
        before_inventory = pane_inventory()
        validate_live_bindings(args, before_inventory)
        protected_before = tuple(
            identity
            for identity in protected_inventory(args, before_inventory)
            if identity.target != canonical_target(args.old_target)
        )
    require_preclose_eligibility(args, plan)

    def pre_input_check() -> None:
        require_preclose_eligibility(args, plan)
        validate_live_bindings(args, pane_inventory())

    session_id = stop(
        StopArgs(
            target=args.old_target,
            wait_s=10.0,
            lines=2000,
            dry_run=False,
            allow_self=False,
            root=args.root,
            task_file=args.old_task if is_pcodx_replacement(args) else "",
            no_feedback=True,
            bound_symbolic_target=args.old_target,
            bound_pane_id=args.old_pane_id,
            bound_pane_pid=args.old_pane_pid,
            bound_pane_start_ticks=args.old_pane_start_ticks,
            bound_expected_session_id=args.old_session_id,
            bound_close_proof_path=str(proof_path),
            bound_close_audit_path=str(authority_path),
            bound_close_proof_secret=secret,
            bound_close_proof_commitment=commitment,
            human_close_authorization_source=args.authority_file if is_pcodx_replacement(args) else "",
            human_close_authorization_sha256=args.authority_sha256 if is_pcodx_replacement(args) else "",
            human_close_authorized_target=args.old_target if is_pcodx_replacement(args) else "",
            bound_pre_input_check=pre_input_check if is_pcodx_replacement(args) else None,
        )
    )
    if session_id.lower() != args.old_session_id:
        raise ReplaceError(f"stopped manager session id mismatch: expected {args.old_session_id}, found {session_id or '<missing>'}")
    if not has_bound_close_proof(proof_path, commitment):
        raise ReplaceError("old manager close did not produce its bound durable proof")
    inventory = pane_inventory()
    if canonical_target(args.old_target) in inventory:
        raise ReplaceError("old manager target remains live after guarded close")
    if canonical_target(args.new_target) in inventory:
        raise ReplaceError("successor target launched before singular ownership proof")
    if is_pcodx_replacement(args) and tuple(inventory.get(identity.target) for identity in protected_before) != protected_before:
        raise ReplaceError("non-replaced protected pane/process inventory changed during close")


def prove_committed(args: Args, plan: Plan, old_after: Snapshot, child_after: tuple[Snapshot, ...], todo_after: Snapshot, successor: Snapshot) -> None:
    require_snapshot(plan.authority, "replacement authority")
    require_snapshot(plan.authority_envelope, "replacement authority envelope")
    require_snapshot(old_after, "committed old manager")
    require_snapshot(todo_after, "committed TODO")
    require_snapshot(successor, "committed successor")
    for snapshot in child_after:
        require_snapshot(snapshot, f"committed child {snapshot.path.name}")
    if authoritative_active_target_task_paths(args.root, args.old_target):
        raise ReplaceError("old target retains an active owner after replacement")
    if authoritative_active_target_task_paths(args.root, args.new_target) != (plan.successor_path.resolve(),):
        raise ReplaceError("new target does not have exactly one successor owner")
    if active_child_task_refs(args.root, plan.successor_path, args.new_target) != tuple(child.task for child in args.children):
        raise ReplaceError("successor does not own the exact migrated active-child set")
    if active_child_task_refs(args.root, plan.old.path, args.old_target):
        raise ReplaceError("old manager retains an active child after replacement")
    for snapshot, queue in zip(child_after, plan.child_queues, strict=True):
        if metadata(snapshot.data, args.root, snapshot.path.name).pending_task_items != queue:
            raise ReplaceError(f"active child queue changed during migration: {snapshot.path.name}")
    successor_metadata = metadata(successor.data, args.root, "successor task")
    if successor_metadata.pending_task_items != plan.successor_queue or successor_metadata.status != "blocked":
        raise ReplaceError("successor queue or launch gate changed")
    inventory = pane_inventory()
    if canonical_target(args.old_target) in inventory or canonical_target(args.new_target) in inventory:
        raise ReplaceError("old or successor pane is live at the singular ownership proof boundary")
    remaining_protected = tuple(
        identity for identity in plan.protected_identities if identity.target != canonical_target(args.old_target)
    )
    if tuple(inventory.get(identity.target) for identity in remaining_protected) != remaining_protected:
        raise ReplaceError("protected pane/process inventory changed before singular ownership proof")
    expected_membership = tuple(sorted((*plan.initial_markdown_paths, plan.successor_path.resolve(strict=False)), key=str))
    if markdown_paths(args.root) != expected_membership:
        raise ReplaceError("Markdown membership changed before singular ownership proof")


def replace_manager(args: Args) -> str:
    validate_targets(args)
    if not args.root.is_dir():
        raise ReplaceError("work-log root is unavailable")
    initial_paths = markdown_paths(args.root)
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    authority_source_path = task_path(args.root, args.authority_file)
    authority_envelope_path = task_path(args.root, args.authority_envelope_task)
    close_authority_path = args.audit_output.with_name(f".{args.audit_output.name}.close-authority")
    proof_path = close_authority_path.with_name(f".{close_authority_path.name}.owner-stopped")
    lock_paths = tuple(
        sorted(
            {
                *initial_paths,
                old_path,
                successor_path,
                authority_source_path,
                authority_envelope_path,
                args.audit_output,
                close_authority_path,
                proof_path,
            },
            key=str,
        )
    )
    with ExitStack() as locks:
        locks.enter_context(root_membership_lock(args.root))
        for target in sorted({canonical_target(args.old_target), canonical_target(args.new_target)}):
            locks.enter_context(task_target_lock(args.root, target))
        for path in lock_paths:
            locks.enter_context(task_file_lock(path))
        if markdown_paths(args.root) != initial_paths:
            raise ReplaceError("Markdown membership changed while replacement locks were acquired")
        owner_stopped = False
        if args.audit_output.exists() or args.audit_output.is_symlink():
            recovery = recover_existing(args, proof_path, close_authority_path)
            if recovery.result:
                return recovery.result
            plan = recovery.plan
            record = recovery.record
            audit_bytes = recovery.audit_bytes
            entries = recovery.entries
            owner_stopped = recovery.owner_stopped
            secret = record.get("close_proof_secret")
            commitment = record.get("close_proof_commitment")
            if not isinstance(secret, str) or not isinstance(commitment, str):
                raise ReplaceError("private replacement audit lost its close capability")
        else:
            if close_authority_path.exists() or proof_path.exists():
                raise ReplaceError("bound close authority or proof exists without its private replacement audit")
            validate_panes_before_close(args)
            plan = prepare(args, initial_paths)
            validate_panes_before_close(args)
            secret = os.urandom(32).hex()
            commitment = digest(secret.encode())
            record = audit_record(args, plan, secret, commitment)
            audit_bytes = reserve_audit(args.audit_output, record)
            _ = reserve_audit(close_authority_path, close_authority_record(args, audit_bytes, commitment))
            record, audit_bytes, entries, membership = read_audit(args)
            if membership != plan.initial_markdown_paths:
                raise ReplaceError("private replacement audit did not preserve prepared Markdown membership")
        if not owner_stopped:
            validate_panes_before_close(args)
            try:
                stop_old_manager(args, plan, proof_path, close_authority_path, secret, commitment)
            except Exception as exc:
                try:
                    record, audit_bytes = transition_audit(
                        args.audit_output,
                        audit_bytes,
                        record,
                        "stop_failed",
                        completed=(),
                        error=str(exc),
                    )
                except Exception as audit_exc:
                    raise ReplaceError(f"manager stop failed and audit finalization failed: {exc}; audit: {audit_exc}") from exc
                raise ReplaceError(f"manager stop failed before lifecycle mutation: {exc}") from exc
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "owner_stopped", completed=())
        completed: list[str] = []
        try:
            require_snapshot(plan.authority, "replacement authority")
            require_snapshot(plan.authority_envelope, "replacement authority envelope")
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=())
            # 🧑 "The agent failed. They did not run the experiment. Replace them. The replacement agent should finish the task."
            updated_old = replace_snapshot(plan.old, plan.old_after, "old manager")
            completed.append(args.old_task)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            updated_children: list[Snapshot] = []
            for pin, before, after_data in zip(args.children, plan.children, plan.child_after, strict=True):
                after = replace_snapshot(before, after_data, f"active child {pin.task}")
                updated_children.append(after)
                completed.append(pin.task)
                record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            updated_todo = replace_snapshot(plan.todo, plan.todo_after, "TODO")
            completed.append("TODO.md")
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            successor = create_snapshot(
                plan.successor_path,
                plan.successor_data,
                stat.S_IMODE(plan.old.state.st_mode),
                plan.old.state.st_gid,
            )
            completed.append(args.successor_task)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "proving", completed=tuple(completed))
            prove_committed(args, plan, updated_old, tuple(updated_children), updated_todo, successor)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "committed", completed=tuple(completed))
        except Exception as exc:
            failures, preserved = rollback_record(args, entries, tuple(completed))
            state = "rollback_failed" if failures else "rolled_back"
            try:
                record, audit_bytes = transition_audit(
                    args.audit_output,
                    audit_bytes,
                    record,
                    state,
                    completed=tuple(completed),
                    error=str(exc),
                    rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
                )
            except Exception as audit_exc:
                raise ReplaceError(f"replacement failed; rollback failures={failures or 'none'}; audit finalization failed: {audit_exc}") from exc
            if failures:
                raise ReplaceError(f"replacement failed and rollback was incomplete: {'; '.join(failures)}") from exc
            if preserved:
                raise ReplaceError(
                    f"replacement failed; all owned lifecycle writes rolled back and concurrent bytes were preserved: {', '.join(preserved)}"
                ) from exc
            raise ReplaceError(f"replacement failed; all lifecycle writes rolled back: {exc}") from exc
        return (
            f"closed {args.old_task}, migrated {len(args.children)} active child task(s), and created blocked unlaunched "
            f"{args.successor_task}; sole ownership at {args.new_target} proved; launch remains a separate supported operation; audit={args.audit_output}"
        )


def main(argv: list[str] | None = None) -> int:
    try:
        print(replace_manager(parse_args(sys.argv[1:] if argv is None else argv)))
    except (OSError, ReplaceError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_manager_replace.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
