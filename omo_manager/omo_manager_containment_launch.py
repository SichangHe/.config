#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Launch one fresh manager from an exact completed containment sentinel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_manager_rotate import CODEX_PACKAGE
from omo_manager.omo_manager_rotate import SUCCESS_STATUSES
from omo_manager.omo_manager_rotate import TERMINAL_FAILURE_STATUSES
from omo_manager.omo_manager_rotate import PaneIdentity
from omo_manager.omo_manager_rotate import RotationError
from omo_manager.omo_manager_rotate import is_codex_launch_argv
from omo_manager.omo_manager_rotate import resolve_exact_pane
from omo_manager.omo_manager_rotate import status_classification
from omo_manager.omo_manager_rotation_contain import CONTAINMENT_VERSION
from omo_manager.omo_manager_rotation_contain import Args as ContainmentArgs
from omo_manager.omo_manager_rotation_contain import AuditBinding
from omo_manager.omo_manager_rotation_contain import ContainmentError
from omo_manager.omo_manager_rotation_contain import FileEvidence
from omo_manager.omo_manager_rotation_contain import ProcessIdentity
from omo_manager.omo_manager_rotation_contain import SessionEvidence
from omo_manager.omo_manager_rotation_contain import TaskBinding
from omo_manager.omo_manager_rotation_contain import WatcherProof
from omo_manager.omo_manager_rotation_contain import audit_binding
from omo_manager.omo_manager_rotation_contain import current_command
from omo_manager.omo_manager_rotation_contain import json_no_duplicates
from omo_manager.omo_manager_rotation_contain import manager_rotation_lock
from omo_manager.omo_manager_rotation_contain import parse_datetime
from omo_manager.omo_manager_rotation_contain import process_argv
from omo_manager.omo_manager_rotation_contain import process_snapshot
from omo_manager.omo_manager_rotation_contain import process_stat
from omo_manager.omo_manager_rotation_contain import process_tree
from omo_manager.omo_manager_rotation_contain import queue_sha256
from omo_manager.omo_manager_rotation_contain import read_regular_file
from omo_manager.omo_manager_rotation_contain import receipt_bytes
from omo_manager.omo_manager_rotation_contain import replace_private_exact
from omo_manager.omo_manager_rotation_contain import require_private_parent
from omo_manager.omo_manager_rotation_contain import session_evidence
from omo_manager.omo_manager_rotation_contain import sha256
from omo_manager.omo_manager_rotation_contain import stable_process_identity
from omo_manager.omo_manager_rotation_contain import task_binding
from omo_manager.omo_manager_rotation_contain import watcher_proof
from omo_manager.omo_manager_rotation_contain import write_private_exclusive
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock

BRIDGE_VERSION = "omo-manager-containment-launch/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
UUID_ANY_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?$")
PANE_ID_RE = re.compile(r"^%[1-9][0-9]*$")
WINDOW_ID_RE = re.compile(r"^@[1-9][0-9]*$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
FILE_KEYS = {"path", "device", "inode", "size", "mtime_ns", "mode", "uid", "sha256"}
PROCESS_KEYS = {"pid", "ppid", "state", "process_group", "session", "tty", "start_ticks", "argv_sha256"}
PANE_KEYS = {"canonical_target", "pane_id", "window_id", "pane_pid", "working_directory"}
SESSION_KEYS = {"file", "session_id", "started_at", "cwd", "launch_prompt_sha256"}
TASK_KEYS = {
    "file",
    "status",
    "blocker",
    "runat",
    "managerat",
    "pending_items",
    "queue_sha256",
    "todo",
    "todo_section",
    "todo_row",
    "todo_row_sha256",
}
WATCHER_KEYS = {"root", "declared_root", "lock_path", "lock_device", "lock_inode", "pid", "start_ticks", "argv_sha256"}
AUDIT_KEYS = {
    "file",
    "recorded_at",
    "target",
    "root",
    "state_dir",
    "prompt",
    "watcher_failure_log",
    "old_pane",
    "old_launch_pid",
    "old_launch_argv_sha256",
    "model",
    "reasoning_effort",
}
ORIGINAL_AUDIT_KEYS = {
    "error",
    "fresh_command",
    "launch",
    "outcome",
    "pane",
    "prior_pane_output",
    "prompt_path",
    "recorded_at",
    "root",
    "status",
    "target",
}
ORIGINAL_LAUNCH_KEYS = {"launch_argv", "launch_pid", "model", "reasoning_effort", "source"}
CONTAINMENT_RECEIPT_KEYS = {
    "version",
    "operation",
    "state",
    "claim",
    "recorded_at",
    "root",
    "target",
    "protected_targets",
    "protected_targets_sha256",
    "failed_audit",
    "old_session",
    "successor_pane",
    "successor_process",
    "successor_process_tree",
    "successor_session",
    "successor_session_final_file",
    "task",
    "watcher",
    "contained_process",
    "dispatch_state",
    "error",
}


class BridgeError(RuntimeError):
    """A containment-to-successor safety check or mutation failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    target: str
    containment_receipt: Path
    containment_receipt_sha256: str
    session_root: Path
    expected_contained_pane_id: str
    expected_contained_window_id: str
    expected_contained_pid: int
    expected_contained_start_ticks: int
    expected_contained_argv_sha256: str
    expected_failed_successor_command: str
    expected_task_sha256: str
    expected_blocker: str
    expected_manager_target: str
    expected_pending_items: tuple[str, ...]
    expected_watcher_pid: int
    expected_watcher_start_ticks: int
    protected_targets: tuple[str, ...]
    ownership_receipt: Path
    startup_timeout_s: float
    poll_interval_s: float
    dry_run: bool


class ParsedArgs(argparse.Namespace):
    launch_contained_successor: bool = False
    root: Path = Path(".")
    task_file: Path = Path(".")
    target: str = ""
    containment_receipt: Path = Path(".")
    containment_receipt_sha256: str = ""
    session_root: Path = Path(".")
    expected_contained_pane_id: str = ""
    expected_contained_window_id: str = ""
    expected_contained_pid: int = 0
    expected_contained_start_ticks: int = 0
    expected_contained_argv_sha256: str = ""
    expected_failed_successor_command: str = ""
    expected_task_sha256: str = ""
    expected_blocker: str = ""
    expected_manager_target: str = ""
    expected_pending_item: list[str] = []
    expect_empty_queue: bool = False
    expected_watcher_pid: int = 0
    expected_watcher_start_ticks: int = 0
    protected_target: list[str] = []
    ownership_receipt: Path = Path(".")
    startup_timeout_s: float = 60.0
    poll_interval_s: float = 0.25
    dry_run: bool = False


@dataclass(frozen=True)
class ContainmentReceipt:
    file: FileEvidence
    recorded_at: str
    audit: AuditBinding
    old_session: SessionEvidence
    failed_successor_pane: PaneIdentity
    failed_successor_process: ProcessIdentity
    failed_successor_tree: tuple[ProcessIdentity, ...]
    failed_successor_session: SessionEvidence
    task: TaskBinding
    watcher: WatcherProof
    contained_process: ProcessIdentity
    protected_targets: tuple[str, ...]
    launch_argv0: Path
    launch_executable: FileEvidence
    containment_args: ContainmentArgs


@dataclass(frozen=True)
class ProtectedBinding:
    target: str
    pane: PaneIdentity
    command: str
    process: ProcessIdentity


@dataclass(frozen=True)
class ContainedLive:
    pane: PaneIdentity
    sentinel: ProcessIdentity
    task: TaskBinding
    watcher: WatcherProof
    protected: tuple[ProtectedBinding, ...]


@dataclass(frozen=True)
class SessionHandle:
    session: SessionEvidence
    holder_pid: int
    holder_start_ticks: int
    descriptor: int


@dataclass(frozen=True)
class ActiveSuccessor:
    pane: PaneIdentity
    process: ProcessIdentity
    process_tree: tuple[ProcessIdentity, ...]
    session_handle: SessionHandle
    status: str
    environment_sha256: str
    task: TaskBinding
    watcher: WatcherProof
    protected: tuple[ProtectedBinding, ...]


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        epilog=(
            "This helper consumes one exact complete containment receipt, revalidates its inert sentinel and every "
            "task/watcher/ownership binding, and replaces only that sentinel with a fresh manager. --dry-run is read-only."
        ),
    )
    _ = parser.add_argument("--launch-contained-successor", action="store_true", help="Required explicit bridge mode.")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-file", type=Path, required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--containment-receipt", type=Path, required=True)
    _ = parser.add_argument("--containment-receipt-sha256", required=True)
    _ = parser.add_argument("--session-root", type=Path, required=True)
    _ = parser.add_argument("--expected-contained-pane-id", required=True)
    _ = parser.add_argument("--expected-contained-window-id", required=True)
    _ = parser.add_argument("--expected-contained-pid", type=int, required=True)
    _ = parser.add_argument("--expected-contained-start-ticks", type=int, required=True)
    _ = parser.add_argument("--expected-contained-argv-sha256", required=True)
    _ = parser.add_argument("--expected-failed-successor-command", required=True)
    _ = parser.add_argument("--expected-task-sha256", required=True)
    _ = parser.add_argument("--expected-blocker", required=True)
    _ = parser.add_argument("--expected-manager-target", required=True)
    _ = parser.add_argument("--expected-pending-item", action="append", default=[])
    _ = parser.add_argument("--expect-empty-queue", action="store_true")
    _ = parser.add_argument("--expected-watcher-pid", type=int, required=True)
    _ = parser.add_argument("--expected-watcher-start-ticks", type=int, required=True)
    _ = parser.add_argument("--protected-target", action="append", default=[])
    _ = parser.add_argument("--ownership-receipt", type=Path, required=True)
    _ = parser.add_argument("--startup-timeout-s", type=float, default=60.0)
    _ = parser.add_argument("--poll-interval-s", type=float, default=0.25)
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.launch_contained_successor:
        parser.error("--launch-contained-successor is required.")
    hashes = (
        parsed.containment_receipt_sha256,
        parsed.expected_contained_argv_sha256,
        parsed.expected_task_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        parser.error("all SHA-256 assertions must be lowercase 64-character hexadecimal values.")
    targets = (parsed.target, parsed.expected_manager_target, *parsed.protected_target)
    if any(TARGET_RE.fullmatch(value) is None for value in targets):
        parser.error("target assertions must be exact SESSION:WINDOW[.PANE] values.")
    if parsed.target.partition(":")[0].startswith("h"):
        parser.error("the containment bridge cannot modify a human-owned h* target.")
    duplicate_protected = any(
        same_tmux_target(target, other)
        for index, target in enumerate(parsed.protected_target)
        for other in parsed.protected_target[index + 1 :]
    )
    if not parsed.protected_target or duplicate_protected:
        parser.error("repeat --protected-target for one nonempty duplicate-free protected set.")
    if any(target.partition(":")[0].startswith("h") for target in parsed.protected_target):
        parser.error("protected targets cannot be human-owned h* sessions.")
    if any(same_tmux_target(parsed.target, target) for target in parsed.protected_target):
        parser.error("the bridge target cannot appear in the protected-target set.")
    if bool(parsed.expected_pending_item) == parsed.expect_empty_queue:
        parser.error("use either repeated --expected-pending-item or --expect-empty-queue, exactly once.")
    if any(not item or any(character in item for character in "\n\r\0") for item in parsed.expected_pending_item):
        parser.error("expected pending items must be nonempty one-line values.")
    if PANE_ID_RE.fullmatch(parsed.expected_contained_pane_id) is None:
        parser.error("--expected-contained-pane-id must be a numeric tmux pane id such as %15.")
    if WINDOW_ID_RE.fullmatch(parsed.expected_contained_window_id) is None:
        parser.error("--expected-contained-window-id must be a numeric tmux window id such as @15.")
    if min(
        parsed.expected_contained_pid,
        parsed.expected_contained_start_ticks,
        parsed.expected_watcher_pid,
        parsed.expected_watcher_start_ticks,
    ) <= 1:
        parser.error("expected process ids and start ticks must be greater than one.")
    if COMMAND_RE.fullmatch(parsed.expected_failed_successor_command) is None:
        parser.error("the failed-successor command assertion contains unsupported characters.")
    if not math.isfinite(parsed.startup_timeout_s) or parsed.startup_timeout_s <= 0:
        parser.error("--startup-timeout-s must be finite and positive.")
    if not math.isfinite(parsed.poll_interval_s) or parsed.poll_interval_s <= 0 or parsed.poll_interval_s > parsed.startup_timeout_s:
        parser.error("--poll-interval-s must be finite, positive, and no greater than the startup timeout.")
    root = parsed.root.expanduser().resolve(strict=True)
    task_file = parsed.task_file.expanduser()
    if not task_file.is_absolute():
        task_file = root / task_file
    task_file = task_file.resolve(strict=True)
    if root not in task_file.parents or task_file == root / "TODO.md":
        parser.error("--task-file must resolve beneath --root and differ from TODO.md.")
    absolute_paths = (parsed.containment_receipt, parsed.session_root, parsed.ownership_receipt)
    if any(not path.expanduser().is_absolute() for path in absolute_paths):
        parser.error("containment, session-root, and ownership-receipt paths must be absolute.")
    session_root = parsed.session_root.expanduser().resolve(strict=True)
    if not session_root.is_dir():
        parser.error("--session-root must resolve to a directory.")
    containment_receipt = parsed.containment_receipt.expanduser()
    ownership_receipt = parsed.ownership_receipt.expanduser()
    if containment_receipt == ownership_receipt:
        parser.error("input containment and output ownership receipts must differ.")
    expected_output = containment_receipt.with_name(f"{containment_receipt.stem}-successor-ownership.receipt")
    if ownership_receipt != expected_output:
        parser.error(f"--ownership-receipt must use the one-shot path {expected_output}.")
    return Args(
        root,
        task_file,
        parsed.target,
        containment_receipt,
        parsed.containment_receipt_sha256,
        session_root,
        parsed.expected_contained_pane_id,
        parsed.expected_contained_window_id,
        parsed.expected_contained_pid,
        parsed.expected_contained_start_ticks,
        parsed.expected_contained_argv_sha256,
        parsed.expected_failed_successor_command,
        parsed.expected_task_sha256,
        parsed.expected_blocker,
        parsed.expected_manager_target,
        tuple(parsed.expected_pending_item),
        parsed.expected_watcher_pid,
        parsed.expected_watcher_start_ticks,
        tuple(parsed.protected_target),
        ownership_receipt,
        parsed.startup_timeout_s,
        parsed.poll_interval_s,
        parsed.dry_run,
    )


def exact_record(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BridgeError(f"{label} has an unsupported schema.")
    record = cast(dict[str, object], value)
    if set(record) != keys:
        raise BridgeError(f"{label} has an unsupported schema.")
    return record


def exact_string(record: Mapping[str, object], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise BridgeError(f"{label} field {key!r} must be a string.")
    return value


def exact_integer(record: Mapping[str, object], key: str, label: str) -> int:
    value = record.get(key)
    if type(value) is not int:
        raise BridgeError(f"{label} field {key!r} must be an integer.")
    return value


def file_evidence_record(value: object, label: str) -> FileEvidence:
    record = exact_record(value, FILE_KEYS, label)
    path = exact_string(record, "path", label)
    digest = exact_string(record, "sha256", label)
    if not Path(path).is_absolute() or SHA256_RE.fullmatch(digest) is None:
        raise BridgeError(f"{label} path or SHA-256 is invalid.")
    integers = tuple(exact_integer(record, key, label) for key in ("device", "inode", "size", "mtime_ns", "mode", "uid"))
    if min(integers[:4]) < 0 or integers[4] < 0 or integers[4] > 0o7777 or integers[5] < 0:
        raise BridgeError(f"{label} file identity is invalid.")
    return FileEvidence(path, integers[0], integers[1], integers[2], integers[3], integers[4], integers[5], digest)


def process_record(value: object, label: str) -> ProcessIdentity:
    record = exact_record(value, PROCESS_KEYS, label)
    state = exact_string(record, "state", label)
    digest = exact_string(record, "argv_sha256", label)
    integers = tuple(
        exact_integer(record, key, label)
        for key in ("pid", "ppid", "process_group", "session", "tty", "start_ticks")
    )
    pid, ppid, process_group, session, tty, start_ticks = integers
    if (
        len(state) != 1
        or SHA256_RE.fullmatch(digest) is None
        or pid <= 1
        or ppid < 0
        or process_group <= 1
        or session <= 1
        or tty < 0
        or start_ticks <= 1
    ):
        raise BridgeError(f"{label} process identity is invalid.")
    return ProcessIdentity(pid, ppid, state, process_group, session, tty, start_ticks, digest)


def pane_record(value: object, label: str) -> PaneIdentity:
    record = exact_record(value, PANE_KEYS, label)
    target = exact_string(record, "canonical_target", label)
    pane_id = exact_string(record, "pane_id", label)
    window_id = exact_string(record, "window_id", label)
    cwd = Path(exact_string(record, "working_directory", label))
    pid = exact_integer(record, "pane_pid", label)
    if TARGET_RE.fullmatch(target) is None or PANE_ID_RE.fullmatch(pane_id) is None or WINDOW_ID_RE.fullmatch(window_id) is None:
        raise BridgeError(f"{label} tmux identity is invalid.")
    if not cwd.is_absolute() or pid <= 1:
        raise BridgeError(f"{label} process or working-directory identity is invalid.")
    return PaneIdentity(target, pane_id, window_id, pid, cwd)


def session_record(value: object, label: str) -> SessionEvidence:
    record = exact_record(value, SESSION_KEYS, label)
    session_id = exact_string(record, "session_id", label).casefold()
    started_at = exact_string(record, "started_at", label)
    cwd = exact_string(record, "cwd", label)
    prompt_digest = exact_string(record, "launch_prompt_sha256", label)
    if UUID_RE.fullmatch(session_id) is None or not Path(cwd).is_absolute() or SHA256_RE.fullmatch(prompt_digest) is None:
        raise BridgeError(f"{label} metadata is invalid.")
    _ = parse_datetime(started_at, f"{label} timestamp")
    return SessionEvidence(file_evidence_record(record["file"], f"{label} file"), session_id, started_at, cwd, prompt_digest)


def task_record(value: object, label: str) -> TaskBinding:
    record = exact_record(value, TASK_KEYS, label)
    pending_raw = record.get("pending_items")
    if not isinstance(pending_raw, list):
        raise BridgeError(f"{label} pending-items list is invalid.")
    pending_values = cast(list[object], pending_raw)
    if not all(isinstance(item, str) for item in pending_values):
        raise BridgeError(f"{label} pending-items list is invalid.")
    pending = tuple(cast(str, item) for item in pending_values)
    queue_digest = exact_string(record, "queue_sha256", label)
    todo_row_digest = exact_string(record, "todo_row_sha256", label)
    if SHA256_RE.fullmatch(queue_digest) is None or SHA256_RE.fullmatch(todo_row_digest) is None:
        raise BridgeError(f"{label} queue or TODO-row digest is invalid.")
    return TaskBinding(
        file_evidence_record(record["file"], f"{label} file"),
        exact_string(record, "status", label),
        exact_string(record, "blocker", label),
        exact_string(record, "runat", label),
        exact_string(record, "managerat", label),
        pending,
        queue_digest,
        file_evidence_record(record["todo"], f"{label} TODO"),
        exact_string(record, "todo_section", label),
        exact_string(record, "todo_row", label),
        todo_row_digest,
    )


def watcher_record(value: object, label: str) -> WatcherProof:
    record = exact_record(value, WATCHER_KEYS, label)
    digest = exact_string(record, "argv_sha256", label)
    integers = tuple(exact_integer(record, key, label) for key in ("lock_device", "lock_inode", "pid", "start_ticks"))
    if SHA256_RE.fullmatch(digest) is None or min(integers) <= 0:
        raise BridgeError(f"{label} identity is invalid.")
    paths = tuple(exact_string(record, key, label) for key in ("root", "declared_root", "lock_path"))
    if not all(Path(path).is_absolute() for path in paths):
        raise BridgeError(f"{label} paths must be absolute.")
    return WatcherProof(paths[0], paths[1], paths[2], integers[0], integers[1], integers[2], integers[3], digest)


def audit_record(value: object, label: str) -> AuditBinding:
    record = exact_record(value, AUDIT_KEYS, label)
    recorded_at = exact_string(record, "recorded_at", label)
    _ = parse_datetime(recorded_at, f"{label} timestamp")
    digest = exact_string(record, "old_launch_argv_sha256", label)
    old_launch_pid = exact_integer(record, "old_launch_pid", label)
    if SHA256_RE.fullmatch(digest) is None or old_launch_pid <= 1:
        raise BridgeError(f"{label} launch identity is invalid.")
    return AuditBinding(
        file_evidence_record(record["file"], f"{label} file"),
        recorded_at,
        exact_string(record, "target", label),
        exact_string(record, "root", label),
        exact_string(record, "state_dir", label),
        file_evidence_record(record["prompt"], f"{label} prompt"),
        file_evidence_record(record["watcher_failure_log"], f"{label} watcher log"),
        pane_record(record["old_pane"], f"{label} old pane"),
        old_launch_pid,
        digest,
        exact_string(record, "model", label),
        exact_string(record, "reasoning_effort", label),
    )


def executable_evidence(path: Path) -> FileEvidence:
    """Read one stable executable without requiring its owner to invoke the bridge."""

    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            raise BridgeError("original failed-audit bunx target is not an executable regular file.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise BridgeError("original failed-audit bunx target changed while it was read.")
    finally:
        os.close(descriptor)
    return FileEvidence(
        str(path),
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        digest.hexdigest(),
    )


def original_launch_executable(audit: AuditBinding) -> tuple[Path, FileEvidence]:
    raw, evidence = read_regular_file(Path(audit.file.path), "original failed audit", audit.file.sha256)
    if evidence != audit.file:
        raise BridgeError("original failed-audit file identity changed after containment.")
    record = json_no_duplicates(raw, "original failed audit")
    if set(record) != ORIGINAL_AUDIT_KEYS:
        raise BridgeError("original failed audit has an unsupported schema.")
    launch = exact_record(record.get("launch"), ORIGINAL_LAUNCH_KEYS, "original failed audit launch")
    argv_raw = launch.get("launch_argv")
    if not isinstance(argv_raw, list):
        raise BridgeError("original failed audit launch argv is invalid.")
    argv_values = cast(list[object], argv_raw)
    if not argv_values or not all(isinstance(value, str) for value in argv_values):
        raise BridgeError("original failed audit launch argv is invalid.")
    argv = tuple(cast(str, value) for value in argv_values)
    if sha256("\0".join(argv).encode()) != audit.old_launch_argv_sha256:
        raise BridgeError("original failed audit launch argv does not match the containment receipt.")
    executable = Path(argv[0])
    if not executable.is_absolute() or executable.name != "bunx":
        raise BridgeError("original failed audit does not bind an absolute bunx executable.")
    try:
        resolved_executable = executable.resolve(strict=True)
    except OSError as exc:
        raise BridgeError("original failed-audit bunx executable is unavailable.") from exc
    if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
        raise BridgeError("original failed-audit bunx executable is not executable.")
    return executable, executable_evidence(resolved_executable)


def task_binding_matches(current: TaskBinding, recorded: TaskBinding) -> bool:
    return (
        current.file.path == recorded.file.path
        and current.file.device == recorded.file.device
        and current.file.size == recorded.file.size
        and current.file.mode == recorded.file.mode
        and current.file.uid == recorded.file.uid
        and current.file.sha256 == recorded.file.sha256
        and current.status == recorded.status
        and current.blocker == recorded.blocker
        and current.runat == recorded.runat
        and current.managerat == recorded.managerat
        and current.pending_items == recorded.pending_items
        and current.queue_sha256 == recorded.queue_sha256
        and current.todo.path == recorded.todo.path
        and current.todo.device == recorded.todo.device
        and current.todo.mode == recorded.todo.mode
        and current.todo.uid == recorded.todo.uid
        and current.todo_section == recorded.todo_section
        and current.todo_row == recorded.todo_row
        and current.todo_row_sha256 == recorded.todo_row_sha256
    )


def containment_receipt(args: Args) -> ContainmentReceipt:
    expected_output = args.containment_receipt.with_name(
        f"{args.containment_receipt.stem}-successor-ownership.receipt"
    )
    if args.ownership_receipt != expected_output:
        raise BridgeError(f"ownership receipt must use the one-shot path {expected_output}.")
    require_private_parent(args.containment_receipt, "containment receipt")
    raw, receipt_file = read_regular_file(
        args.containment_receipt,
        "containment receipt",
        args.containment_receipt_sha256,
    )
    if receipt_file.mode != 0o600 or receipt_file.uid != os.getuid():
        raise BridgeError("containment receipt must be owner-private and owned by the current user.")
    receipt = json_no_duplicates(raw, "containment receipt")
    if set(receipt) != CONTAINMENT_RECEIPT_KEYS:
        raise BridgeError("containment receipt has an unsupported schema.")
    if (
        receipt.get("version") != CONTAINMENT_VERSION
        or receipt.get("operation") != "contain-failed-manager-rotation"
        or receipt.get("state") != "complete"
        or receipt.get("claim") != "legacy-audit-not-reconciled-post-failure-work-not-accepted"
        or receipt.get("dispatch_state") != "stopped-no-codex-process"
        or receipt.get("error") != ""
    ):
        raise BridgeError("containment receipt is not one exact successful fail-closed containment.")
    recorded_at = exact_string(receipt, "recorded_at", "containment receipt")
    _ = parse_datetime(recorded_at, "containment receipt timestamp")
    protected_raw = receipt.get("protected_targets")
    if not isinstance(protected_raw, list):
        raise BridgeError("containment receipt protected-target list is invalid.")
    protected_values = cast(list[object], protected_raw)
    if not all(isinstance(target, str) for target in protected_values):
        raise BridgeError("containment receipt protected-target list is invalid.")
    protected_targets = tuple(cast(str, target) for target in protected_values)
    if protected_targets != args.protected_targets or queue_sha256(protected_targets) != receipt.get("protected_targets_sha256"):
        raise BridgeError("containment receipt protected-target binding does not match the explicit assertion.")
    audit = audit_record(receipt.get("failed_audit"), "containment receipt failed audit")
    old_session = session_record(receipt.get("old_session"), "containment receipt old session")
    failed_pane = pane_record(receipt.get("successor_pane"), "containment receipt failed successor pane")
    failed_process = process_record(receipt.get("successor_process"), "containment receipt failed successor process")
    failed_tree_raw = receipt.get("successor_process_tree")
    if not isinstance(failed_tree_raw, list) or not failed_tree_raw:
        raise BridgeError("containment receipt failed-successor process tree is invalid.")
    failed_tree = tuple(
        process_record(value, f"containment receipt failed successor process {index}")
        for index, value in enumerate(cast(list[object], failed_tree_raw), 1)
    )
    failed_session = session_record(receipt.get("successor_session"), "containment receipt failed successor session")
    final_session_file = file_evidence_record(
        receipt.get("successor_session_final_file"),
        "containment receipt failed successor final transcript",
    )
    recorded_task = task_record(receipt.get("task"), "containment receipt task")
    recorded_watcher = watcher_record(receipt.get("watcher"), "containment receipt watcher")
    contained = process_record(receipt.get("contained_process"), "containment receipt sentinel")
    if exact_string(receipt, "root", "containment receipt") != str(args.root):
        raise BridgeError("containment receipt root does not match the explicit canonical root.")
    if not same_tmux_target(exact_string(receipt, "target", "containment receipt"), args.target):
        raise BridgeError("containment receipt target does not match the explicit bridge target.")
    if (
        failed_pane.pane_id != args.expected_contained_pane_id
        or failed_pane.window_id != args.expected_contained_window_id
        or not same_tmux_target(failed_pane.canonical_target, args.target)
        or contained.pid != args.expected_contained_pid
        or contained.start_ticks != args.expected_contained_start_ticks
        or contained.argv_sha256 != args.expected_contained_argv_sha256
    ):
        raise BridgeError("containment receipt pane or inert-sentinel identity does not match explicit assertions.")
    if (
        recorded_task.file.path != str(args.task_file)
        or recorded_task.file.sha256 != args.expected_task_sha256
        or recorded_task.status != "blocked"
        or recorded_task.blocker != args.expected_blocker
        or not same_tmux_target(recorded_task.runat, args.target)
        or not same_tmux_target(recorded_task.managerat, args.expected_manager_target)
        or recorded_task.pending_items != args.expected_pending_items
        or recorded_task.queue_sha256 != queue_sha256(args.expected_pending_items)
        or recorded_task.todo_section != "current"
    ):
        raise BridgeError("containment receipt task, queue, TODO, owner, or reporting binding does not match assertions.")
    if recorded_watcher.pid != args.expected_watcher_pid or recorded_watcher.start_ticks != args.expected_watcher_start_ticks:
        raise BridgeError("containment receipt watcher identity does not match explicit assertions.")
    if Path(audit.root) != args.root or not same_tmux_target(audit.target, args.target):
        raise BridgeError("containment receipt failed-audit root or target is inconsistent.")
    if (
        failed_process.pid != failed_pane.pane_pid
        or not failed_tree
        or failed_tree[0] != failed_process
        or contained.pid in {process.pid for process in failed_tree}
        or failed_pane.pane_id != audit.old_pane.pane_id
        or failed_pane.window_id != audit.old_pane.window_id
        or failed_pane.working_directory != audit.old_pane.working_directory
    ):
        raise BridgeError("containment receipt process lineage or same-pane chain is inconsistent.")
    if failed_session.file != final_session_file:
        raise BridgeError("containment receipt failed-successor transcript finalization is inconsistent.")
    launch_argv0, launch_executable = original_launch_executable(audit)
    if launch_argv0.name != args.expected_failed_successor_command:
        raise BridgeError("failed-successor executable does not match the explicit command assertion.")
    failed_prompt_bytes, _ = read_regular_file(Path(audit.prompt.path), "contained fresh prompt", audit.prompt.sha256)
    try:
        failed_prompt = failed_prompt_bytes.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise BridgeError("contained fresh prompt is not UTF-8.") from exc
    failed_argv = (
        str(launch_argv0),
        CODEX_PACKAGE,
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        audit.model,
        "--config",
        f'model_reasoning_effort="{audit.reasoning_effort}"',
        failed_prompt,
    )
    if sha256("\0".join(failed_argv).encode()) != failed_process.argv_sha256:
        raise BridgeError("containment receipt failed-successor command does not match its recorded argv identity.")
    containment_args = ContainmentArgs(
        args.root,
        args.task_file,
        args.target,
        Path(audit.file.path),
        audit.file.sha256,
        Path(audit.watcher_failure_log.path),
        audit.watcher_failure_log.sha256,
        audit.prompt.sha256,
        Path(old_session.file.path),
        old_session.file.sha256,
        old_session.session_id,
        Path(failed_session.file.path),
        failed_session.session_id,
        recorded_task.file.sha256,
        recorded_task.blocker,
        recorded_task.managerat,
        recorded_task.pending_items,
        audit.old_pane.pane_id,
        audit.old_pane.window_id,
        audit.old_pane.pane_pid,
        audit.old_launch_pid,
        failed_pane.pane_pid,
        args.expected_failed_successor_command,
        protected_targets,
        args.containment_receipt,
        True,
    )
    validated_audit, validated_old_session = audit_binding(containment_args)
    if validated_audit != audit or validated_old_session != old_session:
        raise BridgeError("containment receipt no longer matches its original failed audit or old session.")
    prompt_bytes, prompt_file = read_regular_file(Path(audit.prompt.path), "contained fresh prompt", audit.prompt.sha256)
    prompt = prompt_bytes.decode("utf-8").rstrip("\n")
    if prompt_file != audit.prompt:
        raise BridgeError("contained fresh-prompt file identity changed after containment.")
    validated_failed_session = session_evidence(
        Path(failed_session.file.path),
        failed_session.session_id,
        prompt,
        "contained failed-successor transcript",
        failed_session.file.sha256,
    )
    if validated_failed_session != failed_session:
        raise BridgeError("contained failed-successor transcript changed after containment.")
    if parse_datetime(recorded_at, "containment receipt timestamp") < parse_datetime(failed_session.started_at, "failed successor timestamp"):
        raise BridgeError("containment receipt predates the failed successor session.")
    if args.session_root not in Path(old_session.file.path).parents or args.session_root not in Path(failed_session.file.path).parents:
        raise BridgeError("explicit session root does not contain both receipt-bound session transcripts.")
    state_dir = Path(audit.state_dir)
    if args.containment_receipt.parent != state_dir / "rotations" or args.ownership_receipt.parent != args.containment_receipt.parent:
        raise BridgeError("containment and ownership receipts must be siblings in the bound rotations directory.")
    return ContainmentReceipt(
        receipt_file,
        recorded_at,
        audit,
        old_session,
        failed_pane,
        failed_process,
        failed_tree,
        failed_session,
        recorded_task,
        recorded_watcher,
        contained,
        protected_targets,
        launch_argv0,
        launch_executable,
        containment_args,
    )


def protected_binding(target: str) -> ProtectedBinding:
    pane = resolve_exact_pane(target)
    process = process_stat(pane.pane_pid)
    if process.state == "Z":
        raise BridgeError(f"protected target is not live: {target}")
    return ProtectedBinding(target, pane, current_command(pane.pane_id), process)


def current_task(receipt: ContainmentReceipt) -> TaskBinding:
    observed = task_binding(receipt.containment_args)
    if not task_binding_matches(observed, receipt.task):
        raise BridgeError("task, queue, TODO membership, reporting parent, or sole ownership changed after containment.")
    return observed


def process_has_failed_successor_identity(process: ProcessIdentity, receipt: ContainmentReceipt) -> bool:
    sessions = {value.session for value in receipt.failed_successor_tree}
    groups = {value.process_group for value in receipt.failed_successor_tree}
    return process.session in sessions or process.process_group in groups


def contained_live(args: Args, receipt: ContainmentReceipt) -> ContainedLive:
    pane = resolve_exact_pane(args.target)
    if (
        pane.pane_id != args.expected_contained_pane_id
        or pane.window_id != args.expected_contained_window_id
        or not same_tmux_target(pane.canonical_target, args.target)
        or pane.working_directory != receipt.failed_successor_pane.working_directory
        or pane.pane_pid != receipt.contained_process.pid
        or current_command(pane.pane_id) != "sleep"
    ):
        raise BridgeError("live pane is not the exact receipt-bound inert containment sentinel.")
    sentinel = process_stat(pane.pane_pid)
    argv = process_argv(pane.pane_pid)
    if (
        sentinel.state == "Z"
        or stable_process_identity(sentinel) != stable_process_identity(receipt.contained_process)
        or len(argv) != 2
        or Path(argv[0]).name != "sleep"
        or argv[1] != "infinity"
        or sha256("\0".join(argv).encode()) != receipt.contained_process.argv_sha256
    ):
        raise BridgeError("live containment process is not the exact inert sleep infinity sentinel.")
    processes = process_snapshot()
    if process_tree(sentinel.pid, processes) != (sentinel,):
        raise BridgeError("inert containment sentinel unexpectedly has a child process.")
    survivors = sorted(
        process.pid
        for process in processes.values()
        if process.pid != sentinel.pid and process_has_failed_successor_identity(process, receipt)
    )
    if survivors:
        raise BridgeError(f"failed successor process session/group has live survivors: {survivors}")
    observed_watcher = watcher_proof(args.root)
    if observed_watcher != receipt.watcher:
        raise BridgeError("canonical root watcher changed after containment.")
    protected = tuple(protected_binding(target) for target in args.protected_targets)
    return ContainedLive(pane, sentinel, current_task(receipt), observed_watcher, protected)


def protected_unchanged(current: tuple[ProtectedBinding, ...], expected: tuple[ProtectedBinding, ...]) -> bool:
    return len(current) == len(expected) and all(
        observed.target == recorded.target
        and observed.pane == recorded.pane
        and observed.command == recorded.command
        and stable_process_identity(observed.process) == stable_process_identity(recorded.process)
        for observed, recorded in zip(current, expected, strict=True)
    )


def revalidate_contained(args: Args, receipt: ContainmentReceipt, initial: ContainedLive) -> ContainedLive:
    current = contained_live(args, receipt)
    if (
        current.pane != initial.pane
        or stable_process_identity(current.sentinel) != stable_process_identity(initial.sentinel)
        or not task_binding_matches(current.task, initial.task)
        or current.watcher != initial.watcher
        or not protected_unchanged(current.protected, initial.protected)
    ):
        raise BridgeError("sentinel, task, queue, TODO, watcher, owner, or protected target changed before launch.")
    return current


def bridge_locks(args: Args, state_dir: Path) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(manager_rotation_lock(state_dir))
    stack.enter_context(task_file_lock(args.root / ".omo-task-membership.lock"))
    for target in sorted((args.target, *args.protected_targets)):
        stack.enter_context(task_target_lock(args.root, target))
    for path in sorted((args.task_file, args.root / "TODO.md"), key=str):
        stack.enter_context(task_file_lock(path))
    return stack


def fresh_launch_command(args: Args, receipt: ContainmentReceipt) -> tuple[str, tuple[str, ...]]:
    if executable_evidence(Path(receipt.launch_executable.path)) != receipt.launch_executable:
        raise BridgeError("fresh manager executable changed after containment receipt validation.")
    prompt_bytes, _ = read_regular_file(
        Path(receipt.audit.prompt.path),
        "fresh manager prompt",
        receipt.audit.prompt.sha256,
    )
    try:
        prompt = prompt_bytes.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise BridgeError("fresh manager prompt is not UTF-8.") from exc
    executable = str(receipt.launch_argv0)
    argv = (
        executable,
        CODEX_PACKAGE,
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        receipt.audit.model,
        "--config",
        f'model_reasoning_effort="{receipt.audit.reasoning_effort}"',
        "--config",
        "check_for_update_on_startup=false",
    )
    exports = {
        "OMO_AGENT_TMUX_TARGET": receipt.failed_successor_pane.canonical_target,
        "OMO_MANAGER_TMUX_TARGET": receipt.failed_successor_pane.canonical_target,
        "OMO_MANAGER_STATE_DIR": receipt.audit.state_dir,
        "OMO_WORK_LOGS_ROOT": str(args.root),
    }
    export_command = " ".join(f"{name}={shlex.quote(value)}" for name, value in exports.items())
    command = (
        f"export {export_command} && exec -a {shlex.quote(executable)} "
        f"{shlex.quote(receipt.launch_executable.path)} {shlex.join(argv[1:])} {shlex.quote(prompt)}"
    )
    if " resume " in f" {command.casefold()} " or UUID_ANY_RE.search(command) is not None:
        raise BridgeError("fresh containment-bridge command unexpectedly attempts session resume.")
    return command, argv


def tmux_condition(pane: PaneIdentity, expected_command: str) -> str:
    if COMMAND_RE.fullmatch(expected_command) is None:
        raise BridgeError("tmux process guard command contains unsupported characters.")
    return "#{&&:#{==:#{pane_id},%s},#{&&:#{==:#{window_id},%s},#{&&:#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{&&:#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}}}}" % (
        pane.pane_id,
        pane.window_id,
        pane.canonical_target,
        pane.pane_pid,
        expected_command,
    )


def guarded_fresh_launch(live: ContainedLive, command: str) -> None:
    accepted = f"OMO_MANAGER_BRIDGE_ACCEPTED_{os.getpid()}_{time.monotonic_ns()}"
    rejected = f"OMO_MANAGER_BRIDGE_REJECTED_{os.getpid()}_{time.monotonic_ns()}"
    respawn = shlex.join(
        ["respawn-pane", "-k", "-t", live.pane.pane_id, "-c", str(live.pane.working_directory), command]
    )
    result = subprocess.run(
        [
            "tmux",
            "if-shell",
            "-F",
            "-t",
            live.pane.canonical_target,
            tmux_condition(live.pane, "sleep"),
            f"{respawn} ; display-message -p {accepted}",
            f"display-message -p {rejected}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise BridgeError("tmux rejected the exact inert-sentinel launch guard.")


def session_inventory(session_root: Path) -> frozenset[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for path in session_root.rglob("rollout-*.jsonl"):
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    return frozenset(identities)


def relevant_environment(pid: int) -> tuple[dict[str, str], str]:
    before = process_stat(pid)
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError as exc:
        raise BridgeError("cannot inspect fresh manager environment.") from exc
    after = process_stat(pid)
    if stable_process_identity(before) != stable_process_identity(after):
        raise BridgeError("fresh manager process changed while reading its environment.")
    values: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            key, value = entry.decode("utf-8").split("=", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BridgeError("fresh manager environment is malformed.") from exc
        if key in values:
            raise BridgeError("fresh manager environment contains a duplicate variable.")
        values[key] = value
    names = ("OMO_AGENT_TMUX_TARGET", "OMO_MANAGER_TMUX_TARGET", "OMO_MANAGER_STATE_DIR", "OMO_WORK_LOGS_ROOT")
    relevant = {name: values.get(name, "") for name in names}
    return relevant, sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode())


def held_session_paths(tree: tuple[ProcessIdentity, ...], session_root: Path) -> dict[tuple[int, int], tuple[Path, int, int]]:
    result: dict[tuple[int, int], tuple[Path, int, int]] = {}
    for process in tree:
        fd_root = Path("/proc") / str(process.pid) / "fd"
        try:
            descriptors = tuple(fd_root.iterdir())
        except OSError:
            continue
        for descriptor_path in descriptors:
            try:
                descriptor = int(descriptor_path.name)
                destination = Path(os.readlink(descriptor_path))
                info = descriptor_path.stat()
            except (OSError, ValueError):
                continue
            if not destination.is_absolute() or session_root not in destination.parents or not destination.name.startswith("rollout-") or destination.suffix != ".jsonl":
                continue
            try:
                resolved = destination.resolve(strict=True)
                path_info = resolved.stat(follow_symlinks=False)
            except OSError:
                continue
            if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(path_info.st_mode):
                continue
            result[(info.st_dev, info.st_ino)] = (resolved, process.pid, descriptor)
    return result


def fresh_session_handle(
    tree: tuple[ProcessIdentity, ...],
    session_root: Path,
    preexisting: frozenset[tuple[int, int]],
    prompt: str,
    cwd: Path,
    started_after: datetime,
    forbidden_ids: frozenset[str],
) -> SessionHandle | None:
    matches: list[SessionHandle] = []
    for identity, (path, holder_pid, descriptor) in held_session_paths(tree, session_root).items():
        if identity in preexisting:
            continue
        match = UUID_ANY_RE.search(path.name)
        if match is None or not path.name.endswith(f"-{match.group(0)}.jsonl"):
            continue
        try:
            evidence = session_evidence(path, match.group(0).casefold(), prompt, "fresh manager transcript")
        except (ContainmentError, OSError):
            continue
        if (
            (evidence.file.device, evidence.file.inode) != identity
            or evidence.session_id in forbidden_ids
            or evidence.file.mode & 0o022
            or Path(evidence.cwd).resolve(strict=True) != cwd
            or parse_datetime(evidence.started_at, "fresh manager timestamp") < started_after
        ):
            continue
        holder = next((process for process in tree if process.pid == holder_pid), None)
        if holder is None:
            continue
        matches.append(SessionHandle(evidence, holder_pid, holder.start_ticks, descriptor))
    unique = {(match.session.file.device, match.session.file.inode): match for match in matches}
    if len(unique) > 1:
        raise BridgeError("fresh manager process tree holds multiple new matching Codex sessions.")
    return next(iter(unique.values()), None)


def exact_launch_argv(argv: tuple[str, ...], executable_argv: tuple[str, ...], prompt: str) -> bool:
    return argv == (*executable_argv, prompt)


def verify_process_group_is_singular(root: ProcessIdentity, tree: tuple[ProcessIdentity, ...], processes: Mapping[int, ProcessIdentity]) -> None:
    tree_pids = {process.pid for process in tree}
    outsiders = sorted(
        process.pid
        for process in processes.values()
        if process.pid not in tree_pids
        and process.pid != root.pid
        and (process.session == root.session or process.process_group == root.process_group or (root.tty and process.tty == root.tty))
    )
    if outsiders:
        raise BridgeError(f"fresh manager process group/session/TTY has outside members: {outsiders}")


def active_successor(
    args: Args,
    receipt: ContainmentReceipt,
    live: ContainedLive,
    executable_argv: tuple[str, ...],
    preexisting_sessions: frozenset[tuple[int, int]],
    started_after: datetime,
) -> ActiveSuccessor:
    deadline = time.monotonic() + args.startup_timeout_s
    prompt_bytes, _ = read_regular_file(Path(receipt.audit.prompt.path), "fresh manager prompt", receipt.audit.prompt.sha256)
    prompt = prompt_bytes.decode("utf-8").rstrip("\n")
    forbidden_ids = frozenset((receipt.old_session.session_id, receipt.failed_successor_session.session_id))
    last_state = "startup"
    while time.monotonic() < deadline:
        pane = resolve_exact_pane(args.target)
        if (
            pane.pane_id != live.pane.pane_id
            or pane.window_id != live.pane.window_id
            or pane.canonical_target != live.pane.canonical_target
            or pane.working_directory != live.pane.working_directory
            or pane.pane_pid == live.sentinel.pid
        ):
            raise BridgeError("fresh launch did not preserve the exact pane/window or replace the sentinel PID.")
        command = current_command(pane.pane_id)
        if command != Path(receipt.launch_executable.path).name:
            last_state = f"command={command or '<empty>'}"
            time.sleep(args.poll_interval_s)
            continue
        process = process_stat(pane.pane_pid)
        argv = process_argv(pane.pane_pid)
        if process.state == "Z" or process.session != process.pid or process.process_group != process.pid:
            raise BridgeError("fresh manager pane PID is not one live process-group and session leader.")
        if not exact_launch_argv(argv, executable_argv, prompt):
            raise BridgeError("fresh manager process argv does not match the receipt-derived launch.")
        environment, environment_digest = relevant_environment(process.pid)
        expected_environment = {
            "OMO_AGENT_TMUX_TARGET": live.pane.canonical_target,
            "OMO_MANAGER_TMUX_TARGET": live.pane.canonical_target,
            "OMO_MANAGER_STATE_DIR": receipt.audit.state_dir,
            "OMO_WORK_LOGS_ROOT": str(args.root),
        }
        if environment != expected_environment:
            raise BridgeError("fresh manager environment is not bound to the exact target/root/state directory.")
        processes = process_snapshot()
        tree = process_tree(process.pid, processes)
        launchers = [value for value in tree if is_codex_launch_argv(process_argv(value.pid))]
        if len(launchers) != 1 or launchers[0].pid != process.pid:
            raise BridgeError("fresh manager tree does not contain exactly one supported Codex launcher.")
        verify_process_group_is_singular(process, tree, processes)
        status = status_classification(live.pane.canonical_target)
        if status in TERMINAL_FAILURE_STATUSES:
            raise BridgeError(f"fresh manager startup classified as {status}.")
        if status not in SUCCESS_STATUSES:
            last_state = f"status={status or '<unknown>'}"
            time.sleep(args.poll_interval_s)
            continue
        handle = fresh_session_handle(
            tree,
            args.session_root,
            preexisting_sessions,
            prompt,
            live.pane.working_directory,
            started_after,
            forbidden_ids,
        )
        if handle is None:
            last_state = "new session transcript not yet held"
            time.sleep(args.poll_interval_s)
            continue
        observed_task = current_task(receipt)
        observed_watcher = watcher_proof(args.root)
        observed_protected = tuple(protected_binding(target) for target in args.protected_targets)
        if observed_watcher != live.watcher or not protected_unchanged(observed_protected, live.protected):
            raise BridgeError("watcher or protected-target binding changed during fresh launch.")
        return ActiveSuccessor(
            pane,
            process,
            tree,
            handle,
            status,
            environment_digest,
            observed_task,
            observed_watcher,
            observed_protected,
        )
    raise BridgeError(f"timed out proving the fresh manager successor: {last_state}.")


def revalidate_active(
    args: Args,
    receipt: ContainmentReceipt,
    live: ContainedLive,
    active: ActiveSuccessor,
    executable_argv: tuple[str, ...],
    preexisting_sessions: frozenset[tuple[int, int]],
    started_after: datetime,
) -> ActiveSuccessor:
    if containment_receipt(args) != receipt:
        raise BridgeError("containment receipt or its historical evidence changed after fresh launch.")
    current = active_successor(args, receipt, live, executable_argv, preexisting_sessions, started_after)
    if (
        current.pane != active.pane
        or stable_process_identity(current.process) != stable_process_identity(active.process)
        or current.session_handle.session.session_id != active.session_handle.session.session_id
        or current.session_handle.session.file.path != active.session_handle.session.file.path
        or (current.session_handle.session.file.device, current.session_handle.session.file.inode)
        != (active.session_handle.session.file.device, active.session_handle.session.file.inode)
        or current.session_handle.holder_pid != active.session_handle.holder_pid
        or current.session_handle.holder_start_ticks != active.session_handle.holder_start_ticks
        or current.session_handle.descriptor != active.session_handle.descriptor
        or not task_binding_matches(current.task, active.task)
        or current.watcher != active.watcher
        or not protected_unchanged(current.protected, active.protected)
    ):
        raise BridgeError("fresh successor identity, session, ownership, or protected binding changed before receipt finalization.")
    return current


def guarded_inert_rollback(pane: PaneIdentity, process: ProcessIdentity, command: str) -> None:
    if pane.pane_pid != process.pid:
        raise BridgeError("rollback pane and process identity do not agree.")
    sleep_raw = shutil.which("sleep")
    if sleep_raw is None:
        raise BridgeError("sleep is unavailable for fail-closed bridge rollback.")
    sleep_path = str(Path(sleep_raw).resolve(strict=True))
    accepted = f"OMO_MANAGER_BRIDGE_ROLLBACK_{os.getpid()}_{time.monotonic_ns()}"
    rejected = f"OMO_MANAGER_BRIDGE_ROLLBACK_REJECTED_{os.getpid()}_{time.monotonic_ns()}"
    respawn = shlex.join(
        ["respawn-pane", "-k", "-t", pane.pane_id, "-c", str(pane.working_directory), f"exec {shlex.quote(sleep_path)} infinity"]
    )
    result = subprocess.run(
        [
            "tmux",
            "if-shell",
            "-F",
            "-t",
            pane.canonical_target,
            tmux_condition(pane, command),
            f"{respawn} ; display-message -p {accepted}",
            f"display-message -p {rejected}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise BridgeError("tmux rejected the exact unverified-successor rollback guard.")


def verify_rollback(args: Args, prior_pane: PaneIdentity, prior_tree: tuple[ProcessIdentity, ...]) -> ProcessIdentity:
    deadline = time.monotonic() + 5.0
    pane: PaneIdentity | None = None
    while time.monotonic() < deadline:
        pane = resolve_exact_pane(args.target)
        if pane.pane_pid != prior_pane.pane_pid and current_command(pane.pane_id) == "sleep":
            break
        time.sleep(0.05)
    if (
        pane is None
        or pane.pane_id != prior_pane.pane_id
        or pane.window_id != prior_pane.window_id
        or pane.canonical_target != prior_pane.canonical_target
        or pane.working_directory != prior_pane.working_directory
        or pane.pane_pid == prior_pane.pane_pid
        or current_command(pane.pane_id) != "sleep"
    ):
        raise BridgeError("bridge rollback did not install a same-pane inert sentinel.")
    sentinel = process_stat(pane.pane_pid)
    argv = process_argv(pane.pane_pid)
    if (
        sentinel.state == "Z"
        or sentinel.session != sentinel.pid
        or sentinel.process_group != sentinel.pid
        or len(argv) != 2
        or Path(argv[0]).name != "sleep"
        or argv[1] != "infinity"
    ):
        raise BridgeError("bridge rollback process is not an exact inert sleep infinity sentinel.")
    processes = process_snapshot()
    old_sessions = {value.session for value in prior_tree}
    old_groups = {value.process_group for value in prior_tree}
    old_ttys = {value.tty for value in prior_tree if value.tty}
    survivors = sorted(
        value.pid
        for value in processes.values()
        if value.pid != sentinel.pid
        and (value.session in old_sessions or value.process_group in old_groups or (value.tty and value.tty in old_ttys))
    )
    if survivors or process_tree(sentinel.pid, processes) != (sentinel,):
        raise BridgeError(f"unverified successor survived fail-closed rollback: {survivors}")
    return sentinel


def rollback_unverified(args: Args, receipt: ContainmentReceipt, live: ContainedLive) -> ProcessIdentity:
    pane = resolve_exact_pane(args.target)
    if (
        pane.pane_id != live.pane.pane_id
        or pane.window_id != live.pane.window_id
        or pane.canonical_target != live.pane.canonical_target
        or pane.working_directory != live.pane.working_directory
    ):
        raise BridgeError("cannot safely rollback because the target pane/window identity changed.")
    if pane.pane_pid == live.sentinel.pid:
        restored = contained_live(args, receipt)
        if (
            restored.pane != live.pane
            or stable_process_identity(restored.sentinel) != stable_process_identity(live.sentinel)
            or not task_binding_matches(restored.task, live.task)
            or restored.watcher != live.watcher
            or not protected_unchanged(restored.protected, live.protected)
        ):
            raise BridgeError("original containment sentinel or its protected bindings changed during rollback.")
        return restored.sentinel
    process = process_stat(pane.pane_pid)
    command = current_command(pane.pane_id)
    if process.state == "Z" or stable_process_identity(process) != stable_process_identity(process_stat(pane.pane_pid)):
        raise BridgeError("cannot safely rollback an unstable fresh manager process.")
    tree = process_tree(process.pid, process_snapshot())
    guarded_inert_rollback(pane, process, command)
    sentinel = verify_rollback(args, pane, tree)
    processes = process_snapshot()
    survivors = sorted(
        value.pid
        for value in processes.values()
        if value.pid != sentinel.pid and process_has_failed_successor_identity(value, receipt)
    )
    observed_task = current_task(receipt)
    observed_watcher = watcher_proof(args.root)
    observed_protected = tuple(protected_binding(target) for target in args.protected_targets)
    if (
        survivors
        or not task_binding_matches(observed_task, live.task)
        or observed_watcher != live.watcher
        or not protected_unchanged(observed_protected, live.protected)
    ):
        raise BridgeError(
            "rollback did not preserve the task, watcher, protected targets, or failed-successor absence."
        )
    return sentinel


def receipt_payload(
    args: Args,
    containment: ContainmentReceipt,
    live: ContainedLive,
    state: str,
    command: str,
    executable_argv: tuple[str, ...],
    preexisting_sessions: frozenset[tuple[int, int]],
    *,
    active: ActiveSuccessor | None = None,
    rollback: ProcessIdentity | None = None,
    launch_attempted: bool = False,
    error: str = "",
) -> dict[str, object]:
    consumed = active is not None or launch_attempted
    if active is not None:
        claim = "containment-consumed-one-fresh-sole-successor"
        dispatch_state = "fresh-codex-active"
    elif rollback is not None:
        claim = "successor-launch-failed-contained-inert"
        dispatch_state = "rolled-back-to-inert"
    elif launch_attempted:
        claim = "successor-launch-failed-containment-unverified"
        dispatch_state = "containment-unknown"
    elif state == "prepared":
        claim = "containment-reserved-successor-launch"
        dispatch_state = "launch-not-executed"
    else:
        claim = "successor-launch-not-attempted"
        dispatch_state = "launch-not-executed"
    return {
        "version": BRIDGE_VERSION,
        "operation": "launch-contained-manager-successor",
        "state": state,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "claim": claim,
        "root": str(args.root),
        "target": args.target,
        "containment_receipt": asdict(containment.file),
        "containment_receipt_consumed": consumed,
        "contained_pane": asdict(live.pane) | {"working_directory": str(live.pane.working_directory)},
        "contained_process": asdict(live.sentinel),
        "task": asdict(live.task),
        "watcher": asdict(live.watcher),
        "protected_targets": [asdict(value) | {"pane": asdict(value.pane) | {"working_directory": str(value.pane.working_directory)}} for value in live.protected],
        "launch_command_sha256": sha256(command.encode()),
        "launch_executable": asdict(containment.launch_executable),
        "launch_prefix_argv_sha256": sha256("\0".join(executable_argv).encode()),
        "session_root": str(args.session_root),
        "preexisting_session_count": len(preexisting_sessions),
        "preexisting_sessions_sha256": sha256(
            json.dumps(sorted(preexisting_sessions), separators=(",", ":")).encode()
        ),
        "fresh_session": asdict(active.session_handle.session) if active is not None else None,
        "fresh_session_handle": {
            "holder_pid": active.session_handle.holder_pid,
            "holder_start_ticks": active.session_handle.holder_start_ticks,
            "descriptor": active.session_handle.descriptor,
        }
        if active is not None
        else None,
        "fresh_pane": asdict(active.pane) | {"working_directory": str(active.pane.working_directory)} if active is not None else None,
        "fresh_process": asdict(active.process) if active is not None else None,
        "fresh_process_tree": [asdict(value) for value in active.process_tree] if active is not None else [],
        "fresh_status": active.status if active is not None else "",
        "fresh_environment_sha256": active.environment_sha256 if active is not None else "",
        "final_task": asdict(active.task) if active is not None else None,
        "final_watcher": asdict(active.watcher) if active is not None else None,
        "final_protected_targets": [
            asdict(value) | {"pane": asdict(value.pane) | {"working_directory": str(value.pane.working_directory)}}
            for value in active.protected
        ]
        if active is not None
        else [],
        "authoritative_owner_count": 1 if active is not None else 0,
        "authorized_successor_count": 1 if active is not None else 0,
        "ownership_state": "sole-fresh-successor" if active is not None else "not-established",
        "dispatch_state": dispatch_state,
        "rollback_process": asdict(rollback) if rollback is not None else None,
        "error": error,
    }


def finalize_failure(path: Path, prepared: bytes, failed: bytes, primary: Exception) -> None:
    try:
        replace_private_exact(path, prepared, failed)
    except Exception as receipt_error:
        try:
            _ = print(f"ownership receipt finalization also failed: {receipt_error}", file=sys.stderr)
        except (OSError, UnicodeError, ValueError):
            pass
    raise primary


def finalize_success(path: Path, prepared: bytes, complete: bytes) -> bool:
    """Finalize once; return true when an apparent fsync failure still committed exact bytes."""

    try:
        replace_private_exact(path, prepared, complete)
    except Exception as primary:
        try:
            current, evidence = read_regular_file(path, "ownership receipt after finalization error")
        except Exception:
            raise primary
        if current == complete and evidence.mode == 0o600 and evidence.uid == os.getuid():
            return True
        raise primary
    return True


# 🧑 Manager delegation: "Atomically replace only that sentinel, reject any drift, and emit a final successful ownership receipt."
def launch_contained_successor(args: Args) -> tuple[str, Path | None]:
    initial_receipt = containment_receipt(args)
    state_dir = Path(initial_receipt.audit.state_dir)
    require_private_parent(args.ownership_receipt, "ownership receipt")
    if args.ownership_receipt.exists() or args.ownership_receipt.is_symlink():
        raise BridgeError("ownership receipt output must not already exist.")
    with bridge_locks(args, state_dir):
        rebound_receipt = containment_receipt(args)
        if rebound_receipt != initial_receipt:
            raise BridgeError("containment receipt binding changed before bridge execution.")
        live = contained_live(args, rebound_receipt)
        command, executable_argv = fresh_launch_command(args, rebound_receipt)
        if args.dry_run:
            _ = revalidate_contained(args, rebound_receipt, live)
            return "successor-launch-ready", None
        preexisting_sessions = session_inventory(args.session_root)
        prepared = receipt_bytes(
            receipt_payload(
                args,
                rebound_receipt,
                live,
                "prepared",
                command,
                executable_argv,
                preexisting_sessions,
            )
        )
        write_private_exclusive(args.ownership_receipt, prepared)
        launch_attempted = False
        active: ActiveSuccessor | None = None
        started_after = datetime.now(timezone.utc)
        try:
            _ = revalidate_contained(args, rebound_receipt, live)
            launch_attempted = True
            guarded_fresh_launch(live, command)
            active = active_successor(
                args,
                rebound_receipt,
                live,
                executable_argv,
                preexisting_sessions,
                started_after,
            )
            active = revalidate_active(
                args,
                rebound_receipt,
                live,
                active,
                executable_argv,
                preexisting_sessions,
                started_after,
            )
        except Exception as primary:
            rollback: ProcessIdentity | None = None
            if launch_attempted:
                try:
                    rollback = rollback_unverified(args, rebound_receipt, live)
                except Exception as rollback_error:
                    primary = BridgeError(f"{primary}; fail-closed rollback also failed: {rollback_error}")
            failed = receipt_bytes(
                receipt_payload(
                    args,
                    rebound_receipt,
                    live,
                    "failed",
                    command,
                    executable_argv,
                    preexisting_sessions,
                    rollback=rollback,
                    launch_attempted=launch_attempted,
                    error=str(primary),
                )
            )
            finalize_failure(args.ownership_receipt, prepared, failed, primary)
        assert active is not None
        complete = receipt_bytes(
            receipt_payload(
                args,
                rebound_receipt,
                live,
                "complete",
                command,
                executable_argv,
                preexisting_sessions,
                active=active,
            )
        )
        try:
            _ = finalize_success(args.ownership_receipt, prepared, complete)
        except Exception as primary:
            rollback_after_finalize: ProcessIdentity | None = None
            try:
                rollback_after_finalize = rollback_unverified(args, rebound_receipt, live)
            except Exception as rollback_error:
                primary = BridgeError(f"{primary}; fail-closed rollback also failed: {rollback_error}")
            failed = receipt_bytes(
                receipt_payload(
                    args,
                    rebound_receipt,
                    live,
                    "failed",
                    command,
                    executable_argv,
                    preexisting_sessions,
                    rollback=rollback_after_finalize,
                    launch_attempted=True,
                    error=str(primary),
                )
            )
            finalize_failure(args.ownership_receipt, prepared, failed, primary)
        try:
            active = revalidate_active(
                args,
                rebound_receipt,
                live,
                active,
                executable_argv,
                preexisting_sessions,
                started_after,
            )
        except Exception as primary:
            rollback_after_commit: ProcessIdentity | None = None
            try:
                rollback_after_commit = rollback_unverified(args, rebound_receipt, live)
            except Exception as rollback_error:
                primary = BridgeError(f"{primary}; fail-closed rollback also failed: {rollback_error}")
            failed = receipt_bytes(
                receipt_payload(
                    args,
                    rebound_receipt,
                    live,
                    "failed",
                    command,
                    executable_argv,
                    preexisting_sessions,
                    rollback=rollback_after_commit,
                    launch_attempted=True,
                    error=str(primary),
                )
            )
            finalize_failure(args.ownership_receipt, complete, failed, primary)
        return "successor-launched", args.ownership_receipt


def main(argv: list[str] | None = None) -> int:
    try:
        result, receipt = launch_contained_successor(parse_args(sys.argv[1:] if argv is None else argv))
    except (BridgeError, ContainmentError, OSError, RotationError, subprocess.SubprocessError) as exc:
        print(f"omo_manager_containment_launch: {exc}", file=sys.stderr)
        return 2
    print(f"result: {result}")
    if receipt is not None:
        print(f"receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
