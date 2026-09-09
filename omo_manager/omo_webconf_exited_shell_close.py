#!/usr/bin/env python3
"""Close the exact completed WebConf worker left in its original shell."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_stop import WEBCONF_EXITED_CLOSE_OPERATION
from omo_manager.omo_codex_stop import bound_close_secret
from omo_manager.omo_codex_stop import close_bound_tmux_target
from omo_manager.omo_codex_stop import done_live_close_started_path
from omo_manager.omo_codex_stop import has_bound_close_proof
from omo_manager.omo_codex_stop import pane_id
from omo_manager.omo_codex_stop import path_entry_exists
from omo_manager.omo_codex_stop import process_start_ticks
from omo_manager.omo_codex_stop import promote_done_live_close_started
from omo_manager.omo_codex_stop import require_nonsymlink_directory
from omo_manager.omo_codex_stop import validate_exited_codex_shell_with_consumed_report
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_status import DONE_CLOSE_IN_PROGRESS
from omo_manager.omo_task_status import TaskFrontmatterError
from omo_manager.omo_task_status import authoritative_active_target_task_paths
from omo_manager.omo_task_status import finish_done_transaction
from omo_manager.omo_task_status import has_pending_marker
from omo_manager.omo_task_status import read_private_audit
from omo_manager.omo_task_status import reconcile_todo_text
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import replace_private_audit
from omo_manager.omo_task_status import reserve_private_audit
from omo_manager.omo_task_status import root_membership_lock
from omo_manager.omo_task_status import update_frontmatter_status

TASK_REF = Path("webconf_list_email.md")
TARGET = "dw:19"
MANAGER_TARGET = "wl:1"
SOURCE_LOCATOR = "manager_mail/85c5dff58359-1524.txt:1-3"
SOURCE_TEXT = "Subject: Re: sample thewebconf papers\n\nEmail me the paper title, authors, year, official URL, then URL that searches it in Google Scholar, in a list\n"
SOURCE_SHA256 = "5347e871809a360ea8a08bdb3a95ab1dbc91caefad64e3010618e1da9b8130b3"
REPORT_REPLAY_ID = "a3fca7e249315c3e739e68ac02a9ee7c522e600ee614cd0b8d71ea40fcb21f5e"
REPORT_COMMITMENT_SHA256 = "08523d5e1c778aac7e87d552a418d1e6c59d44b62f492c6767a24cca236637be"
REPORT_SHA256 = "f28dc6bcbde8a9ffa9f2090b12dfc88fa6db684dfec68b607d9eeff8b8ad1a20"
REPORT_COMMITMENT_PATH = Path(
    "/home/sichangheagent/.local/state/omo-manager/report-receipts/"
    "a3fca7e249315c3e739e68ac02a9ee7c522e600ee614cd0b8d71ea40fcb21f5e.commitment"
)
SESSION_ID = "01a08342-0b49-7de2-9bb0-fc885e92fd1a"
INCIDENT_TASK_SHA256 = "1215a3c225ee23090898e15cc4a5d44cb57090413b63afd526879f4bde93dc65"
INCIDENT_TODO_SHA256 = "f881ba07a5ea909e72bceeafc7db98477963187c7b6d7e3e0dbace7166c5a5d7"
INCIDENT_CAPTURE_SHA256 = "7a8f478eb5d5416cbaeff0265232066a4a22875fda28a13f22ccfc87a365faa2"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PANE_RE = re.compile(r"%[1-9][0-9]*")
TX_SCHEMA = "omo-webconf-exited-shell-close/v2"
EXPECTED_ROOT = Path("/ssd1/sichangheagent/work_logs")


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    expected_task_sha256: str
    expected_todo_sha256: str
    expected_pane_id: str
    expected_pane_pid: int
    expected_pane_start_ticks: int
    expected_capture_sha256: str
    report_commitment: Path
    report_commitment_sha256: str
    audit_output: Path


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-task-sha256", required=True)
    parser.add_argument("--expected-todo-sha256", required=True)
    parser.add_argument("--expected-pane-id", required=True)
    parser.add_argument("--expected-pane-pid", type=int, required=True)
    parser.add_argument("--expected-pane-start-ticks", type=int, required=True)
    parser.add_argument("--expected-capture-sha256", required=True)
    parser.add_argument("--report-commitment", type=Path, required=True)
    parser.add_argument("--report-commitment-sha256", required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("task_file", type=Path)
    parsed = parser.parse_args(argv)
    digests = (parsed.expected_task_sha256, parsed.expected_todo_sha256, parsed.expected_capture_sha256, parsed.report_commitment_sha256)
    if any(SHA256_RE.fullmatch(value) is None for value in digests):
        parser.error("all expected digests must be lowercase SHA-256 values")
    if tuple(digests[:3]) != (INCIDENT_TASK_SHA256, INCIDENT_TODO_SHA256, INCIDENT_CAPTURE_SHA256):
        parser.error("task, TODO, and capture digests must match the immutable WebConf release")
    if PANE_RE.fullmatch(parsed.expected_pane_id) is None or parsed.expected_pane_pid <= 1 or parsed.expected_pane_start_ticks <= 0:
        parser.error("pane id, PID, and start ticks must be exact positive identities")
    if parsed.task_file != TASK_REF:
        parser.error(f"this source-bound helper supports only {TASK_REF}")
    if not parsed.report_commitment.is_absolute() or not parsed.audit_output.is_absolute():
        parser.error("report commitment and audit paths must be absolute")
    if parsed.report_commitment.resolve() != REPORT_COMMITMENT_PATH:
        parser.error("report commitment must be the exact registered WebConf commitment path")
    return Args(parsed.root.resolve(), parsed.task_file, *digests[:2], parsed.expected_pane_id, parsed.expected_pane_pid, parsed.expected_pane_start_ticks, parsed.expected_capture_sha256, parsed.report_commitment.resolve(), parsed.report_commitment_sha256, parsed.audit_output.resolve())


def private_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(fd)
        payload = os.read(fd, 1_000_001)
        after = os.fstat(fd)
        current = path.lstat()
    except OSError as error:
        raise TaskFrontmatterError(f"could not bind {label}: {error}") from error
    finally:
        if fd is not None:
            os.close(fd)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if (
        identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or len(payload) != before.st_size
        or len(payload) > 1_000_000
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise TaskFrontmatterError(f"{label} changed or is not one exact owner-private file")
    return payload


def validate_private_parent(path: Path, label: str) -> None:
    try:
        parent = require_nonsymlink_directory(path.parent)
        info = parent.stat()
    except (OSError, RuntimeError) as error:
        raise TaskFrontmatterError(f"{label} parent is unavailable or linked: {error}") from error
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskFrontmatterError(f"{label} parent must be owner-private")


def validate_source_root(args: Args) -> bytes:
    try:
        configured = EXPECTED_ROOT.resolve(strict=True)
        actual_mail = require_nonsymlink_directory(args.root / "manager_mail")
    except (OSError, RuntimeError) as error:
        raise TaskFrontmatterError(f"Source-1524 authority root is unavailable: {error}") from error
    if configured != args.root.resolve(strict=True) or actual_mail != configured / "manager_mail":
        raise TaskFrontmatterError("Source-1524 is not in the configured real manager_mail directory")
    info = actual_mail.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskFrontmatterError("Source-1524 manager_mail directory is not owner-private")
    directory_fd: int | None = None
    source_fd: int | None = None
    try:
        directory_fd = os.open(actual_mail, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        directory_before = os.fstat(directory_fd)
        source_fd = os.open("85c5dff58359-1524.txt", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        source_before = os.fstat(source_fd)
        payload = os.read(source_fd, 1_000_001)
        source_after = os.fstat(source_fd)
        directory_after = os.fstat(directory_fd)
        current_directory = actual_mail.lstat()
    except OSError as error:
        raise TaskFrontmatterError(f"could not bind Source-1524 through its directory: {error}") from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    directory_identity = (directory_before.st_dev, directory_before.st_ino, directory_before.st_mtime_ns, directory_before.st_ctime_ns)
    source_identity = (source_before.st_dev, source_before.st_ino, source_before.st_size, source_before.st_mtime_ns, source_before.st_ctime_ns)
    if (
        directory_identity != (directory_after.st_dev, directory_after.st_ino, directory_after.st_mtime_ns, directory_after.st_ctime_ns)
        or directory_identity != (current_directory.st_dev, current_directory.st_ino, current_directory.st_mtime_ns, current_directory.st_ctime_ns)
        or source_identity != (source_after.st_dev, source_after.st_ino, source_after.st_size, source_after.st_mtime_ns, source_after.st_ctime_ns)
        or not stat.S_ISREG(source_before.st_mode)
        or source_before.st_uid != os.getuid()
        or source_before.st_nlink != 1
        or stat.S_IMODE(source_before.st_mode) != 0o600
        or len(payload) != source_before.st_size
        or len(payload) > 1_000_000
        or hashlib.sha256(payload).hexdigest() != SOURCE_SHA256
    ):
        raise TaskFrontmatterError("Source-1524 changed or lost its bound private directory/file identity")
    return payload


def validate_report_commitment(args: Args, task_path: Path) -> None:
    if args.report_commitment_sha256 != REPORT_COMMITMENT_SHA256:
        raise TaskFrontmatterError("report commitment is not the registered WebConf replay")
    payload = private_bytes(args.report_commitment, args.report_commitment_sha256, "report commitment")
    try:
        record = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TaskFrontmatterError(f"report commitment is not canonical JSON: {error}") from error
    if payload != (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode():
        raise TaskFrontmatterError("report commitment bytes are not canonical JSON")
    transfer = record.get("transfer") if isinstance(record, dict) else None
    authority = transfer.get("authority") if isinstance(transfer, dict) else None
    allocation = record.get("allocation") if isinstance(record, dict) else None
    submitted = allocation.get("file_at_submission") if isinstance(allocation, dict) else None
    if (
        record.get("schema") != "omo-report-transaction-commitment/v2"
        or record.get("replay_id") != REPORT_REPLAY_ID
        or not isinstance(authority, dict)
        or authority.get("kind") != "agent-originated"
        or authority.get("source_task") != str(task_path)
        or authority.get("producer_target") != TARGET
        or not isinstance(submitted, dict)
        or submitted.get("sha256") != REPORT_SHA256
    ):
        raise TaskFrontmatterError("report commitment does not bind the exact WebConf task and routed report")
    report = private_bytes(Path(str(allocation.get("file", ""))), str(submitted["sha256"]), "WebConf report")
    if not report.startswith(b"Completed the manager-routed addition to the existing Web Conference paper-list lane;"):
        raise TaskFrontmatterError("WebConf report does not contain the exact terminal completion")


def close_note() -> str:
    return f"(manager closed Codex agent after source-bound no-mail recovery; tmux target `{TARGET}`; session_id: `{SESSION_ID}`; report replay: `{REPORT_REPLAY_ID}`.)"


def canonical_text(path: Path, label: str) -> str:
    payload = path.read_bytes()
    if b"\r" in payload or not payload.endswith(b"\n"):
        raise TaskFrontmatterError(f"{label} is not canonical LF-terminated text")
    return payload.decode("utf-8")


def completed_texts(args: Args, task_path: Path, task_text: str, todo_text: str) -> tuple[str, str]:
    completed_task = update_frontmatter_status(f"{task_text.rstrip()}\n{close_note()}\n", "done", "", args.root)
    completed_todo = reconcile_todo_text(args.root, task_path, todo_text, TARGET, "previous", ("current",))
    return completed_task, completed_todo


def validate_source_task(args: Args, task_path: Path, task_text: str, todo_text: str, *, current_owner: bool = True) -> None:
    metadata = parse_task_metadata(task_text, args.root)
    source = validate_source_root(args)
    if source.replace(b"\r\n", b"\n").split(b"\n", 3)[:3] != SOURCE_TEXT.encode().split(b"\n", 3)[:3]:
        raise TaskFrontmatterError("Source-1524 Human request bytes do not match the embedded authority")
    envelope = f'<human_instruction authoritative="true" source="{SOURCE_LOCATOR}">\n{SOURCE_TEXT}</human_instruction>'
    report_record = f"private packet via omo_report.sh done receipt {REPORT_REPLAY_ID}"
    if (
        metadata is None
        or metadata.status != "blocked"
        or metadata.blocked_on != DONE_CLOSE_IN_PROGRESS
        or metadata.runat != TARGET
        or metadata.managerat != MANAGER_TARGET
        or metadata.is_manager
        or metadata.tool != "codex"
        or metadata.pending_task_items
        or has_pending_marker(task_text)
        or task_text.count(envelope) != 1
        or task_text.count(report_record) != 1
        or authoritative_active_target_task_paths(args.root, TARGET) != ((task_path,) if current_owner else ())
    ):
        raise TaskFrontmatterError("task lacks the exact completed source, report, queue, manager, or sole-owner binding")
    _ = reconcile_todo_text(args.root, task_path, todo_text, TARGET, "previous", ("current",))


def close_authority_text(args: Args, commitment: str) -> str:
    record = {
        "close_note": "",
        "close_proof_commitment": commitment,
        "completed_task_sha256": "",
        "manager_consumed_receipt_sha256": args.report_commitment_sha256,
        "manager_target": MANAGER_TARGET,
        "operation": WEBCONF_EXITED_CLOSE_OPERATION,
        "pane_id": args.expected_pane_id,
        "pane_pid": args.expected_pane_pid,
        "pane_start_ticks": args.expected_pane_start_ticks,
        "session_id": SESSION_ID,
        "state": "terminalized",
        "target": TARGET,
        "task": TASK_REF.as_posix(),
        "task_sha256": args.expected_task_sha256,
        "terminal_capture_sha256": args.expected_capture_sha256,
        "terminal_evidence_sha256": hashlib.sha256(REPORT_REPLAY_ID.encode()).hexdigest(),
        "todo_sha256": args.expected_todo_sha256,
        "version": "v2.0.0",
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


def validate_close_authority_file(
    audit_path: Path,
    commitment: str,
    target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    expected_audit_sha256: str,
    *,
    closed_identity: bool = False,
) -> None:
    """Reauthenticate the incident authority, including capture, in the close child."""

    payload = private_bytes(audit_path, expected_audit_sha256, "WebConf close authority")
    try:
        record = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("WebConf close authority is not canonical JSON") from error
    if (
        payload != (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        or not isinstance(record, dict)
        or record.get("operation") != WEBCONF_EXITED_CLOSE_OPERATION
        or record.get("state") != "terminalized"
        or record.get("target") != TARGET
        or record.get("manager_target") != MANAGER_TARGET
        or record.get("task") != TASK_REF.as_posix()
        or record.get("task_sha256") != INCIDENT_TASK_SHA256
        or record.get("todo_sha256") != INCIDENT_TODO_SHA256
        or record.get("manager_consumed_receipt_sha256") != REPORT_COMMITMENT_SHA256
        or record.get("terminal_capture_sha256") != INCIDENT_CAPTURE_SHA256
        or record.get("terminal_evidence_sha256") != hashlib.sha256(REPORT_REPLAY_ID.encode()).hexdigest()
        or record.get("session_id") != SESSION_ID
        or record.get("close_proof_commitment") != commitment
        or (target, expected_pane_id, expected_pane_pid, expected_pane_start_ticks)
        != (TARGET, record.get("pane_id"), record.get("pane_pid"), record.get("pane_start_ticks"))
    ):
        raise RuntimeError("WebConf close authority does not bind the exact incident")
    if not closed_identity:
        observed = validate_exited_codex_shell_with_consumed_report(TARGET, expected_pane_id, SESSION_ID, REPORT_REPLAY_ID)
        if observed != record["terminal_capture_sha256"]:
            raise RuntimeError("WebConf exited-shell capture changed inside the bound close child")


def transaction_record(args: Args, state: str, source_task: str, source_todo: str, completed_task: str, completed_todo: str, authority_path: Path, authority_sha256: str, secret: str) -> str:
    record = {
        "close_authority": str(authority_path),
        "close_authority_sha256": authority_sha256,
        "close_proof_secret": secret,
        "completed_task": completed_task,
        "completed_task_sha256": hashlib.sha256(completed_task.encode()).hexdigest(),
        "completed_todo": completed_todo,
        "completed_todo_sha256": hashlib.sha256(completed_todo.encode()).hexdigest(),
        "report_commitment": str(args.report_commitment),
        "report_commitment_sha256": args.report_commitment_sha256,
        "schema": TX_SCHEMA,
        "source_task": source_task,
        "source_task_sha256": args.expected_task_sha256,
        "source_todo": source_todo,
        "source_todo_sha256": args.expected_todo_sha256,
        "state": state,
        "target": TARGET,
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


def run(args: Args) -> int:
    task_path = args.root / args.task_file
    todo_path = args.root / "TODO.md"
    authority_path = args.audit_output.with_name(f".{args.audit_output.name}.close-authority")
    proof_path = authority_path.with_name(f".{authority_path.name}.owner-stopped")
    started_path = done_live_close_started_path(authority_path)
    try:
        validate_private_parent(args.audit_output, "audit")
        with root_membership_lock(args.root), task_target_lock(args.root, TARGET), ExitStack() as locks:
            for path in sorted({task_path, todo_path}, key=str):
                locks.enter_context(task_file_lock(path))
            validate_report_commitment(args, task_path)
            if args.audit_output.exists():
                raw_tx = read_private_audit(args.audit_output)
                record = json.loads(raw_tx)
                if not isinstance(record, dict) or record.get("schema") != TX_SCHEMA:
                    raise TaskFrontmatterError("existing WebConf transaction audit is invalid")
                source_task = str(record.get("source_task", ""))
                source_todo = str(record.get("source_todo", ""))
                completed_task = str(record.get("completed_task", ""))
                completed_todo = str(record.get("completed_todo", ""))
                secret = str(record.get("close_proof_secret", ""))
                commitment = hashlib.sha256(secret.encode()).hexdigest()
                authority = close_authority_text(args, commitment)
                authority_sha256 = hashlib.sha256(authority.encode()).hexdigest()
                expected_tx = transaction_record(args, str(record.get("state", "")), source_task, source_todo, completed_task, completed_todo, authority_path, authority_sha256, secret)
                if raw_tx != expected_tx or record.get("state") not in {"prepared", "shell-closed", "complete"}:
                    raise TaskFrontmatterError("existing WebConf transaction audit changed or is noncanonical")
                if hashlib.sha256(source_task.encode()).hexdigest() != args.expected_task_sha256 or hashlib.sha256(source_todo.encode()).hexdigest() != args.expected_todo_sha256:
                    raise TaskFrontmatterError("transaction source images do not match the immutable invocation")
                validate_source_task(
                    args,
                    task_path,
                    source_task,
                    source_todo,
                    current_owner=canonical_text(task_path, "task") == source_task,
                )
                if completed_texts(args, task_path, source_task, source_todo) != (completed_task, completed_todo):
                    raise TaskFrontmatterError("transaction completion images are not canonical")
                if not authority_path.exists():
                    if path_entry_exists(proof_path) or path_entry_exists(started_path):
                        raise TaskFrontmatterError("close authority disappeared after close artifacts were created")
                    reserve_private_audit(authority_path, authority)
                elif private_bytes(authority_path, authority_sha256, "close authority") != authority.encode():
                    raise TaskFrontmatterError("close authority bytes changed")
            else:
                task_before = task_path.stat()
                todo_before = todo_path.stat()
                task_bytes = task_path.read_bytes()
                todo_bytes = todo_path.read_bytes()
                if b"\r" in task_bytes or b"\r" in todo_bytes or not task_bytes.endswith(b"\n") or not todo_bytes.endswith(b"\n") or hashlib.sha256(task_bytes).hexdigest() != args.expected_task_sha256 or hashlib.sha256(todo_bytes).hexdigest() != args.expected_todo_sha256:
                    raise TaskFrontmatterError("task or TODO bytes do not match the canonical invocation")
                source_task = task_bytes.decode("utf-8")
                source_todo = todo_bytes.decode("utf-8")
                validate_source_task(args, task_path, source_task, source_todo)
                completed_task, completed_todo = completed_texts(args, task_path, source_task, source_todo)
                if pane_id(TARGET) != args.expected_pane_id or pane_id(args.expected_pane_id) != args.expected_pane_id or process_start_ticks(args.expected_pane_pid) != args.expected_pane_start_ticks:
                    raise TaskFrontmatterError("WebConf pane/process identity changed")
                if validate_exited_codex_shell_with_consumed_report(TARGET, args.expected_pane_id, SESSION_ID, REPORT_REPLAY_ID) != args.expected_capture_sha256:
                    raise TaskFrontmatterError("WebConf exited-shell capture changed")
                secret = secrets.token_hex(32)
                commitment = hashlib.sha256(secret.encode()).hexdigest()
                authority = close_authority_text(args, commitment)
                authority_sha256 = hashlib.sha256(authority.encode()).hexdigest()
                raw_tx = transaction_record(args, "prepared", source_task, source_todo, completed_task, completed_todo, authority_path, authority_sha256, secret)
                reserve_private_audit(args.audit_output, raw_tx)
                reserve_private_audit(authority_path, authority)

            current_task = canonical_text(task_path, "task")
            current_todo = canonical_text(todo_path, "TODO")
            if (current_task, current_todo) not in {(source_task, source_todo), (source_task, completed_todo), (completed_task, completed_todo)}:
                raise TaskFrontmatterError("task/TODO are not an exact source, partial, or completed transaction state")
            audit_sha256 = hashlib.sha256(authority.encode()).hexdigest()
            final_secret = bound_close_secret(proof_path, commitment, audit_sha256, WEBCONF_EXITED_CLOSE_OPERATION)
            started_secret = bound_close_secret(started_path, commitment, audit_sha256, WEBCONF_EXITED_CLOSE_OPERATION)
            absent = not pane_id(TARGET) and not pane_id(args.expected_pane_id) and process_start_ticks(args.expected_pane_pid) is None
            if final_secret:
                if started_secret or not absent:
                    raise TaskFrontmatterError("final close proof contradicts pane or started-marker state")
            elif started_secret and absent:
                promote_done_live_close_started(proof_path, authority_path, commitment, audit_sha256, TARGET, args.expected_pane_id, args.expected_pane_pid, args.expected_pane_start_ticks, WEBCONF_EXITED_CLOSE_OPERATION)
            else:
                if absent:
                    raise TaskFrontmatterError("pane disappeared without durable pre-kill evidence")

                def evidence_is_current() -> bool:
                    try:
                        return (
                            canonical_text(task_path, "task") == current_task
                            and canonical_text(todo_path, "TODO") == current_todo
                            and process_start_ticks(args.expected_pane_pid) == args.expected_pane_start_ticks
                            and validate_exited_codex_shell_with_consumed_report(TARGET, args.expected_pane_id, SESSION_ID, REPORT_REPLAY_ID) == args.expected_capture_sha256
                        )
                    except (OSError, RuntimeError):
                        return False

                close_bound_tmux_target(args.expected_pane_id, evidence_is_current, TARGET, args.expected_pane_id, str(proof_path), str(authority_path), secret, commitment, args.expected_pane_pid, args.expected_pane_start_ticks, evidence_is_current, WEBCONF_EXITED_CLOSE_OPERATION, audit_sha256)
            if not has_bound_close_proof(proof_path, commitment, audit_sha256, WEBCONF_EXITED_CLOSE_OPERATION) or path_entry_exists(started_path) or pane_id(TARGET) or pane_id(args.expected_pane_id) or process_start_ticks(args.expected_pane_pid) is not None:
                raise TaskFrontmatterError("WebConf close lacks final durable proof or exact absence")
            transaction_state = json.loads(raw_tx)["state"]
            if transaction_state == "complete" and (current_task, current_todo) != (completed_task, completed_todo):
                raise TaskFrontmatterError("complete WebConf audit contradicts reverted lifecycle files")
            if transaction_state == "prepared":
                shell_closed_tx = transaction_record(args, "shell-closed", source_task, source_todo, completed_task, completed_todo, authority_path, authority_sha256, secret)
                replace_private_audit(args.audit_output, raw_tx, shell_closed_tx)
                raw_tx = shell_closed_tx
            task_before = task_path.stat()
            todo_before = todo_path.stat()
            current_task = canonical_text(task_path, "task")
            current_todo = canonical_text(todo_path, "TODO")
            if (current_task, current_todo) == (source_task, source_todo):
                finish_done_transaction(args.root, task_path, completed_task, task_before, locked=True, todo_text=source_todo, prepared_todo=completed_todo, todo_before=todo_before)
            elif (current_task, current_todo) == (source_task, completed_todo):
                replace_if_unchanged_locked(task_path, completed_task, task_before)
            elif (current_task, current_todo) != (completed_task, completed_todo):
                raise TaskFrontmatterError("WebConf lifecycle transaction drifted before completion")
            if canonical_text(task_path, "task") != completed_task or canonical_text(todo_path, "TODO") != completed_todo:
                raise TaskFrontmatterError("WebConf lifecycle transaction did not publish exact completion")
            if json.loads(raw_tx)["state"] != "complete":
                replace_private_audit(args.audit_output, raw_tx, transaction_record(args, "complete", source_task, source_todo, completed_task, completed_todo, authority_path, authority_sha256, secret))
        print(f"Closed {TARGET}; session_id: {SESSION_ID}; no Human mail sent.")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, UnicodeError, TaskFrontmatterError) as error:
        print(f"omo_webconf_exited_shell_close.py: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
