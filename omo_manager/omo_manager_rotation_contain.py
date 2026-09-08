#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Contain an unauthenticated successor left by a failed manager rotation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TASK_RE
from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_manager_rotate import CODEX_PACKAGE
from omo_manager.omo_manager_rotate import RotationError
from omo_manager.omo_manager_rotate import SUPPORTED_CODEX_PACKAGES
from omo_manager.omo_manager_rotate import PaneIdentity
from omo_manager.omo_manager_rotate import resolve_exact_pane
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths
from omo_manager.omo_task_status import has_pending_marker

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?$")
PANE_ID_RE = re.compile(r"^%[1-9][0-9]*$")
WINDOW_ID_RE = re.compile(r"^@[1-9][0-9]*$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
AUDIT_KEYS = {
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
PANE_KEYS = {"canonical_target", "pane_id", "pane_pid", "window_id", "working_directory"}
LAUNCH_KEYS = {"launch_argv", "launch_pid", "model", "reasoning_effort", "source"}
CONTAINMENT_VERSION = "omo-manager-rotation-containment/v1"
FAILURE_PREFIX = "watcher setup failed after fresh Codex startup: pending watcher supervisor exited; see "
LOCK_DIR_PREFIX = "omo-pending-watch-roots-"
PROC_ROOT = Path("/proc")
PROC_LOCKS = PROC_ROOT / "locks"


class ContainmentError(RuntimeError):
    """A containment safety check or mutation failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    target: str
    failed_audit: Path
    failed_audit_sha256: str
    watcher_failure_log: Path
    watcher_failure_log_sha256: str
    fresh_prompt_sha256: str
    old_session_transcript: Path
    old_session_transcript_sha256: str
    expected_old_session_id: str
    current_session_transcript: Path
    expected_current_session_id: str
    expected_task_sha256: str
    expected_blocker: str
    expected_manager_target: str
    expected_pending_items: tuple[str, ...]
    expected_old_pane_id: str
    expected_old_window_id: str
    expected_old_pane_pid: int
    expected_old_launch_pid: int
    expected_current_pane_pid: int
    expected_current_command: str
    protected_targets: tuple[str, ...]
    receipt_output: Path
    dry_run: bool


class ParsedArgs(argparse.Namespace):
    contain_failed_rotation: bool = False
    root: Path = Path(".")
    task_file: Path = Path(".")
    target: str = ""
    failed_audit: Path = Path(".")
    failed_audit_sha256: str = ""
    watcher_failure_log: Path = Path(".")
    watcher_failure_log_sha256: str = ""
    fresh_prompt_sha256: str = ""
    old_session_transcript: Path = Path(".")
    old_session_transcript_sha256: str = ""
    expected_old_session_id: str = ""
    current_session_transcript: Path = Path(".")
    expected_current_session_id: str = ""
    expected_task_sha256: str = ""
    expected_blocker: str = ""
    expected_manager_target: str = ""
    expected_pending_item: list[str] = []
    expect_empty_queue: bool = False
    expected_old_pane_id: str = ""
    expected_old_window_id: str = ""
    expected_old_pane_pid: int = 0
    expected_old_launch_pid: int = 0
    expected_current_pane_pid: int = 0
    expected_current_command: str = ""
    protected_target: list[str] = []
    receipt_output: Path = Path(".")
    dry_run: bool = False


@dataclass(frozen=True)
class FileEvidence:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    uid: int
    sha256: str


@dataclass(frozen=True)
class SessionEvidence:
    file: FileEvidence
    session_id: str
    started_at: str
    cwd: str
    launch_prompt_sha256: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    state: str
    process_group: int
    session: int
    tty: int
    start_ticks: int
    argv_sha256: str


@dataclass(frozen=True)
class WatcherProof:
    root: str
    declared_root: str
    lock_path: str
    lock_device: int
    lock_inode: int
    pid: int
    start_ticks: int
    argv_sha256: str


@dataclass(frozen=True)
class TaskBinding:
    file: FileEvidence
    status: str
    blocker: str
    runat: str
    managerat: str
    pending_items: tuple[str, ...]
    queue_sha256: str
    todo: FileEvidence
    todo_section: str
    todo_row: str
    todo_row_sha256: str


@dataclass(frozen=True)
class AuditBinding:
    file: FileEvidence
    recorded_at: str
    target: str
    root: str
    state_dir: str
    prompt: FileEvidence
    watcher_failure_log: FileEvidence
    old_pane: PaneIdentity
    old_launch_pid: int
    old_launch_argv_sha256: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class LiveBinding:
    pane: PaneIdentity
    pane_process: ProcessIdentity
    process_tree: tuple[ProcessIdentity, ...]
    session: SessionEvidence
    task: TaskBinding
    watcher: WatcherProof


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        epilog=(
            "This helper never authenticates or resumes the failed successor. It validates the exact failed audit, "
            "both session transcripts, task/queue/owner bindings, and canonical root watcher before replacing only "
            "the bound non-human pane process with an inert sleep process. --dry-run performs all read-only checks."
        ),
    )
    _ = parser.add_argument("--contain-failed-rotation", action="store_true", help="Required explicit containment mode.")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-file", type=Path, required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--failed-audit", type=Path, required=True)
    _ = parser.add_argument("--failed-audit-sha256", required=True)
    _ = parser.add_argument("--watcher-failure-log", type=Path, required=True)
    _ = parser.add_argument("--watcher-failure-log-sha256", required=True)
    _ = parser.add_argument("--fresh-prompt-sha256", required=True)
    _ = parser.add_argument("--old-session-transcript", type=Path, required=True)
    _ = parser.add_argument("--old-session-transcript-sha256", required=True)
    _ = parser.add_argument("--expected-old-session-id", required=True)
    _ = parser.add_argument("--current-session-transcript", type=Path, required=True)
    _ = parser.add_argument("--expected-current-session-id", required=True)
    _ = parser.add_argument("--expected-task-sha256", required=True)
    _ = parser.add_argument("--expected-blocker", required=True)
    _ = parser.add_argument("--expected-manager-target", required=True)
    _ = parser.add_argument("--expected-pending-item", action="append", default=[])
    _ = parser.add_argument("--expect-empty-queue", action="store_true")
    _ = parser.add_argument("--expected-old-pane-id", required=True)
    _ = parser.add_argument("--expected-old-window-id", required=True)
    _ = parser.add_argument("--expected-old-pane-pid", type=int, required=True)
    _ = parser.add_argument("--expected-old-launch-pid", type=int, required=True)
    _ = parser.add_argument("--expected-current-pane-pid", type=int, required=True)
    _ = parser.add_argument("--expected-current-command", required=True)
    _ = parser.add_argument("--protected-target", action="append", default=[])
    _ = parser.add_argument("--receipt-output", type=Path, required=True)
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.contain_failed_rotation:
        parser.error("--contain-failed-rotation is required; reconciliation is not supported for a legacy unbound audit.")
    hashes = (
        parsed.failed_audit_sha256,
        parsed.watcher_failure_log_sha256,
        parsed.fresh_prompt_sha256,
        parsed.old_session_transcript_sha256,
        parsed.expected_task_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        parser.error("all SHA-256 assertions must be lowercase 64-character hexadecimal values.")
    if UUID_RE.fullmatch(parsed.expected_old_session_id) is None or UUID_RE.fullmatch(parsed.expected_current_session_id) is None:
        parser.error("both expected Codex session ids must be UUIDs.")
    if parsed.expected_old_session_id.casefold() == parsed.expected_current_session_id.casefold():
        parser.error("old and current Codex session ids must differ.")
    targets = (parsed.target, parsed.expected_manager_target, *parsed.protected_target)
    if any(TARGET_RE.fullmatch(target) is None for target in targets):
        parser.error("target assertions must be exact SESSION:WINDOW[.PANE] values.")
    if parsed.target.partition(":")[0].startswith("h"):
        parser.error("containment cannot modify a human-owned h* session.")
    if not parsed.protected_target or len(set(parsed.protected_target)) != len(parsed.protected_target):
        parser.error("repeat --protected-target for one nonempty duplicate-free protected set.")
    if any(same_tmux_target(parsed.target, target) for target in parsed.protected_target):
        parser.error("the containment target cannot be in the protected-target set.")
    if bool(parsed.expected_pending_item) == parsed.expect_empty_queue:
        parser.error("use either repeated --expected-pending-item or --expect-empty-queue, exactly once.")
    if any(not item or "\n" in item or "\r" in item or "\0" in item for item in parsed.expected_pending_item):
        parser.error("expected pending items must be nonempty one-line values.")
    if PANE_ID_RE.fullmatch(parsed.expected_old_pane_id) is None or WINDOW_ID_RE.fullmatch(parsed.expected_old_window_id) is None:
        parser.error("old pane and window ids must be numeric tmux ids such as %15 and @15.")
    if min(parsed.expected_old_pane_pid, parsed.expected_old_launch_pid, parsed.expected_current_pane_pid) <= 1:
        parser.error("expected process ids must be greater than one.")
    if COMMAND_RE.fullmatch(parsed.expected_current_command) is None:
        parser.error("expected current command contains unsupported characters.")
    root = parsed.root.expanduser().resolve(strict=True)
    task_file = parsed.task_file.expanduser()
    if not task_file.is_absolute():
        task_file = root / task_file
    task_file = task_file.resolve(strict=True)
    if root not in task_file.parents or task_file == root / "TODO.md":
        parser.error("--task-file must resolve to a task beneath --root and differ from TODO.md.")
    absolute_paths = (
        parsed.failed_audit,
        parsed.watcher_failure_log,
        parsed.old_session_transcript,
        parsed.current_session_transcript,
        parsed.receipt_output,
    )
    if any(not path.expanduser().is_absolute() for path in absolute_paths):
        parser.error("audit, log, transcript, and receipt paths must be absolute.")
    return Args(
        root=root,
        task_file=task_file,
        target=parsed.target,
        failed_audit=parsed.failed_audit.expanduser(),
        failed_audit_sha256=parsed.failed_audit_sha256,
        watcher_failure_log=parsed.watcher_failure_log.expanduser(),
        watcher_failure_log_sha256=parsed.watcher_failure_log_sha256,
        fresh_prompt_sha256=parsed.fresh_prompt_sha256,
        old_session_transcript=parsed.old_session_transcript.expanduser(),
        old_session_transcript_sha256=parsed.old_session_transcript_sha256,
        expected_old_session_id=parsed.expected_old_session_id.casefold(),
        current_session_transcript=parsed.current_session_transcript.expanduser(),
        expected_current_session_id=parsed.expected_current_session_id.casefold(),
        expected_task_sha256=parsed.expected_task_sha256,
        expected_blocker=parsed.expected_blocker,
        expected_manager_target=parsed.expected_manager_target,
        expected_pending_items=tuple(parsed.expected_pending_item),
        expected_old_pane_id=parsed.expected_old_pane_id,
        expected_old_window_id=parsed.expected_old_window_id,
        expected_old_pane_pid=parsed.expected_old_pane_pid,
        expected_old_launch_pid=parsed.expected_old_launch_pid,
        expected_current_pane_pid=parsed.expected_current_pane_pid,
        expected_current_command=parsed.expected_current_command,
        protected_targets=tuple(parsed.protected_target),
        receipt_output=parsed.receipt_output.expanduser(),
        dry_run=parsed.dry_run,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_no_duplicates(data: bytes, label: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ContainmentError(f"{label} contains duplicate JSON key {key!r}.")
            result[key] = value
        return result

    try:
        parsed = cast(object, json.loads(data, object_pairs_hook=pairs))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContainmentError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ContainmentError(f"{label} must contain one JSON object.")
    return cast(dict[str, object], parsed)


def require_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ContainmentError(f"cannot inspect path component {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ContainmentError(f"path contains a symlink component: {current}")


def read_regular_file(path: Path, label: str, expected_sha256: str = "") -> tuple[bytes, FileEvidence]:
    require_no_symlink_components(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContainmentError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
            raise ContainmentError(f"{label} must be one regular file owned by the current user: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ContainmentError(f"{label} changed while it was read: {path}")
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    digest = sha256(data)
    if expected_sha256 and digest != expected_sha256:
        raise ContainmentError(f"{label} SHA-256 does not match its explicit assertion: {path}")
    evidence = FileEvidence(str(path), before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, stat.S_IMODE(before.st_mode), before.st_uid, digest)
    return data, evidence


def require_private_parent(path: Path, label: str) -> None:
    parent = path.parent
    require_no_symlink_components(parent)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ContainmentError(f"{label} parent must be an owner-private real directory: {parent}")


def parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ContainmentError(f"{label} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContainmentError(f"{label} must be an ISO timestamp.") from exc
    if parsed.tzinfo is None:
        raise ContainmentError(f"{label} must include a timezone.")
    return parsed


def prompt_user_messages(transcript: bytes, label: str) -> tuple[dict[str, object], list[str]]:
    complete_lines = transcript.splitlines()
    if not complete_lines:
        raise ContainmentError(f"{label} is empty.")
    records: list[dict[str, object]] = []
    for raw_line in complete_lines[:64]:
        records.append(json_no_duplicates(raw_line, label))
    metadata = records[0]
    if metadata.get("type") != "session_meta" or not isinstance(metadata.get("payload"), dict):
        raise ContainmentError(f"{label} does not begin with Codex session metadata.")
    messages: list[str] = []
    for record in records[1:]:
        payload_raw = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(payload_raw, dict):
            continue
        payload = cast(dict[str, object], payload_raw)
        content = payload.get("content")
        if payload.get("type") != "message" or payload.get("role") != "user" or not isinstance(content, list):
            continue
        parts: list[str] = []
        for item in cast(list[object], content):
            if not isinstance(item, dict):
                continue
            item_record = cast(dict[str, object], item)
            text = item_record.get("text")
            if item_record.get("type") == "input_text" and isinstance(text, str):
                parts.append(text)
        messages.append("".join(parts))
    return metadata, messages


def session_evidence(path: Path, expected_id: str, expected_prompt: str, label: str, expected_sha256: str = "") -> SessionEvidence:
    data, file_evidence = read_regular_file(path, label, expected_sha256)
    metadata, messages = prompt_user_messages(data, label)
    payload = cast(dict[str, object], metadata["payload"])
    session_id = payload.get("id")
    cwd = payload.get("cwd")
    started_at = metadata.get("timestamp")
    if session_id != expected_id or UUID_RE.fullmatch(str(session_id)) is None or not path.name.endswith(f"-{expected_id}.jsonl"):
        raise ContainmentError(f"{label} metadata, filename, and expected session id do not agree.")
    if not isinstance(cwd, str) or not isinstance(started_at, str):
        raise ContainmentError(f"{label} metadata lacks its working directory or timestamp.")
    _ = parse_datetime(started_at, f"{label} timestamp")
    matches = [message for message in messages if message == expected_prompt]
    if len(matches) != 1:
        raise ContainmentError(f"{label} does not contain exactly one initial user prompt matching the bound launch.")
    return SessionEvidence(file_evidence, expected_id, started_at, cwd, sha256(expected_prompt.encode()))


def audit_binding(args: Args) -> tuple[AuditBinding, SessionEvidence]:
    require_private_parent(args.failed_audit, "failed audit")
    audit_bytes, audit_file = read_regular_file(args.failed_audit, "failed audit", args.failed_audit_sha256)
    audit = json_no_duplicates(audit_bytes, "failed audit")
    if set(audit) != AUDIT_KEYS or audit.get("outcome") != "failed" or audit.get("status") != "":
        raise ContainmentError("failed audit does not have the exact legacy manager-rotation failure schema.")
    if not isinstance(audit.get("pane"), dict) or set(cast(dict[str, object], audit["pane"])) != PANE_KEYS:
        raise ContainmentError("failed audit pane identity is malformed.")
    if not isinstance(audit.get("launch"), dict) or set(cast(dict[str, object], audit["launch"])) != LAUNCH_KEYS:
        raise ContainmentError("failed audit launch identity is malformed.")
    pane_record = cast(dict[str, object], audit["pane"])
    launch = cast(dict[str, object], audit["launch"])
    if not isinstance(audit.get("target"), str) or not same_tmux_target(cast(str, audit["target"]), args.target):
        raise ContainmentError("failed audit target does not match the explicit containment target.")
    try:
        audit_root = Path(cast(str, audit["root"])).resolve(strict=True)
        old_cwd = Path(cast(str, pane_record["working_directory"])).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise ContainmentError("failed audit root or pane working directory is unavailable.") from exc
    if audit_root != args.root:
        raise ContainmentError("failed audit root does not match the canonical work-log root.")
    if (
        pane_record.get("pane_id") != args.expected_old_pane_id
        or pane_record.get("window_id") != args.expected_old_window_id
        or pane_record.get("pane_pid") != args.expected_old_pane_pid
        or launch.get("launch_pid") != args.expected_old_launch_pid
        or pane_record.get("canonical_target") != audit.get("target")
    ):
        raise ContainmentError("failed audit old pane/process identity does not match the explicit assertions.")
    launch_argv_raw = launch.get("launch_argv")
    if not isinstance(launch_argv_raw, list):
        raise ContainmentError("failed audit launch argv is malformed.")
    launch_argv_values = cast(list[object], launch_argv_raw)
    if not all(isinstance(value, str) for value in launch_argv_values):
        raise ContainmentError("failed audit launch argv is malformed.")
    launch_argv = tuple(cast(str, value) for value in launch_argv_values)
    if len(launch_argv) < 3 or Path(launch_argv[0]).name != "bunx" or launch_argv[1] not in SUPPORTED_CODEX_PACKAGES:
        raise ContainmentError("failed audit old process is not a supported Codex launch.")
    model = launch.get("model")
    effort = launch.get("reasoning_effort")
    if not isinstance(model, str) or not isinstance(effort, str) or model not in launch_argv or f'model_reasoning_effort="{effort}"' not in launch_argv:
        raise ContainmentError("failed audit launch model/effort does not match its argv.")
    old_prompt = launch_argv[-1]
    old_session = session_evidence(
        args.old_session_transcript,
        args.expected_old_session_id,
        old_prompt,
        "old Codex session transcript",
        args.old_session_transcript_sha256,
    )
    if Path(old_session.cwd).resolve(strict=True) != old_cwd:
        raise ContainmentError("old Codex session working directory does not match the failed audit.")
    prompt_path_raw = audit.get("prompt_path")
    if not isinstance(prompt_path_raw, str):
        raise ContainmentError("failed audit prompt path is malformed.")
    prompt_path = Path(prompt_path_raw)
    if not prompt_path.is_absolute() or prompt_path.parent != args.failed_audit.parent:
        raise ContainmentError("failed audit prompt is not a sibling in the owner-private rotation directory.")
    prompt_bytes, prompt_file = read_regular_file(prompt_path, "fresh manager prompt", args.fresh_prompt_sha256)
    try:
        _ = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContainmentError("fresh manager prompt is not UTF-8.") from exc
    error = audit.get("error")
    if not isinstance(error, str) or not error.startswith(FAILURE_PREFIX):
        raise ContainmentError("failed audit is not the exact post-startup watcher-setup failure.")
    error_log = Path(error.removeprefix(FAILURE_PREFIX))
    if error_log != args.watcher_failure_log or error_log.parent != args.failed_audit.parent.parent:
        raise ContainmentError("failed audit watcher log does not match the explicit sibling state evidence.")
    watcher_failure_log = validate_failure_log(args)
    fresh_command = audit.get("fresh_command")
    if not isinstance(fresh_command, str) or " resume " in f" {fresh_command.casefold()} " or prompt_path_raw not in fresh_command:
        raise ContainmentError("failed audit fresh command is malformed or attempts session resume.")
    recorded_at = audit.get("recorded_at")
    recorded = parse_datetime(recorded_at, "failed audit recorded_at")
    if parse_datetime(old_session.started_at, "old session timestamp") >= recorded:
        raise ContainmentError("old Codex session does not predate the failed rotation record.")
    old_pane = PaneIdentity(cast(str, pane_record["canonical_target"]), args.expected_old_pane_id, args.expected_old_window_id, args.expected_old_pane_pid, old_cwd)
    return (
        AuditBinding(
            audit_file,
            cast(str, recorded_at),
            cast(str, audit["target"]),
            str(audit_root),
            str(args.failed_audit.parent.parent),
            prompt_file,
            watcher_failure_log,
            old_pane,
            args.expected_old_launch_pid,
            sha256("\0".join(launch_argv).encode()),
            model,
            effort,
        ),
        old_session,
    )


def validate_failure_log(args: Args) -> FileEvidence:
    require_private_parent(args.watcher_failure_log, "watcher failure log")
    log_bytes, evidence = read_regular_file(args.watcher_failure_log, "watcher failure log", args.watcher_failure_log_sha256)
    try:
        lines = log_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContainmentError("watcher failure log is not UTF-8.") from exc
    expected_refusal = f"omo_pending_watch: pending watcher already running for root: {args.root}"
    duplicate_line = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} pending watcher duplicate-root refusal; stopping supervisor$")
    if not lines or len(lines) % 2 or any(lines[index] != expected_refusal or duplicate_line.fullmatch(lines[index + 1]) is None for index in range(0, len(lines), 2)):
        raise ContainmentError("watcher failure log contains anything other than exact duplicate-root refusal pairs.")
    return evidence


def process_stat(pid: int, proc_root: Path = PROC_ROOT) -> ProcessIdentity:
    try:
        raw_stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        after_name = raw_stat.rsplit(")", 1)[1].split()
        argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
        decoded = tuple(value.decode("utf-8", errors="strict") for value in argv if value)
        return ProcessIdentity(
            pid,
            int(after_name[1]),
            after_name[0],
            int(after_name[2]),
            int(after_name[3]),
            int(after_name[4]),
            int(after_name[19]),
            sha256("\0".join(decoded).encode()),
        )
    except (IndexError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ContainmentError(f"cannot bind process identity for PID {pid}: {exc}") from exc


def process_argv(pid: int, proc_root: Path = PROC_ROOT) -> tuple[str, ...]:
    try:
        return tuple(value.decode("utf-8", errors="strict") for value in (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0") if value)
    except (OSError, UnicodeDecodeError) as exc:
        raise ContainmentError(f"cannot read argv for PID {pid}: {exc}") from exc


def process_snapshot(proc_root: Path = PROC_ROOT) -> dict[int, ProcessIdentity]:
    result: dict[int, ProcessIdentity] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise ContainmentError(f"cannot inspect process table: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            identity = process_stat(int(entry.name), proc_root)
        except ContainmentError:
            continue
        if identity.state != "Z":
            result[identity.pid] = identity
    return result


def process_tree(root_pid: int, processes: dict[int, ProcessIdentity]) -> tuple[ProcessIdentity, ...]:
    owned = {root_pid}
    changed = True
    while changed:
        changed = False
        for process in processes.values():
            if process.ppid in owned and process.pid not in owned:
                owned.add(process.pid)
                changed = True
    if root_pid not in processes:
        raise ContainmentError(f"pane process PID {root_pid} disappeared during binding.")
    return tuple(processes[pid] for pid in sorted(owned) if pid in processes)


def stable_process_identity(value: ProcessIdentity) -> tuple[int, int, int, int, int, int, str]:
    """Exclude only the scheduler-controlled process state from identity."""

    return (
        value.pid,
        value.ppid,
        value.process_group,
        value.session,
        value.tty,
        value.start_ticks,
        value.argv_sha256,
    )


def command_line_matches_fresh_launch(argv: tuple[str, ...], prompt: str, audit: AuditBinding, expected_command: str) -> bool:
    return (
        len(argv) == 8
        and Path(argv[0]).name == expected_command
        and argv[1] == CODEX_PACKAGE
        and argv[2] == "--dangerously-bypass-approvals-and-sandbox"
        and argv[3:5] == ("--model", audit.model)
        and argv[5] == "--config"
        and argv[6] == f'model_reasoning_effort="{audit.reasoning_effort}"'
        and argv[7] == prompt.rstrip("\n")
    )


def task_ref(root: Path, task_file: Path) -> str:
    try:
        return task_file.relative_to(root).as_posix()
    except ValueError as exc:
        raise ContainmentError("task file is outside the canonical work-log root.") from exc


def paths_in_todo_line(root: Path, line: str) -> tuple[Path, ...]:
    result: list[Path] = []
    for match in TASK_RE.finditer(line):
        candidate = (root / match.group(1)).resolve(strict=False)
        if root in candidate.parents:
            result.append(candidate)
    return tuple(result)


def exact_current_todo_row(root: Path, task_file: Path, runat: str, todo_text: str) -> str:
    section = ""
    current_headers = 0
    matches: list[tuple[str, str]] = []
    for line in todo_text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
            if section == "current":
                current_headers += 1
            continue
        if task_file in paths_in_todo_line(root, line):
            matches.append((section, line))
    expected = f"{task_ref(root, task_file)} {runat}"
    if current_headers != 1 or matches != [("current", expected)]:
        raise ContainmentError("TODO must contain one exact current task row with the authoritative runat target.")
    return expected


def queue_sha256(items: tuple[str, ...]) -> str:
    return sha256(json.dumps(list(items), ensure_ascii=False, separators=(",", ":")).encode())


def task_binding(args: Args) -> TaskBinding:
    task_bytes, task_file = read_regular_file(args.task_file, "manager task", args.expected_task_sha256)
    try:
        task_text = task_bytes.decode("utf-8")
        metadata = parse_task_metadata(task_text, args.root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise ContainmentError(f"manager task metadata is invalid: {exc}") from exc
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != args.expected_blocker
        or metadata.tool != "codex"
        or not metadata.is_manager
        or not same_tmux_target(metadata.runat, args.target)
        or not same_tmux_target(metadata.managerat, args.expected_manager_target)
        or metadata.pending_task_items != args.expected_pending_items
        or has_pending_marker(task_text)
    ):
        raise ContainmentError("manager task status, blocker, target, parent, queue, or pending-marker state drifted.")
    owners = authoritative_active_target_task_paths(args.root, metadata.runat)
    if owners != (args.task_file,):
        raise ContainmentError(f"manager target does not have exactly one active task owner: {[str(owner) for owner in owners]}")
    todo_path = args.root / "TODO.md"
    todo_bytes, todo_file = read_regular_file(todo_path, "TODO index")
    try:
        todo_text = todo_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContainmentError("TODO index is not UTF-8.") from exc
    row = exact_current_todo_row(args.root, args.task_file, metadata.runat, todo_text)
    return TaskBinding(
        task_file,
        metadata.status,
        metadata.blocked_on,
        metadata.runat,
        metadata.managerat,
        metadata.pending_task_items,
        queue_sha256(metadata.pending_task_items),
        todo_file,
        "current",
        row,
        sha256(f"current:\n{row}\n".encode()),
    )


def watcher_lock_path(root: Path) -> Path:
    digest = sha256(os.fspath(root).encode())
    return Path("/tmp") / f"{LOCK_DIR_PREFIX}{os.getuid()}" / f"{digest}.lock"


def lock_holders(lock_info: os.stat_result, proc_locks: Path = PROC_LOCKS) -> tuple[int, ...]:
    holders: list[int] = []
    try:
        lines = proc_locks.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContainmentError(f"cannot inspect active kernel locks: {exc}") from exc
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"] or fields[-2:] != ["0", "EOF"]:
            continue
        device_inode = fields[5].split(":")
        if len(device_inode) != 3:
            continue
        try:
            major, minor, inode = int(device_inode[0], 16), int(device_inode[1], 16), int(device_inode[2])
            pid = int(fields[4])
        except ValueError:
            continue
        if (major, minor, inode) == (os.major(lock_info.st_dev), os.minor(lock_info.st_dev), lock_info.st_ino):
            holders.append(pid)
    return tuple(holders)


def option_values(argv: tuple[str, ...], option: str) -> tuple[str, ...]:
    values: list[str] = []
    for index, value in enumerate(argv[:-1]):
        if value == option:
            values.append(argv[index + 1])
    return tuple(values)


def watcher_proof(root: Path, proc_root: Path = PROC_ROOT, lock_path_override: Path | None = None) -> WatcherProof:
    """Prove one live supported watcher owns the exact canonical-root flock."""

    lock_path = watcher_lock_path(root) if lock_path_override is None else lock_path_override
    require_no_symlink_components(lock_path)
    parent = lock_path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ContainmentError("canonical pending-watcher lock directory is not owner-private.")
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except OSError as exc:
        raise ContainmentError(f"canonical pending-watcher lock is unavailable: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ContainmentError("canonical pending-watcher lock file is unsafe.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise ContainmentError("canonical pending-watcher lock is not held by a running watcher.")
        holders = lock_holders(info, proc_root / "locks")
        if len(holders) != 1:
            raise ContainmentError(f"canonical pending-watcher lock has {len(holders)} kernel owners, expected one.")
        pid = holders[0]
        process = process_stat(pid, proc_root)
        argv = process_argv(pid, proc_root)
        if process.state == "Z" or len(argv) < 4 or not Path(argv[0]).name.startswith("python"):
            raise ContainmentError("canonical pending-watcher lock owner is not one live Python watcher.")
        expected_script = (Path(__file__).resolve().parent / "omo_pending_watch.py").resolve(strict=True)
        try:
            actual_script = Path(argv[1]).resolve(strict=True)
        except OSError as exc:
            raise ContainmentError("canonical pending-watcher lock owner script is unavailable.") from exc
        declared_roots = option_values(argv, "--root")
        if actual_script != expected_script or len(declared_roots) != 1 or Path(declared_roots[0]).resolve(strict=True) != root:
            raise ContainmentError("pending-watcher lock owner does not run the supported helper for the canonical root.")
        matching_fds = 0
        for fd_path in (proc_root / str(pid) / "fd").iterdir():
            try:
                fd_info = fd_path.stat()
            except OSError:
                continue
            matching_fds += (fd_info.st_dev, fd_info.st_ino) == (info.st_dev, info.st_ino)
        if matching_fds != 1:
            raise ContainmentError("pending-watcher process does not hold exactly one descriptor for the canonical root lock.")
        rechecked = process_stat(pid, proc_root)
        if rechecked.state == "Z" or stable_process_identity(rechecked) != stable_process_identity(process) or lock_holders(info, proc_root / "locks") != (pid,):
            raise ContainmentError("pending-watcher process or root-lock ownership changed during proof.")
        return WatcherProof(str(root), declared_roots[0], str(lock_path), info.st_dev, info.st_ino, pid, process.start_ticks, process.argv_sha256)
    finally:
        os.close(descriptor)


def current_command(target: str) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise ContainmentError(f"cannot inspect current pane command: {result.stderr.strip()}")
    return result.stdout.strip()


def live_binding(args: Args, audit: AuditBinding) -> LiveBinding:
    pane = resolve_exact_pane(args.target)
    if (
        pane.pane_id != args.expected_old_pane_id
        or pane.window_id != args.expected_old_window_id
        or pane.pane_pid != args.expected_current_pane_pid
        or pane.pane_pid == args.expected_old_pane_pid
        or pane.working_directory != audit.old_pane.working_directory
        or current_command(pane.pane_id) != args.expected_current_command
    ):
        raise ContainmentError("current successor does not retain the asserted same-pane identity and new process.")
    processes = process_snapshot()
    pane_process = processes.get(pane.pane_pid)
    if pane_process is None or pane_process.session != pane.pane_pid or pane_process.process_group != pane.pane_pid:
        raise ContainmentError("current pane PID is not one live process-group and session leader.")
    argv = process_argv(pane.pane_pid)
    prompt_bytes, _ = read_regular_file(Path(audit.prompt.path), "fresh manager prompt", args.fresh_prompt_sha256)
    try:
        prompt = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContainmentError("fresh manager prompt is not UTF-8.") from exc
    if not command_line_matches_fresh_launch(argv, prompt, audit, args.expected_current_command):
        raise ContainmentError("current pane process argv is not the exact fresh launch recorded by the failed audit.")
    current_session = session_evidence(
        args.current_session_transcript,
        args.expected_current_session_id,
        prompt.rstrip("\n"),
        "current Codex session transcript",
    )
    if Path(current_session.cwd).resolve(strict=True) != pane.working_directory:
        raise ContainmentError("current Codex session working directory does not match the bound pane.")
    if parse_datetime(current_session.started_at, "current session timestamp") > parse_datetime(audit.recorded_at, "failed audit timestamp"):
        raise ContainmentError("current Codex session started after the failed audit was finalized.")
    return LiveBinding(pane, pane_process, process_tree(pane.pane_pid, processes), current_session, task_binding(args), watcher_proof(args.root))


def stable_session_identity(session: SessionEvidence) -> tuple[object, ...]:
    return (
        session.file.path,
        session.file.device,
        session.file.inode,
        session.session_id,
        session.started_at,
        session.cwd,
        session.launch_prompt_sha256,
    )


def revalidate_live_binding(args: Args, audit: AuditBinding, initial: LiveBinding) -> LiveBinding:
    current = live_binding(args, audit)
    if (
        current.pane != initial.pane
        or stable_process_identity(current.pane_process) != stable_process_identity(initial.pane_process)
        or tuple(stable_process_identity(process) for process in current.process_tree)
        != tuple(stable_process_identity(process) for process in initial.process_tree)
        or stable_session_identity(current.session) != stable_session_identity(initial.session)
        or current.task != initial.task
        or current.watcher != initial.watcher
    ):
        raise ContainmentError("pane, process, session, task, queue, TODO, ownership, or watcher binding changed before containment.")
    return current


@contextmanager
def manager_rotation_lock(state_dir: Path) -> Iterator[None]:
    require_private_parent(state_dir / "manager-rotation.lock", "manager rotation lock")
    lock_path = state_dir / "manager-rotation.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContainmentError("another manager rotation or recovery holds the state-directory lock.") from exc
        yield
    finally:
        os.close(descriptor)


def receipt_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()


def write_private_exclusive(path: Path, data: bytes) -> None:
    require_private_parent(path, "containment receipt")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def replace_private_exact(path: Path, expected: bytes, replacement: bytes) -> None:
    current, current_evidence = read_regular_file(path, "prepared containment receipt")
    if current != expected or current_evidence.mode != 0o600:
        raise ContainmentError("prepared containment receipt changed before finalization.")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        write_private_exclusive(temporary, replacement)
        rebound = path.lstat()
        if (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns) != (
            current_evidence.device,
            current_evidence.inode,
            current_evidence.size,
            current_evidence.mtime_ns,
        ):
            raise ContainmentError("prepared containment receipt path changed before atomic finalization.")
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def receipt_payload(
    args: Args,
    audit: AuditBinding,
    old_session: SessionEvidence,
    live: LiveBinding,
    state: str,
    *,
    contained: ProcessIdentity | None = None,
    current_session_file: FileEvidence | None = None,
    error: str = "",
) -> dict[str, object]:
    audit_record = asdict(audit)
    audit_record["old_pane"]["working_directory"] = str(audit.old_pane.working_directory)
    return {
        "version": CONTAINMENT_VERSION,
        "operation": "contain-failed-manager-rotation",
        "state": state,
        "claim": "legacy-audit-not-reconciled-post-failure-work-not-accepted",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "root": str(args.root),
        "target": args.target,
        "protected_targets": list(args.protected_targets),
        "protected_targets_sha256": queue_sha256(args.protected_targets),
        "failed_audit": audit_record,
        "old_session": asdict(old_session),
        "successor_pane": asdict(live.pane) | {"working_directory": str(live.pane.working_directory)},
        "successor_process": asdict(live.pane_process),
        "successor_process_tree": [asdict(process) for process in live.process_tree],
        "successor_session": asdict(live.session),
        "successor_session_final_file": asdict(current_session_file) if current_session_file is not None else None,
        "task": asdict(live.task),
        "watcher": asdict(live.watcher),
        "contained_process": asdict(contained) if contained is not None else None,
        "dispatch_state": "stopped-no-codex-process" if contained is not None else "containment-not-yet-executed",
        "error": error,
    }


def containment_condition(live: LiveBinding, expected_command: str) -> str:
    pane = live.pane
    return "#{&&:#{==:#{pane_id},%s},#{&&:#{==:#{window_id},%s},#{&&:#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{&&:#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}}}}" % (
        pane.pane_id,
        pane.window_id,
        pane.canonical_target,
        pane.pane_pid,
        expected_command,
    )


def guarded_inert_respawn(live: LiveBinding, expected_command: str) -> None:
    sleep_path_raw = shutil.which("sleep")
    if sleep_path_raw is None:
        raise ContainmentError("sleep is unavailable for the inert containment process.")
    sleep_path = Path(sleep_path_raw).resolve(strict=True)
    accepted = f"OMO_MANAGER_CONTAIN_ACCEPTED_{os.getpid()}_{time.monotonic_ns()}"
    rejected = f"OMO_MANAGER_CONTAIN_REJECTED_{os.getpid()}_{time.monotonic_ns()}"
    respawn = shlex.join(
        [
            "respawn-pane",
            "-k",
            "-t",
            live.pane.pane_id,
            "-c",
            str(live.pane.working_directory),
            f"exec {shlex.quote(str(sleep_path))} infinity",
        ]
    )
    success = f"{respawn} ; display-message -p {accepted}"
    failure = f"display-message -p {rejected}"
    result = subprocess.run(
        ["tmux", "if-shell", "-F", "-t", live.pane.canonical_target, containment_condition(live, expected_command), success, failure],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise ContainmentError("tmux rejected the exact same-pane/process containment guard.")


def verify_contained(args: Args, live: LiveBinding) -> ProcessIdentity:
    deadline = time.monotonic() + 5.0
    current: PaneIdentity | None = None
    while time.monotonic() < deadline:
        current = resolve_exact_pane(args.target)
        if current.pane_pid != live.pane.pane_pid and current_command(current.pane_id) == "sleep":
            break
        time.sleep(0.05)
    if (
        current is None
        or current.pane_id != live.pane.pane_id
        or current.window_id != live.pane.window_id
        or current.canonical_target != live.pane.canonical_target
        or current.working_directory != live.pane.working_directory
        or current.pane_pid == live.pane.pane_pid
        or current_command(current.pane_id) != "sleep"
    ):
        raise ContainmentError("containment did not preserve the pane/window while installing the inert process.")
    contained = process_stat(current.pane_pid)
    contained_argv = process_argv(current.pane_pid)
    if contained.state == "Z" or contained.session != contained.pid or contained.process_group != contained.pid or len(contained_argv) != 2 or Path(contained_argv[0]).name != "sleep" or contained_argv[1] != "infinity":
        raise ContainmentError("replacement pane process is not the exact inert sleep sentinel.")
    processes = process_snapshot()
    old_sessions = {process.session for process in live.process_tree}
    old_groups = {process.process_group for process in live.process_tree}
    old_ttys = {process.tty for process in live.process_tree if process.tty}
    survivors = [
        process.pid
        for process in processes.values()
        if process.pid != contained.pid
        and (process.session in old_sessions or process.process_group in old_groups or process.tty in old_ttys)
    ]
    if survivors:
        raise ContainmentError(f"old successor process group/session/tty still has live members after containment: {survivors}")
    if process_tree(contained.pid, processes) != (contained,):
        raise ContainmentError("inert containment process unexpectedly has a live child process.")
    return contained


def containment_locks(args: Args, state_dir: Path) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(manager_rotation_lock(state_dir))
    stack.enter_context(task_file_lock(args.root / ".omo-task-membership.lock"))
    stack.enter_context(task_target_lock(args.root, args.target))
    lock_paths = {args.task_file, args.root / "TODO.md"}
    for path in sorted(lock_paths, key=str):
        stack.enter_context(task_file_lock(path))
    return stack


# 🧑 Manager delegation: "provide a supported containment operation that leaves the task explicitly blocked and the pane harmless without raw tmux mutation."
def contain(args: Args) -> tuple[str, Path | None]:
    audit, old_session = audit_binding(args)
    state_dir = Path(audit.state_dir)
    require_private_parent(args.receipt_output, "containment receipt")
    if args.receipt_output.exists() or args.receipt_output.is_symlink():
        raise ContainmentError("containment receipt output must not already exist.")
    with containment_locks(args, state_dir):
        live = live_binding(args, audit)
        if parse_datetime(old_session.started_at, "old session timestamp") >= parse_datetime(live.session.started_at, "current session timestamp"):
            raise ContainmentError("old and current Codex session chronology is invalid.")
        if args.dry_run:
            _ = revalidate_live_binding(args, audit, live)
            return "containment-ready", None
        prepared_payload = receipt_payload(args, audit, old_session, live, "prepared")
        prepared = receipt_bytes(prepared_payload)
        write_private_exclusive(args.receipt_output, prepared)
        contained: ProcessIdentity | None = None
        try:
            _ = revalidate_live_binding(args, audit, live)
            guarded_inert_respawn(live, args.expected_current_command)
            contained = verify_contained(args, live)
            final_task = task_binding(args)
            final_watcher = watcher_proof(args.root)
            if final_task != live.task or final_watcher != live.watcher:
                raise ContainmentError("task, queue, TODO, ownership, or canonical watcher changed across containment.")
            _, final_session_file = read_regular_file(args.current_session_transcript, "contained successor session transcript")
            if (final_session_file.device, final_session_file.inode) != (live.session.file.device, live.session.file.inode):
                raise ContainmentError("successor session transcript was replaced across containment.")
        except Exception as exc:
            failed = receipt_bytes(receipt_payload(args, audit, old_session, live, "failed", contained=contained, error=str(exc)))
            try:
                replace_private_exact(args.receipt_output, prepared, failed)
            except Exception as receipt_error:
                try:
                    _ = print(f"containment receipt finalization also failed: {receipt_error}", file=sys.stderr)
                except (OSError, UnicodeError, ValueError):
                    pass
            raise
        complete = receipt_bytes(
            receipt_payload(
                args,
                audit,
                old_session,
                live,
                "complete",
                contained=contained,
                current_session_file=final_session_file,
            )
        )
        replace_private_exact(args.receipt_output, prepared, complete)
        return "contained", args.receipt_output


def main(argv: list[str] | None = None) -> int:
    try:
        result, receipt = contain(parse_args(sys.argv[1:] if argv is None else argv))
    except (ContainmentError, OSError, RotationError, TaskFrontmatterError, subprocess.SubprocessError) as exc:
        print(f"omo_manager_rotation_contain: {exc}", file=sys.stderr)
        return 2
    print(f"result: {result}")
    if receipt is not None:
        print(f"receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
