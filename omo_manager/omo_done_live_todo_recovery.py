#!/usr/bin/env python3
"""Source-bound TODO-CAS recovery for one prepared done-live close."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError
import omo_manager.omo_codex_stop as codex_stop_module
import omo_manager.omo_task_status as task_status_module
import omo_manager.omo_task_lock as task_lock_module
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_status import Args as StatusArgs
from omo_manager.omo_task_status import DoneLiveCloseAudit
from omo_manager.omo_task_status import close_done_live_no_mail
from omo_manager.omo_task_status import done_live_close_started_path
from omo_manager.omo_task_status import done_live_pane_state
from omo_manager.omo_task_status import parse_done_live_close_audit
from omo_manager.omo_task_status import read_private_audit
from omo_manager.omo_task_status import render_done_live_close_audit
from omo_manager.omo_task_status import reserve_private_audit
from omo_manager.omo_task_status import root_membership_lock
from omo_manager.omo_task_status import validate_done_live_task
from omo_manager.omo_task_status import validate_done_live_todo
from omo_manager.omo_task_status import validate_done_live_ownership
from omo_manager.omo_task_status import validate_exited_codex_shell

SCHEMA = "omo-done-live-todo-cas-recovery/v1"
REVIEW_SCHEMA = "omo-done-live-todo-cas-recovery-review/v1"
ROOT = Path("/ssd1/sichangheagent/work_logs")
TASK_NAME = "dw_bodyswap_pr.md"
TARGET = "dw8:1"
MANAGER_TARGET = "dw:15"
TASK_SHA256 = "ce3727e729a1a2b0176aac7e7d5b3d8e2723fe5409ae372689d0daf685b1dd46"
OLD_TODO_SHA256 = "4e8cc7975a000dd1ca0c45406aff48b5173977c807c99dd27308b0d90f1dccb7"
PANE_ID = "%864"
PANE_PID = 2863234
PANE_START_TICKS = 27512569
SESSION_ID = "01a07faa-011d-7983-86bb-978cf5a25169"
TERMINAL_EVIDENCE = "4a48c297f58d81f3e263b889ce262af40b4c4589aa614e27878c910990982d7d"
ORIGINAL_AUDIT = Path("/tmp/config4-dw8-1-close.Zlzbqm/audit.json")
ORIGINAL_AUDIT_SHA256 = "30fca8550663c9dbc298f0e1d35033c0244d65ff24d26b2bec936ef6ee627ec7"
MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class Snapshot:
    path: str
    sha256: str
    size: int
    dev: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    uid: int
    nlink: int


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_snapshot(path: Path, label: str, *, private: bool = False) -> tuple[bytes, Snapshot]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TaskFrontmatterError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        data = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
    finally:
        os.close(fd)

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
            value.st_nlink,
        )

    if (
        identity(before) != identity(after)
        or identity(after) != identity(current)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or (private and stat.S_IMODE(before.st_mode) != 0o600)
        or len(data) != before.st_size
        or len(data) > MAX_BYTES
    ):
        raise TaskFrontmatterError(f"{label} changed or has an unsafe file identity.")
    return data, Snapshot(
        str(path.resolve(strict=True)),
        digest(data),
        len(data),
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        before.st_nlink,
    )


def snapshot_record(value: Snapshot) -> dict[str, object]:
    return {
        "ctime_ns": value.ctime_ns,
        "dev": value.dev,
        "inode": value.inode,
        "mode": value.mode,
        "mtime_ns": value.mtime_ns,
        "nlink": value.nlink,
        "path": value.path,
        "sha256": value.sha256,
        "size": value.size,
        "uid": value.uid,
    }


def parse_snapshot(value: object, label: str) -> Snapshot:
    if not isinstance(value, dict) or set(value) != {"ctime_ns", "dev", "inode", "mode", "mtime_ns", "nlink", "path", "sha256", "size", "uid"}:
        raise TaskFrontmatterError(f"{label} identity schema is invalid.")
    try:
        snapshot = Snapshot(
            str(value["path"]),
            str(value["sha256"]),
            int(value["size"]),
            int(value["dev"]),
            int(value["inode"]),
            int(value["mtime_ns"]),
            int(value["ctime_ns"]),
            int(value["mode"]),
            int(value["uid"]),
            int(value["nlink"]),
        )
    except (TypeError, ValueError) as exc:
        raise TaskFrontmatterError(f"{label} identity fields are invalid.") from exc
    if len(snapshot.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in snapshot.sha256):
        raise TaskFrontmatterError(f"{label} SHA-256 is invalid.")
    return snapshot


def assert_snapshot(expected: Snapshot, label: str, *, private: bool = False) -> bytes:
    data, observed = read_snapshot(Path(expected.path), label, private=private)
    if observed != expected:
        raise TaskFrontmatterError(f"{label} changed after preparation.")
    return data


def partition_todo(data: bytes) -> tuple[bytes, bytes, bytes]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("TODO is not UTF-8.") from exc
    expected = f"{TASK_NAME} {TARGET}"
    section = ""
    offsets: list[tuple[int, int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        elif TASK_NAME in line:
            offsets.append((cursor, cursor + len(line.encode()), section))
        cursor += len(line.encode())
    if len(offsets) != 1:
        raise TaskFrontmatterError("TODO recovery requires one exact task row.")
    start, end, row_section = offsets[0]
    row = data[start:end]
    if row_section != "previous" or row.decode("utf-8").rstrip("\r\n") != expected or not row.endswith(b"\n"):
        raise TaskFrontmatterError("TODO recovery requires the exact previous-row custody.")
    return data[:start], row, data[end:]


def status_args(todo_sha256: str, audit_output: Path) -> StatusArgs:
    return StatusArgs(
        ROOT,
        Path(TASK_NAME),
        "done",
        "",
        close_done_live_no_mail=True,
        active_target=TARGET,
        manager_target=MANAGER_TARGET,
        expected_task_sha256=TASK_SHA256,
        expected_todo_sha256=todo_sha256,
        expected_pane_id=PANE_ID,
        expected_pane_pid=PANE_PID,
        expected_pane_start_ticks=PANE_START_TICKS,
        expected_session_id=SESSION_ID,
        terminal_evidence=TERMINAL_EVIDENCE,
        audit_output=audit_output,
    )


def validate_original_audit(data: bytes) -> None:
    if digest(data) != ORIGINAL_AUDIT_SHA256:
        raise TaskFrontmatterError("original prepared audit digest is invalid.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("original prepared audit is not UTF-8.") from exc
    audit = parse_done_live_close_audit(status_args(OLD_TODO_SHA256, ORIGINAL_AUDIT), ROOT / TASK_NAME, text)
    if audit != DoneLiveCloseAudit("prepared"):
        raise TaskFrontmatterError("original audit is not the exact prepared transaction.")


def validate_output_path(path: Path, forbidden: set[Path], label: str) -> Path:
    candidate = path.resolve(strict=False)
    if not candidate.is_absolute() or candidate in forbidden:
        raise TaskFrontmatterError(f"{label} path overlaps immutable evidence.")
    try:
        parent = candidate.parent.resolve(strict=True)
        parent_state = parent.stat()
    except OSError as exc:
        raise TaskFrontmatterError(f"{label} directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent_state.st_mode) or parent_state.st_uid != os.getuid() or stat.S_IMODE(parent_state.st_mode) & 0o077:
        raise TaskFrontmatterError(f"{label} directory must be owner-private.")
    return candidate


def control_paths(path: Path) -> set[Path]:
    """Return one output and the close sidecar names derivable from it."""

    resolved = path.resolve(strict=False)
    return {
        resolved,
        resolved.with_name(f".{resolved.name}.owner-stopped"),
        done_live_close_started_path(resolved),
    }


def require_disjoint_controls(*paths: Path) -> None:
    """Reject output names that alias another output's close controls."""

    groups = [control_paths(path) for path in paths]
    if any(left & right for index, left in enumerate(groups) for right in groups[index + 1 :]):
        raise TaskFrontmatterError("recovery output paths or derived close controls overlap.")


def load_packet(path: Path, expected_sha256: str) -> dict[str, object]:
    data, _snapshot = read_snapshot(path.resolve(strict=True), "recovery packet", private=True)
    if digest(data) != expected_sha256:
        raise TaskFrontmatterError("recovery packet digest is invalid.")
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("recovery packet is not canonical JSON.") from exc
    if not isinstance(value, dict) or data != canonical(value):
        raise TaskFrontmatterError("recovery packet is not canonical JSON.")
    validate_packet(value)
    return value


def decode_partition(packet: dict[str, object]) -> tuple[bytes, bytes, bytes]:
    partition = packet.get("todo_partition")
    if not isinstance(partition, dict) or set(partition) != {"before_b64", "row_b64", "after_b64"}:
        raise TaskFrontmatterError("TODO partition schema is invalid.")
    try:
        before = base64.b64decode(str(partition["before_b64"]), validate=True)
        row = base64.b64decode(str(partition["row_b64"]), validate=True)
        after = base64.b64decode(str(partition["after_b64"]), validate=True)
    except (ValueError, TypeError) as exc:
        raise TaskFrontmatterError("TODO partition encoding is invalid.") from exc
    return before, row, after


def validate_packet(packet: dict[str, object]) -> None:
    expected_keys = {
        "schema",
        "root",
        "task",
        "target",
        "manager_target",
        "task_input",
        "todo_input",
        "todo_partition",
        "original_audit_input",
        "helper_input",
        "codex_stop_input",
        "task_lock_input",
        "task_status_input",
        "rebound_audit_output",
        "terminal",
        "packet_id",
    }
    if set(packet) != expected_keys or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("recovery packet schema is invalid.")
    unsigned = dict(packet)
    packet_id = unsigned.pop("packet_id", None)
    if packet_id != digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()):
        raise TaskFrontmatterError("recovery packet content identity is invalid.")
    if (
        packet.get("root") != str(ROOT)
        or packet.get("task") != TASK_NAME
        or packet.get("target") != TARGET
        or packet.get("manager_target") != MANAGER_TARGET
        or packet.get("terminal")
        != {
            "evidence": TERMINAL_EVIDENCE,
            "pane_id": PANE_ID,
            "pane_pid": PANE_PID,
            "pane_start_ticks": PANE_START_TICKS,
            "session_id": SESSION_ID,
        }
    ):
        raise TaskFrontmatterError("recovery packet incident binding is invalid.")
    task = parse_snapshot(packet.get("task_input"), "task")
    todo = parse_snapshot(packet.get("todo_input"), "TODO")
    audit = parse_snapshot(packet.get("original_audit_input"), "original audit")
    helper = parse_snapshot(packet.get("helper_input"), "recovery helper")
    codex_stop = parse_snapshot(packet.get("codex_stop_input"), "Codex stop helper")
    task_lock = parse_snapshot(packet.get("task_lock_input"), "task lock helper")
    task_status = parse_snapshot(packet.get("task_status_input"), "task-status helper")
    if (
        task.path != str((ROOT / TASK_NAME).resolve(strict=False))
        or task.sha256 != TASK_SHA256
        or todo.path != str((ROOT / "TODO.md").resolve(strict=False))
        or audit.path != str(ORIGINAL_AUDIT.resolve(strict=False))
        or audit.sha256 != ORIGINAL_AUDIT_SHA256
        or helper.path != str(Path(__file__).resolve(strict=True))
        or codex_stop.path != str(Path(codex_stop_module.__file__).resolve(strict=True))
        or task_lock.path != str(Path(task_lock_module.__file__).resolve(strict=True))
        or task_status.path != str(Path(task_status_module.__file__).resolve(strict=True))
    ):
        raise TaskFrontmatterError("recovery packet input paths or digests are invalid.")
    before, row, after = decode_partition(packet)
    todo_data = before + row + after
    if digest(todo_data) != todo.sha256 or partition_todo(todo_data) != (before, row, after):
        raise TaskFrontmatterError("recovery packet does not bind the exact TODO row and unrelated bytes.")
    forbidden = {Path(item.path) for item in (task, todo, audit, helper, codex_stop, task_lock, task_status)} | control_paths(ORIGINAL_AUDIT)
    validate_output_path(Path(str(packet.get("rebound_audit_output"))), forbidden, "rebound audit output")


def prepare(args: argparse.Namespace) -> None:
    helper = Path(__file__).resolve(strict=True)
    codex_stop_path = Path(codex_stop_module.__file__).resolve(strict=True)
    task_lock_path = Path(task_lock_module.__file__).resolve(strict=True)
    task_status_path = Path(task_status_module.__file__).resolve(strict=True)
    forbidden = {
        ROOT / TASK_NAME,
        ROOT / "TODO.md",
        helper,
        codex_stop_path,
        task_lock_path,
        task_status_path,
        *control_paths(ORIGINAL_AUDIT),
    }
    packet_output = validate_output_path(args.packet_output, forbidden, "packet output")
    rebound_output = validate_output_path(
        args.rebound_audit_output,
        forbidden | {packet_output},
        "rebound audit output",
    )
    if packet_output == rebound_output or packet_output.exists() or rebound_output.exists():
        raise TaskFrontmatterError("recovery outputs must be distinct and initially absent.")
    require_disjoint_controls(packet_output, rebound_output, ORIGINAL_AUDIT)
    task = ROOT / TASK_NAME
    todo = ROOT / "TODO.md"
    with root_membership_lock(ROOT), task_target_lock(ROOT, TARGET), ExitStack() as locks:
        for path in sorted({task, todo, ORIGINAL_AUDIT}, key=str):
            locks.enter_context(task_file_lock(path))
        task_data, task_identity = read_snapshot(task, "task")
        todo_data, todo_identity = read_snapshot(todo, "TODO")
        audit_data, audit_identity = read_snapshot(ORIGINAL_AUDIT, "original audit", private=True)
        validate_original_audit(audit_data)
        old_args = status_args(OLD_TODO_SHA256, ORIGINAL_AUDIT)
        validate_done_live_task(old_args, task_data.decode("utf-8"), DoneLiveCloseAudit("prepared"))
        validate_done_live_todo(ROOT, task, todo_data.decode("utf-8"), TARGET)
        before, row, after = partition_todo(todo_data)
        helper_data, helper_identity = read_snapshot(helper, "recovery helper")
        codex_stop_data, codex_stop_identity = read_snapshot(codex_stop_path, "Codex stop helper")
        task_lock_data, task_lock_identity = read_snapshot(task_lock_path, "task lock helper")
        task_status_data, task_status_identity = read_snapshot(task_status_path, "task-status helper")
        if (
            digest(helper_data) != helper_identity.sha256
            or digest(codex_stop_data) != codex_stop_identity.sha256
            or digest(task_lock_data) != task_lock_identity.sha256
            or digest(task_status_data) != task_status_identity.sha256
        ):
            raise AssertionError("helper identity changed while preparing recovery")
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(ROOT),
            "task": TASK_NAME,
            "target": TARGET,
            "manager_target": MANAGER_TARGET,
            "task_input": snapshot_record(task_identity),
            "todo_input": snapshot_record(todo_identity),
            "todo_partition": {
                "after_b64": base64.b64encode(after).decode(),
                "before_b64": base64.b64encode(before).decode(),
                "row_b64": base64.b64encode(row).decode(),
            },
            "original_audit_input": snapshot_record(audit_identity),
            "helper_input": snapshot_record(helper_identity),
            "codex_stop_input": snapshot_record(codex_stop_identity),
            "task_lock_input": snapshot_record(task_lock_identity),
            "task_status_input": snapshot_record(task_status_identity),
            "rebound_audit_output": str(rebound_output),
            "terminal": {
                "evidence": TERMINAL_EVIDENCE,
                "pane_id": PANE_ID,
                "pane_pid": PANE_PID,
                "pane_start_ticks": PANE_START_TICKS,
                "session_id": SESSION_ID,
            },
        }
        packet["packet_id"] = digest(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode())
        validate_packet(packet)
        reserve_private_audit(packet_output, canonical(packet).decode())


def review_record(packet: dict[str, object], packet_sha256: str) -> dict[str, object]:
    return {
        "schema": REVIEW_SCHEMA,
        "packet_id": packet["packet_id"],
        "packet_sha256": packet_sha256,
        "result": "PASS",
        "reviewed_guards": [
            "exact-incident-and-source-helper",
            "original-prepared-audit",
            "task-and-whole-todo-inputs",
            "sole-previous-row-and-all-unrelated-todo-bytes",
            "owner-private-distinct-rebound-audit",
            "execution-time-file-identity-and-race-rechecks",
        ],
    }


def review(args: argparse.Namespace) -> None:
    packet = load_packet(args.packet, args.packet_sha256)
    task = parse_snapshot(packet["task_input"], "task")
    todo = parse_snapshot(packet["todo_input"], "TODO")
    audit = parse_snapshot(packet["original_audit_input"], "original audit")
    helper = parse_snapshot(packet["helper_input"], "recovery helper")
    codex_stop = parse_snapshot(packet["codex_stop_input"], "Codex stop helper")
    task_lock = parse_snapshot(packet["task_lock_input"], "task lock helper")
    task_status = parse_snapshot(packet["task_status_input"], "task-status helper")
    forbidden = {
        *control_paths(args.packet.resolve(strict=True)),
        *control_paths(Path(str(packet["rebound_audit_output"]))),
        *control_paths(ORIGINAL_AUDIT),
        *(Path(item.path) for item in (task, todo, audit, helper, codex_stop, task_lock, task_status)),
    }
    output = validate_output_path(args.review_output, forbidden, "review output")
    if output.exists():
        raise TaskFrontmatterError("review output must be initially absent.")
    require_disjoint_controls(args.packet, Path(str(packet["rebound_audit_output"])), output, ORIGINAL_AUDIT)
    _ = assert_snapshot(task, "task")
    todo_data = assert_snapshot(todo, "TODO")
    validate_original_audit(assert_snapshot(audit, "original audit", private=True))
    _ = assert_snapshot(helper, "recovery helper")
    _ = assert_snapshot(codex_stop, "Codex stop helper")
    _ = assert_snapshot(task_lock, "task lock helper")
    _ = assert_snapshot(task_status, "task-status helper")
    if partition_todo(todo_data) != decode_partition(packet):
        raise TaskFrontmatterError("TODO changed from the reviewed row partition.")
    reserve_private_audit(output, canonical(review_record(packet, args.packet_sha256)).decode())


def validate_review(path: Path, expected_sha256: str, packet: dict[str, object], packet_sha256: str) -> None:
    data, _snapshot = read_snapshot(path.resolve(strict=True), "independent review", private=True)
    if digest(data) != expected_sha256:
        raise TaskFrontmatterError("independent review digest is invalid.")
    expected = review_record(packet, packet_sha256)
    if data != canonical(expected):
        raise TaskFrontmatterError("independent review does not PASS this exact recovery packet.")


def execute(args: argparse.Namespace) -> None:
    packet = load_packet(args.packet, args.packet_sha256)
    require_disjoint_controls(args.packet, args.review_report, Path(str(packet["rebound_audit_output"])), ORIGINAL_AUDIT)
    validate_review(args.review_report, args.review_sha256, packet, args.packet_sha256)
    task_identity = parse_snapshot(packet["task_input"], "task")
    todo_identity = parse_snapshot(packet["todo_input"], "TODO")
    audit_identity = parse_snapshot(packet["original_audit_input"], "original audit")
    helper_identity = parse_snapshot(packet["helper_input"], "recovery helper")
    codex_stop_identity = parse_snapshot(packet["codex_stop_input"], "Codex stop helper")
    task_lock_identity = parse_snapshot(packet["task_lock_input"], "task lock helper")
    task_status_identity = parse_snapshot(packet["task_status_input"], "task-status helper")
    task = Path(task_identity.path)
    todo = Path(todo_identity.path)
    rebound_output = Path(str(packet["rebound_audit_output"]))
    with root_membership_lock(ROOT), task_target_lock(ROOT, TARGET), ExitStack() as locks:
        for path in sorted({task, todo, ORIGINAL_AUDIT}, key=str):
            locks.enter_context(task_file_lock(path))
        todo_data = assert_snapshot(todo_identity, "TODO")
        validate_original_audit(assert_snapshot(audit_identity, "original audit", private=True))
        _ = assert_snapshot(helper_identity, "recovery helper")
        _ = assert_snapshot(codex_stop_identity, "Codex stop helper")
        _ = assert_snapshot(task_lock_identity, "task lock helper")
        _ = assert_snapshot(task_status_identity, "task-status helper")
        _ = load_packet(args.packet, args.packet_sha256)
        validate_review(args.review_report, args.review_sha256, packet, args.packet_sha256)
        if partition_todo(todo_data) != decode_partition(packet):
            raise TaskFrontmatterError("TODO row or unrelated bytes changed before recovery execution.")
        current_args = status_args(todo_identity.sha256, rebound_output)
        existing = read_private_audit(rebound_output)
        if existing is not None:
            existing_data, _existing_identity = read_snapshot(rebound_output, "rebound audit", private=True)
            if existing_data != existing.encode():
                raise TaskFrontmatterError("rebound audit changed while being authenticated.")
        recovery_audit = parse_done_live_close_audit(current_args, task, existing) if existing is not None else DoneLiveCloseAudit("prepared")
        task_data, current_task_identity = read_snapshot(task, "task")
        if existing is None and current_task_identity != task_identity:
            raise TaskFrontmatterError("task changed after TODO recovery preparation.")
        validate_done_live_task(current_args, task_data.decode("utf-8"), recovery_audit)
        validate_done_live_todo(ROOT, task, todo_data.decode("utf-8"), TARGET)
        validate_done_live_ownership(ROOT, task, TARGET)
        if existing is None:
            _ = assert_snapshot(task_identity, "task")
            _ = assert_snapshot(todo_identity, "TODO")
            validate_original_audit(assert_snapshot(audit_identity, "original audit", private=True))
            _ = assert_snapshot(helper_identity, "recovery helper")
            _ = assert_snapshot(codex_stop_identity, "Codex stop helper")
            _ = assert_snapshot(task_lock_identity, "task lock helper")
            _ = assert_snapshot(task_status_identity, "task-status helper")
            _ = load_packet(args.packet, args.packet_sha256)
            validate_review(args.review_report, args.review_sha256, packet, args.packet_sha256)
            if done_live_pane_state(current_args) != "live":
                raise TaskFrontmatterError("recovery requires the exact bound exited-shell pane.")
            capture_sha256 = validate_exited_codex_shell(TARGET, PANE_ID, SESSION_ID, TERMINAL_EVIDENCE)
            if len(capture_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in capture_sha256):
                raise TaskFrontmatterError("recovery did not authenticate one exact exited-shell capture.")
            _ = assert_snapshot(task_identity, "task")
            _ = assert_snapshot(todo_identity, "TODO")
            validate_original_audit(assert_snapshot(audit_identity, "original audit", private=True))
            _ = assert_snapshot(helper_identity, "recovery helper")
            _ = assert_snapshot(codex_stop_identity, "Codex stop helper")
            _ = assert_snapshot(task_lock_identity, "task lock helper")
            _ = assert_snapshot(task_status_identity, "task-status helper")
            _ = load_packet(args.packet, args.packet_sha256)
            validate_review(args.review_report, args.review_sha256, packet, args.packet_sha256)
            if done_live_pane_state(current_args) != "live":
                raise TaskFrontmatterError("recovery lost the exact bound exited-shell pane before publication.")
            if validate_exited_codex_shell(TARGET, PANE_ID, SESSION_ID, TERMINAL_EVIDENCE) != capture_sha256:
                raise TaskFrontmatterError("exited-shell capture changed before recovery publication.")
            rebound = render_done_live_close_audit(current_args, task, DoneLiveCloseAudit("prepared"))
            reserve_private_audit(rebound_output, rebound)
        task_stat = task.stat()
    result = close_done_live_no_mail(current_args, task, task_data.decode("utf-8"), task_stat)
    print(json.dumps({"closed_target": result[0], "session_id": result[1]}, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--review", action="store_true", dest="review_mode")
    modes.add_argument("--execute", action="store_true")
    result.add_argument("--packet-output", type=Path)
    result.add_argument("--rebound-audit-output", type=Path)
    result.add_argument("--packet", type=Path)
    result.add_argument("--packet-sha256", default="")
    result.add_argument("--review-output", type=Path)
    result.add_argument("--review-report", type=Path)
    result.add_argument("--review-sha256", default="")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.prepare:
            if args.packet_output is None or args.rebound_audit_output is None:
                raise TaskFrontmatterError("prepare requires packet and rebound-audit output paths.")
            prepare(args)
        elif args.review_mode:
            if args.packet is None or args.review_output is None or len(args.packet_sha256) != 64:
                raise TaskFrontmatterError("review requires packet, digest, and output paths.")
            review(args)
        else:
            if args.packet is None or args.review_report is None or len(args.packet_sha256) != 64 or len(args.review_sha256) != 64:
                raise TaskFrontmatterError("execute requires packet/review paths and digests.")
            execute(args)
    except (TaskFrontmatterError, OSError, UnicodeDecodeError) as exc:
        print(f"omo_done_live_todo_recovery.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
