#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Close one completed Codex predecessor while preserving its reused task record."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Iterable
from datetime import datetime
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import Args as CodexStopArgs
from omo_manager.omo_codex_stop import (
    SHELL_COMMANDS,
    bound_guarded_read,
    close_bound_tmux_target,
    codex_status,
    done_live_close_started_path,
    has_bound_close_proof,
    pane_id as resolve_pane_id,
    path_entry_exists,
    promote_done_live_close_started,
    stop as guarded_codex_stop,
)
from omo_manager.omo_repository_custody import (
    CustodyError,
    absolute_file_binding,
    canonical_json,
    directory_identity_from,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_report_receipt import bound_receipt_id
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, canonical_target, parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths

SCHEMA = "omo-stale-predecessor-close/v1"
SOURCE1485_SCHEMA = "omo-stale-predecessor-close/v2"
REVIEW_SCHEMA = "omo-stale-predecessor-close-review/v1"
# 🧑 "Continue until each item is complete or cancelled."
OPERATION = "stale-predecessor-no-mail-close"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PANE_RE = re.compile(r"^%[0-9]+$")
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
MAX_FILE_BYTES = 4 * 1024 * 1024
SOURCE1485_ROOT_AUDIT_SHA256 = "dd2cd04c1c6cd6c4050c7cd537d893e3c24aec45c504c1db3dbe0e4c0c792f2b"
SOURCE1485_ROOT_AUDIT_PATH = Path("/tmp/config4-source1485-root.83gryy/audit.json")
PACKET_KEYS = {
    "schema",
    "root",
    "task",
    "todo",
    "manager_task",
    "manager_target",
    "predecessor_target",
    "predecessor_pane",
    "predecessor_session_id",
    "protected_target",
    "protected_pane",
    "protected_session_id",
    "task_sha256",
    "todo_sha256",
    "manager_task_sha256",
    "consumed_export",
    "consumed_export_sha256",
    "predecessor_running_base64",
    "predecessor_running_sha256",
    "predecessor_done_base64",
    "predecessor_done_sha256",
    "report_replay_id",
    "report_attestation_id",
    "helper",
    "helper_sha256",
    "close_proof_secret",
    "close_proof_commitment",
    "audit",
    "inputs",
    "binding_id",
}
SOURCE1485_PACKET_KEYS = PACKET_KEYS | {
    "historical_manager_task",
    "historical_manager_target",
    "source1485_root_audit",
    "source1485_root_audit_sha256",
}
AUDIT_KEYS = {
    "schema",
    "operation",
    "state",
    "root",
    "task",
    "todo",
    "manager_task",
    "audit",
    "manager_target",
    "predecessor_target",
    "predecessor_pane",
    "predecessor_session_id",
    "protected_target",
    "protected_pane",
    "protected_session_id",
    "task_sha256",
    "todo_sha256",
    "manager_task_sha256",
    "consumed_export",
    "consumed_export_sha256",
    "predecessor_running_sha256",
    "predecessor_done_sha256",
    "report_replay_id",
    "report_attestation_id",
    "helper",
    "helper_sha256",
    "close_proof_commitment",
    "inputs",
    "packet_sha256",
    "binding_id",
}
SOURCE1485_AUDIT_KEYS = AUDIT_KEYS | {
    "historical_manager_task",
    "historical_manager_target",
    "source1485_root_audit",
    "source1485_root_audit_sha256",
}


@dataclass(frozen=True)
class PanePin:
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_private(path: Path, expected_sha256: str, label: str) -> bytes:
    if not path.is_absolute() or SHA256_RE.fullmatch(expected_sha256) is None:
        raise TaskFrontmatterError(f"{label} identity is invalid.")
    data, identity, _ancestors = absolute_file_binding(path, label, private=True)
    if identity.size_bytes > MAX_FILE_BYTES or sha256(data) != expected_sha256:
        raise TaskFrontmatterError(f"{label} changed.")
    return data


def object_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TaskFrontmatterError(f"{label} is not an object.")
    return {str(key): item for key, item in value.items()}


def canonical_object(data: bytes, label: str) -> dict[str, object]:
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError(f"{label} is not JSON.") from exc
    record = object_map(value, label)
    if canonical_json(record) != data:
        raise TaskFrontmatterError(f"{label} is not canonical JSON.")
    return record


def validate_source1485_manager_transition(record: dict[str, object]) -> tuple[Path, str, Path]:
    """Bind the exact historical-to-current manager migration used by Source 1485."""

    root = Path(str(record.get("root", "")))
    historical_manager = Path(str(record.get("historical_manager_task", "")))
    current_manager = Path(str(record.get("manager_task", "")))
    audit_path = Path(str(record.get("source1485_root_audit", "")))
    historical_target = str(record.get("historical_manager_target", ""))
    current_target = str(record.get("manager_target", ""))
    audit_sha256 = str(record.get("source1485_root_audit_sha256", ""))
    if (
        record.get("schema") != SOURCE1485_SCHEMA
        or not root.is_absolute()
        or not historical_manager.is_absolute()
        or not current_manager.is_absolute()
        or audit_path != SOURCE1485_ROOT_AUDIT_PATH
        or audit_sha256 != SOURCE1485_ROOT_AUDIT_SHA256
    ):
        raise TaskFrontmatterError("Source-1485 manager transition binding is invalid.")
    audit = canonical_object(read_private(audit_path, audit_sha256, "Source-1485 root audit"), "Source-1485 root audit")
    topology = object_map(audit.get("source1485_topology"), "Source-1485 topology")
    rows = topology.get("rows")
    if not isinstance(rows, list):
        raise TaskFrontmatterError("Source-1485 manager transition evidence is invalid.")
    manager_rows = [object_map(item, "Source-1485 topology row") for item in rows if isinstance(item, dict) and item.get("task") == audit.get("successor_task")]
    files = audit.get("files")
    if not isinstance(files, list):
        raise TaskFrontmatterError("Source-1485 manager transition evidence is invalid.")
    successor_files = [object_map(item, "Source-1485 file") for item in files if isinstance(item, dict) and item.get("task") == audit.get("successor_task")]
    if len(manager_rows) != 1 or len(successor_files) != 1:
        raise TaskFrontmatterError("Source-1485 manager transition evidence is invalid.")
    manager_row, successor_file = manager_rows[0], successor_files[0]
    encoded_after = successor_file.get("after")
    try:
        successor_after = base64.b64decode(str(encoded_after), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TaskFrontmatterError("Source-1485 manager transition evidence is invalid.") from exc
    if (
        audit.get("state") != "committed"
        or audit.get("operation") != "manager-replace"
        or audit.get("root") != str(root)
        or audit.get("old_task") != "dw_manager.md"
        or canonical_target(str(audit.get("old_target", ""))) != "dw:0"
        or audit.get("successor_task") != "dw_root_new.md"
        or canonical_target(str(audit.get("new_target", ""))) != "dw:15"
        or canonical_target(str(audit.get("parent_target", ""))) != "config:1"
        or historical_manager != root / "dw_manager.md"
        or canonical_target(historical_target) != "dw:0"
        or current_manager != root / "dw_root_new.md"
        or canonical_target(current_target) != "dw:15"
        or topology.get("root_task") != "dw_root_new.md"
        or canonical_target(str(topology.get("root_target", ""))) != "dw:15"
        or canonical_target(str(topology.get("parent_target", ""))) != "config:1"
        or manager_row.get("is_manager") is not True
        or manager_row.get("tool") != "codex"
        or canonical_target(str(manager_row.get("runat", ""))) != "dw:15"
        or canonical_target(str(manager_row.get("managerat", ""))) != "config:1"
        or successor_file.get("before") is not None
        or sha256(successor_after) != manager_row.get("sha256")
    ):
        raise TaskFrontmatterError("Source-1485 manager transition evidence is invalid.")
    return historical_manager, historical_target, audit_path


def bound_id(record: dict[str, object], field: str, label: str) -> str:
    unsigned = dict(record)
    observed = unsigned.pop(field, None)
    expected = bound_receipt_id(unsigned)
    if observed != expected:
        raise TaskFrontmatterError(f"{label} {field} is invalid.")
    return expected


def target_identity(target: str) -> tuple[str, int, int]:
    pane = exact_pane_id(target)
    if not pane:
        return "", 0, 0
    raw = bound_guarded_read(target, pane, ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_pid}"]).strip()
    observed, separator, raw_pid = raw.partition("|")
    if not separator or observed != pane or not raw_pid.isdecimal():
        raise TaskFrontmatterError("live target process identity is malformed.")
    pid = int(raw_pid)
    ticks = process_start_ticks(pid)
    if ticks is None or exact_pane_id(target) != pane:
        raise TaskFrontmatterError("live target process identity changed.")
    final = bound_guarded_read(target, pane, ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_pid}"], pid).strip()
    if final != raw or process_start_ticks(pid) != ticks:
        raise TaskFrontmatterError("live target process identity changed.")
    return pane, pid, ticks


def current_pin(pin: PanePin) -> bool:
    try:
        return target_identity(pin.target) == (pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
    except (OSError, RuntimeError, TaskFrontmatterError):
        return False


def pin_is_absent(pin: PanePin) -> bool:
    """Require the symbolic pane, numeric pane, and process identity to be absent."""

    return exact_pane_id(pin.target) == "" and resolve_pane_id(pin.pane_id) == "" and process_start_ticks(pin.pane_pid) is None


def pinned_current_command(pin: PanePin) -> str:
    """Read the command only while both symbolic and numeric pane pins match."""

    if not current_pin(pin):
        raise TaskFrontmatterError("live target process identity changed.")
    command = bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["display-message", "-p", "-t", pin.target, "#{pane_current_command}"],
        pin.pane_pid,
    ).strip()
    if not current_pin(pin):
        raise TaskFrontmatterError("live target process identity changed.")
    return command


def validate_ready_predecessor(pin: PanePin) -> None:
    if not current_pin(pin) or codex_status(pin.target) != "ready":
        raise TaskFrontmatterError("completed predecessor resumed or changed.")


def proc_fields(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, int]:
    try:
        fields = (proc_root / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError) as exc:
        raise TaskFrontmatterError("Codex process identity is unavailable.") from exc
    try:
        return int(fields[1]), int(fields[19])
    except (IndexError, ValueError) as exc:
        raise TaskFrontmatterError("Codex process identity is malformed.") from exc


def descendant_pids(root_pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    parents: dict[int, int] = {}
    for child in proc_root.iterdir():
        if child.name.isdecimal():
            try:
                parents[int(child.name)] = proc_fields(int(child.name), proc_root)[0]
            except TaskFrontmatterError:
                continue
    result: set[int] = set()
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if child not in result and (parent == root_pid or parent in result):
                result.add(child)
                changed = True
    return result


def session_from_process(pin: PanePin, proc_root: Path = Path("/proc")) -> str:
    codex_processes: list[int] = []
    for pid in descendant_pids(pin.pane_pid, proc_root):
        try:
            executable = os.readlink(proc_root / str(pid) / "exe")
            command = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if Path(executable.removesuffix(" (deleted)")).name == "codex" and b"--dangerously-bypass-approvals-and-sandbox" in command:
            codex_processes.append(pid)
    if len(codex_processes) != 1:
        raise TaskFrontmatterError("live pane does not contain one exact primary Codex process.")
    pid = codex_processes[0]
    _parent, start_ticks = proc_fields(pid, proc_root)
    try:
        boot_time_s = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
        ticks_per_s = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError) as exc:
        raise TaskFrontmatterError("Codex process start time is unavailable.") from exc
    process_started_s = boot_time_s + start_ticks / ticks_per_s
    sessions: list[tuple[float, str]] = []
    fd_root = proc_root / str(pid) / "fd"
    for descriptor in fd_root.iterdir():
        try:
            destination = descriptor.readlink()
        except OSError:
            continue
        match = re.fullmatch(
            r".*/rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-([0-9a-f-]{36})\.jsonl",
            str(destination),
        )
        if match is None or SESSION_RE.fullmatch(match.group(2)) is None:
            continue
        created_s = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").astimezone().timestamp()
        delta_s = abs(created_s - process_started_s)
        if delta_s <= 120:
            sessions.append((delta_s, match.group(2)))
    if not sessions:
        raise TaskFrontmatterError("primary Codex process has no launch-time rollout binding.")
    sessions.sort()
    if len(sessions) > 1 and sessions[0][0] == sessions[1][0]:
        raise TaskFrontmatterError("primary Codex rollout identity is ambiguous.")
    return sessions[0][1].lower()


def current_session_id(pin: PanePin, protected: tuple[PanePin, ...]) -> str:
    def stable() -> bool:
        return current_pin(pin) and all(current_pin(item) for item in protected)

    def pre_input_check() -> None:
        if not stable():
            raise TaskFrontmatterError("a protected target changed before session query.")

    pre_input_check()
    session_id = session_from_process(pin)
    pre_input_check()
    if SESSION_RE.fullmatch(session_id) is None:
        raise TaskFrontmatterError("live Codex session id could not be resolved.")
    return session_id.lower()


def predecessor_snapshots(task_data: bytes, root: Path, predecessor_target: str) -> tuple[bytes, bytes]:
    try:
        task_text = task_data.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("protected task is not UTF-8.") from exc
    metadata = parse_task_metadata(task_text, root)
    parts = task_data.split(b"---\n", 2)
    if metadata is None or metadata.status not in {"running", "long_running"} or metadata.runat == predecessor_target or metadata.is_manager or metadata.tool != "codex" or len(parts) != 3:
        raise TaskFrontmatterError("protected task is not one active non-manager successor record.")
    body = parts[2]
    header = (f"---\nversion: v1.0.0\nstatus: running\nrunat: {predecessor_target}\ntool: codex\nmanagerat: {metadata.managerat}\nis_manager: false\npending_task_items: []\n---\n").encode()
    return header, body


def reconstruct_predecessor(
    task_data: bytes,
    root: Path,
    predecessor_target: str,
    running_sha256: str,
    running_size: int,
    done_sha256: str,
    done_size: int,
) -> tuple[bytes, bytes]:
    header, body = predecessor_snapshots(task_data, root, predecessor_target)
    candidates: list[bytes] = []
    for end in [match.end() for match in re.finditer(rb"\n", body)]:
        candidate = header + body[:end]
        if len(candidate) == running_size and sha256(candidate) == running_sha256:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise TaskFrontmatterError("active record does not preserve one exact completed predecessor prefix.")
    running = candidates[0]
    done = running.replace(b"status: running\n", b"status: done\n", 1)
    if len(done) != done_size or sha256(done) != done_sha256:
        raise TaskFrontmatterError("reconstructed predecessor done transition is invalid.")
    return running, done


def validate_consumed_export(
    export_path: Path,
    export_sha256: str,
    task_path: Path,
    task_data: bytes,
    root: Path,
    predecessor_target: str,
    manager_target: str,
) -> tuple[dict[str, object], bytes, bytes, Path, tuple[Path, ...]]:
    export = canonical_object(read_private(export_path, export_sha256, "consumed export"), "consumed export")
    if set(export) != {"attestation", "export_id", "schema", "verification"} or export.get("schema") != "omo-report-consumed-export/v1":
        raise TaskFrontmatterError("consumed export schema is invalid.")
    _ = bound_id(export, "export_id", "consumed export")
    verification = object_map(export["verification"], "consumed export verification")
    attestation = object_map(export["attestation"], "consumed export attestation")
    _ = bound_id(attestation, "attestation_id", "consumed export attestation")
    transfer = object_map(attestation.get("transfer_receipt"), "consumed transfer")
    archive = object_map(attestation.get("archive_custody"), "predecessor custody")
    provenance = object_map(archive.get("git_provenance"), "predecessor transition")
    input_info = object_map(attestation.get("input"), "consumed input")
    consumption = object_map(attestation.get("consumption_evidence"), "watcher consumption")
    manager_path = Path(str(transfer.get("receiver", "")))
    report_path = Path(str(verification.get("message_file", "")))
    commitment_path = Path(str(transfer.get("commitment_path", "")))
    running_sha = str(provenance.get("committed_running_sha256", ""))
    done_sha = str(provenance.get("current_done_sha256", ""))
    running_size = provenance.get("committed_running_size_bytes")
    done_size = provenance.get("current_done_size_bytes")
    if (
        attestation.get("terminal") is not True
        or attestation.get("accepted") is not False
        or attestation.get("reason") != "manager watcher consumed report; acceptance receipt unavailable"
        or attestation.get("recovery_residue") != []
        or archive.get("schema") != "omo-report-terminal-task-custody/v1"
        or provenance.get("schema") != "omo-report-terminal-task-transition/v1"
        or archive.get("original_task") != str(task_path)
        or archive.get("task") != str(task_path)
        or archive.get("task_ref") != task_path.relative_to(root).as_posix()
        or provenance.get("task_ref") != task_path.relative_to(root).as_posix()
        or provenance.get("todo_previous_row") != f"{task_path.relative_to(root).as_posix()} {predecessor_target}"
        or not isinstance(running_size, int)
        or not isinstance(done_size, int)
        or SHA256_RE.fullmatch(running_sha) is None
        or SHA256_RE.fullmatch(done_sha) is None
        or archive.get("task_sha256") != done_sha
        or archive.get("todo") != str(root / "TODO.md")
        or archive.get("todo_reference_count") != 1
        or transfer.get("schema") != "omo-report-transfer-receipt/v1"
        or verification.get("root") != str(root)
        or verification.get("task") != str(task_path)
        or verification.get("producer_target") != predecessor_target
        or verification.get("requested_manager_target") != manager_target
        or verification.get("resolved_manager_target") != manager_target
        or verification.get("archived_task") is not True
        or verification.get("archived_task_path") != str(task_path)
        or verification.get("recovery_replay_id") != attestation.get("replay_id")
        or transfer.get("replay_id", attestation.get("replay_id")) != attestation.get("replay_id")
        or not manager_path.is_absolute()
        or not report_path.is_absolute()
        or not commitment_path.is_absolute()
    ):
        raise TaskFrontmatterError("consumed export predecessor binding is inconsistent.")
    running, done = reconstruct_predecessor(task_data, root, predecessor_target, running_sha, running_size, done_sha, done_size)
    report_data, report_identity, _ = absolute_file_binding(report_path, "historical report", private=True)
    if sha256(report_data) != input_info.get("sha256") or len(report_data) != input_info.get("size_bytes"):
        raise TaskFrontmatterError("historical report differs from the consumed input.")
    pointer = object_map(transfer.get("queue_item"), "consumed queue item").get("pointer")
    if not isinstance(pointer, str):
        raise TaskFrontmatterError("consumed envelope pointer is invalid.")
    envelope_path = Path(pointer.split()[-1].rstrip(")"))
    if not envelope_path.is_absolute():
        raise TaskFrontmatterError("consumed envelope path is invalid.")
    envelope_data, _envelope_identity, _ = absolute_file_binding(envelope_path, "historical report envelope", private=True)
    header, separator, body = envelope_data.partition(b"message:\n")
    if not separator or body != report_data or f"tmux={predecessor_target} ".encode() not in header:
        raise TaskFrontmatterError("historical report envelope does not bind the predecessor.")
    commitment_data, _commitment_identity, _ = absolute_file_binding(commitment_path, "transaction commitment", private=True)
    commitment = canonical_object(commitment_data, "transaction commitment")
    commitment_id = commitment.pop("commitment_id", None)
    if commitment_id != transfer.get("commitment_id") or commitment_id != bound_receipt_id(commitment):
        raise TaskFrontmatterError("transaction commitment identity is invalid.")
    committed_transfer = object_map(commitment.get("transfer"), "committed transfer")
    expected_transfer = dict(committed_transfer)
    expected_transfer["commitment_id"] = commitment_id
    expected_transfer["transfer_id"] = bound_receipt_id(expected_transfer)
    if transfer != expected_transfer:
        raise TaskFrontmatterError("consumed transfer differs from its commitment.")
    preflight = object_map(commitment.get("preflight"), "report preflight")
    records = object_map(preflight.get("records"), "report records")
    allocation = object_map(commitment.get("allocation"), "report allocation")
    submitted = object_map(allocation.get("file_at_submission"), "submitted report")
    if (
        records.get("producer") != str(task_path)
        or records.get("manager") != str(manager_path)
        or records.get("private_envelope") != str(envelope_path)
        or records.get("transaction_commitment") != str(commitment_path)
        or allocation.get("file") != str(report_path)
        or submitted.get("sha256") != sha256(report_data)
        or submitted.get("size") != report_identity.size_bytes
    ):
        raise TaskFrontmatterError("committed report records are inconsistent.")
    ledger_path = Path(str(consumption.get("state", "")))
    ledger_data, _ledger_identity, _ = absolute_file_binding(ledger_path, "watcher consumption ledger", private=True)
    entry_sha = str(consumption.get("entry_sha256", ""))
    matching = [line for line in ledger_data.splitlines() if len(line.split(b"\t")) == 19 and line.split(b"\t")[1].decode() == consumption.get("key")]
    if len(matching) != 1 or sha256(matching[0]) != entry_sha:
        raise TaskFrontmatterError("watcher consumption ledger does not preserve the exact transition.")
    return attestation, running, done, manager_path, (report_path, envelope_path, commitment_path, ledger_path)


def validate_successor(task_path: Path, task_data: bytes, todo_data: bytes, root: Path, protected_target: str, manager_target: str) -> None:
    metadata = parse_task_metadata(task_data.decode(), root)
    ref = task_path.relative_to(root).as_posix()
    rows = [line for line in todo_data.decode().splitlines() if ref in line.split()]
    if (
        metadata is None
        or metadata.status not in {"running", "long_running"}
        or metadata.runat != protected_target
        or metadata.managerat != manager_target
        or metadata.is_manager
        or metadata.tool != "codex"
        or rows != [f"{ref} {protected_target}"]
        or authoritative_active_target_task_paths(root, protected_target) != (task_path,)
    ):
        raise TaskFrontmatterError("protected successor lifecycle identity is invalid.")


def file_input(path: Path, label: str, *, private: bool = False) -> dict[str, object]:
    _data, identity, ancestors = absolute_file_binding(path, label, private=private)
    return {"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]}


def validate_inputs(recorded: object, expected: list[dict[str, object]]) -> None:
    if recorded != expected:
        raise TaskFrontmatterError("packet does not bind the exact complete input set.")


def packet_bytes(record: dict[str, object]) -> bytes:
    unsigned = dict(record)
    unsigned.pop("binding_id", None)
    return canonical_json({**unsigned, "binding_id": bound_receipt_id(unsigned)})


def pane_record(pin: PanePin) -> dict[str, object]:
    return {"target": pin.target, "pane_id": pin.pane_id, "pane_pid": pin.pane_pid, "pane_start_ticks": pin.pane_start_ticks}


def parse_pin(value: object, label: str) -> PanePin:
    record = object_map(value, label)
    if set(record) != {"target", "pane_id", "pane_pid", "pane_start_ticks"}:
        raise TaskFrontmatterError(f"{label} schema is invalid.")
    raw_pid, raw_ticks = record["pane_pid"], record["pane_start_ticks"]
    if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or not isinstance(raw_ticks, int) or isinstance(raw_ticks, bool):
        raise TaskFrontmatterError(f"{label} process identity is invalid.")
    pin = PanePin(str(record["target"]), str(record["pane_id"]), raw_pid, raw_ticks)
    if TARGET_RE.fullmatch(pin.target) is None or PANE_RE.fullmatch(pin.pane_id) is None or pin.pane_pid <= 1 or pin.pane_start_ticks <= 0:
        raise TaskFrontmatterError(f"{label} identity is invalid.")
    return pin


def validate_packet(data: bytes, expected_sha256: str = "") -> dict[str, object]:
    if expected_sha256 and sha256(data) != expected_sha256:
        raise TaskFrontmatterError("packet digest changed.")
    packet = canonical_object(data, "stale-predecessor packet")
    schema = packet.get("schema")
    expected_keys = SOURCE1485_PACKET_KEYS if schema == SOURCE1485_SCHEMA else PACKET_KEYS
    if schema not in {SCHEMA, SOURCE1485_SCHEMA} or set(packet) != expected_keys or bound_id(packet, "binding_id", "packet") != packet["binding_id"]:
        raise TaskFrontmatterError("stale-predecessor packet schema is invalid.")
    if sha256(base64.b64decode(str(packet["predecessor_running_base64"]), validate=True)) != packet["predecessor_running_sha256"]:
        raise TaskFrontmatterError("packet predecessor running image is invalid.")
    if sha256(base64.b64decode(str(packet["predecessor_done_base64"]), validate=True)) != packet["predecessor_done_sha256"]:
        raise TaskFrontmatterError("packet predecessor done image is invalid.")
    if sha256(str(packet["close_proof_secret"]).encode()) != packet["close_proof_commitment"]:
        raise TaskFrontmatterError("packet close proof commitment is invalid.")
    predecessor = parse_pin(packet["predecessor_pane"], "predecessor pane")
    protected = parse_pin(packet["protected_pane"], "protected pane")
    if (
        predecessor.target != packet["predecessor_target"]
        or protected.target != packet["protected_target"]
        or predecessor.target == protected.target
        or any(target.partition(":")[0].startswith("h") for target in (predecessor.target, protected.target, str(packet["manager_target"])))
        or not Path(str(packet["audit"])).is_absolute()
    ):
        raise TaskFrontmatterError("packet target scope is invalid.")
    if schema == SOURCE1485_SCHEMA:
        validate_source1485_manager_transition(packet)
    return packet


def manager_evidence(record: dict[str, object]) -> tuple[Path, str, Path | None]:
    manager_path = Path(str(record["manager_task"]))
    manager_target = str(record["manager_target"])
    if record.get("schema") == SOURCE1485_SCHEMA:
        return validate_source1485_manager_transition(record)
    return manager_path, manager_target, None


def evidence_input_specs(
    record: dict[str, object],
    task_path: Path,
    todo_path: Path,
    manager_path: Path,
    evidence_paths: tuple[Path, ...],
) -> tuple[tuple[Path, str, bool], ...]:
    source_audit = manager_evidence(record)[2]
    items: list[tuple[Path, str, bool]] = [
        (task_path, "protected task", False),
        (todo_path, "TODO", False),
        (manager_path, "manager task", False),
    ]
    if source_audit is not None:
        items.append((source_audit, "Source-1485 root audit", True))
    items.extend(
        (
            (Path(str(record["helper"])), "stale-predecessor helper", False),
            (Path(str(record["consumed_export"])), "consumed export", True),
            *tuple((path, f"predecessor evidence {index}", True) for index, path in enumerate(evidence_paths)),
        )
    )
    return tuple(items)


def live_evidence(
    packet: dict[str, object],
    *,
    query_sessions: bool = True,
    predecessor_absent: bool = False,
    predecessor_shell: bool = False,
) -> tuple[PanePin, PanePin]:
    root = Path(str(packet["root"]))
    task_path, todo_path, manager_path = (Path(str(packet[key])) for key in ("task", "todo", "manager_task"))
    task_data = read_private_or_owned(task_path, str(packet["task_sha256"]), "protected task")
    todo_data = read_private_or_owned(todo_path, str(packet["todo_sha256"]), "TODO")
    manager_data = read_private_or_owned(manager_path, str(packet["manager_task_sha256"]), "manager task")
    read_private_or_owned(Path(str(packet["helper"])), str(packet["helper_sha256"]), "stale-predecessor helper")
    validate_successor(task_path, task_data, todo_data, root, str(packet["protected_target"]), str(packet["manager_target"]))
    manager = parse_task_metadata(manager_data.decode(), root)
    if manager is None or not manager.is_manager or manager.runat != packet["manager_target"] or manager.status not in {"running", "long_running"}:
        raise TaskFrontmatterError("manager task is no longer the active reporting owner.")
    historical_manager, historical_target, _source_audit = manager_evidence(packet)
    attestation, running, done, observed_manager, evidence_paths = validate_consumed_export(
        Path(str(packet["consumed_export"])),
        str(packet["consumed_export_sha256"]),
        task_path,
        task_data,
        root,
        str(packet["predecessor_target"]),
        historical_target,
    )
    if (
        observed_manager != historical_manager
        or sha256(running) != packet["predecessor_running_sha256"]
        or sha256(done) != packet["predecessor_done_sha256"]
        or attestation.get("replay_id") != packet["report_replay_id"]
        or attestation.get("attestation_id") != packet["report_attestation_id"]
    ):
        raise TaskFrontmatterError("predecessor provenance changed.")
    validate_inputs(
        packet["inputs"],
        [file_input(path, label, private=private) for path, label, private in evidence_input_specs(packet, task_path, todo_path, manager_path, evidence_paths)],
    )
    if authoritative_active_target_task_paths(root, str(packet["predecessor_target"])):
        raise TaskFrontmatterError("completed predecessor target has an active lifecycle owner.")
    predecessor = parse_pin(packet["predecessor_pane"], "predecessor pane")
    protected = parse_pin(packet["protected_pane"], "protected pane")
    if predecessor.target == protected.target or not current_pin(protected):
        raise TaskFrontmatterError("predecessor or protected pane identity changed.")
    if predecessor_absent and predecessor_shell:
        raise TaskFrontmatterError("predecessor recovery state is ambiguous.")
    if predecessor_absent:
        if not pin_is_absent(predecessor):
            raise TaskFrontmatterError("completed predecessor is not exactly absent.")
        protected_session = session_from_process(protected) if query_sessions else packet["protected_session_id"]
    elif predecessor_shell:
        if pinned_current_command(predecessor) not in SHELL_COMMANDS:
            raise TaskFrontmatterError("completed predecessor is not the exact exited shell.")
        protected_session = session_from_process(protected) if query_sessions else packet["protected_session_id"]
    else:
        validate_ready_predecessor(predecessor)
        protected_session = current_session_id(protected, (predecessor,)) if query_sessions else packet["protected_session_id"]
        if query_sessions and current_session_id(predecessor, (protected,)) != packet["predecessor_session_id"]:
            raise TaskFrontmatterError("predecessor Codex session changed.")
    if not current_pin(protected) or protected_session != packet["protected_session_id"]:
        raise TaskFrontmatterError("protected Codex session changed.")
    return predecessor, protected


def read_private_or_owned(path: Path, expected_sha256: str, label: str) -> bytes:
    if not path.is_absolute() or SHA256_RE.fullmatch(expected_sha256) is None:
        raise TaskFrontmatterError(f"{label} identity is invalid.")
    data, identity, _ = absolute_file_binding(path, label)
    if identity.uid != os.getuid() or identity.size_bytes > MAX_FILE_BYTES or sha256(data) != expected_sha256:
        raise TaskFrontmatterError(f"{label} changed.")
    return data


def prepare(args: argparse.Namespace) -> None:
    root = args.root.resolve(strict=True)
    task_path, todo_path = args.task.resolve(strict=True), (root / "TODO.md").resolve(strict=True)
    task_data, task_identity, _ = absolute_file_binding(task_path, "protected task")
    todo_data, todo_identity, _ = absolute_file_binding(todo_path, "TODO")
    metadata = parse_task_metadata(task_data.decode(), root)
    if metadata is None or metadata.runat != args.protected_target or metadata.managerat != args.manager_target:
        raise TaskFrontmatterError("protected task does not match requested successor ownership.")
    if (
        TARGET_RE.fullmatch(args.predecessor_target) is None
        or TARGET_RE.fullmatch(args.protected_target) is None
        or TARGET_RE.fullmatch(args.manager_target) is None
        or args.predecessor_target == args.protected_target
        or any(target.partition(":")[0].startswith("h") for target in (args.predecessor_target, args.protected_target, args.manager_target))
        or not args.audit.is_absolute()
        or not args.output.is_absolute()
    ):
        raise TaskFrontmatterError("prepare target or output scope is invalid.")
    manager_paths = []
    for path in authoritative_active_target_task_paths(root, args.manager_target):
        item = parse_task_metadata(path.read_text(), root)
        if item is not None and item.is_manager and item.status in {"running", "long_running"}:
            manager_paths.append(path)
    if len(manager_paths) != 1:
        raise TaskFrontmatterError("reporting manager ownership is not singular.")
    manager_path = manager_paths[0].resolve(strict=True)
    manager_data, manager_identity, _ = absolute_file_binding(manager_path, "manager task")
    helper_path = Path(__file__).resolve(strict=True)
    helper_data, _helper_identity, _ = absolute_file_binding(helper_path, "stale-predecessor helper")
    source_audit_supplied = args.source1485_root_audit is not None or bool(args.source1485_root_audit_sha256)
    if source_audit_supplied != (args.source1485_root_audit is not None and bool(args.source1485_root_audit_sha256)):
        raise TaskFrontmatterError("Source-1485 manager transition requires an audit path and digest.")
    transition_fields: dict[str, object] = {}
    if source_audit_supplied:
        transition_fields = {
            "schema": SOURCE1485_SCHEMA,
            "root": str(root),
            "manager_task": str(manager_path),
            "manager_target": args.manager_target,
            "historical_manager_task": str(root / "dw_manager.md"),
            "historical_manager_target": "dw:0",
            "source1485_root_audit": str(args.source1485_root_audit),
            "source1485_root_audit_sha256": args.source1485_root_audit_sha256,
        }
        historical_manager, historical_target, _source_audit = validate_source1485_manager_transition(transition_fields)
    else:
        historical_manager, historical_target = manager_path, args.manager_target
    predecessor_identity = target_identity(args.predecessor_target)
    protected_identity = target_identity(args.protected_target)
    predecessor = PanePin(args.predecessor_target, *predecessor_identity)
    protected = PanePin(args.protected_target, *protected_identity)
    if not current_pin(predecessor) or not current_pin(protected) or codex_status(predecessor.target) != "ready":
        raise TaskFrontmatterError("live predecessor/protected pane binding is unavailable.")
    predecessor_session = current_session_id(predecessor, (protected,))
    protected_session = current_session_id(protected, (predecessor,))
    attestation, running, done, observed_manager, evidence_paths = validate_consumed_export(
        args.consumed_export.resolve(strict=True),
        args.consumed_export_sha256,
        task_path,
        task_data,
        root,
        args.predecessor_target,
        historical_target,
    )
    if observed_manager != historical_manager:
        raise TaskFrontmatterError("consumed report manager differs from the authenticated historical reporting manager.")
    validate_successor(task_path, task_data, todo_data, root, args.protected_target, args.manager_target)
    if authoritative_active_target_task_paths(root, args.predecessor_target):
        raise TaskFrontmatterError("completed predecessor target still has active lifecycle ownership.")
    secret = secrets.token_hex(32)
    input_record = {
        "schema": SOURCE1485_SCHEMA if source_audit_supplied else SCHEMA,
        "manager_task": str(manager_path),
        "manager_target": args.manager_target,
        **transition_fields,
        "helper": str(helper_path),
        "consumed_export": str(args.consumed_export.resolve(strict=True)),
    }
    inputs = [file_input(path, label, private=private) for path, label, private in evidence_input_specs(input_record, task_path, todo_path, manager_path, evidence_paths)]
    record: dict[str, object] = {
        "schema": SOURCE1485_SCHEMA if source_audit_supplied else SCHEMA,
        "root": str(root),
        "task": str(task_path),
        "todo": str(todo_path),
        "manager_task": str(manager_path),
        "manager_target": args.manager_target,
        "predecessor_target": args.predecessor_target,
        "predecessor_pane": pane_record(predecessor),
        "predecessor_session_id": predecessor_session,
        "protected_target": args.protected_target,
        "protected_pane": pane_record(protected),
        "protected_session_id": protected_session,
        "task_sha256": task_identity.sha256,
        "todo_sha256": todo_identity.sha256,
        "manager_task_sha256": manager_identity.sha256,
        "consumed_export": str(args.consumed_export.resolve(strict=True)),
        "consumed_export_sha256": args.consumed_export_sha256,
        "predecessor_running_base64": base64.b64encode(running).decode(),
        "predecessor_running_sha256": sha256(running),
        "predecessor_done_base64": base64.b64encode(done).decode(),
        "predecessor_done_sha256": sha256(done),
        "report_replay_id": attestation["replay_id"],
        "report_attestation_id": attestation["attestation_id"],
        "helper": str(helper_path),
        "helper_sha256": sha256(helper_data),
        "close_proof_secret": secret,
        "close_proof_commitment": sha256(secret.encode()),
        "audit": str(args.audit),
        "inputs": inputs,
        **{key: value for key, value in transition_fields.items() if key in SOURCE1485_PACKET_KEYS - PACKET_KEYS},
    }
    data = packet_bytes(record)
    publish_or_validate(args.output, data, "stale-predecessor packet")
    print(sha256(data))


def prepared_audit(packet: dict[str, object], packet_sha256: str) -> bytes:
    keys = SOURCE1485_AUDIT_KEYS if packet.get("schema") == SOURCE1485_SCHEMA else AUDIT_KEYS
    fields = {key: packet[key] for key in keys if key in packet}
    fields.update({"schema": packet["schema"], "operation": OPERATION, "state": "prepared", "packet_sha256": packet_sha256})
    return canonical_json(fields)


def _audit_authorizes(
    audit_text: str,
    audit: object,
    commitment: str,
    target: str,
    pane_id: str,
    pane_pid: int,
    pane_start_ticks: int,
    audit_path: Path,
    *,
    predecessor_absent: bool,
) -> bool:
    if not isinstance(audit, dict):
        return False
    record = {str(key): value for key, value in audit.items()}
    schema = record.get("schema")
    expected_keys = SOURCE1485_AUDIT_KEYS if schema == SOURCE1485_SCHEMA else AUDIT_KEYS
    if schema not in {SCHEMA, SOURCE1485_SCHEMA} or set(record) != expected_keys:
        return False
    try:
        predecessor = parse_pin(record["predecessor_pane"], "predecessor pane")
        protected = parse_pin(record["protected_pane"], "protected pane")
        root = Path(str(record["root"]))
        task = Path(str(record["task"]))
        todo = Path(str(record["todo"]))
        manager_path = Path(str(record["manager_task"]))
        task_data = read_private_or_owned(task, str(record["task_sha256"]), "protected task")
        todo_data = read_private_or_owned(todo, str(record["todo_sha256"]), "TODO")
        manager_data = read_private_or_owned(manager_path, str(record["manager_task_sha256"]), "manager task")
        read_private_or_owned(Path(str(record["helper"])), str(record["helper_sha256"]), "stale-predecessor helper")
        validate_successor(task, task_data, todo_data, root, protected.target, str(record["manager_target"]))
        manager = parse_task_metadata(manager_data.decode(), root)
        historical_manager, historical_target, _source_audit = manager_evidence(record)
        attestation, running, done, observed_manager, evidence_paths = validate_consumed_export(
            Path(str(record["consumed_export"])),
            str(record["consumed_export_sha256"]),
            task,
            task_data,
            root,
            predecessor.target,
            historical_target,
        )
        expected_inputs = [file_input(path, label, private=private) for path, label, private in evidence_input_specs(record, task, todo, manager_path, evidence_paths)]
        if predecessor_absent:
            predecessor_state_matches = pin_is_absent(predecessor)
            predecessor_session = record.get("predecessor_session_id")
        else:
            predecessor_state_matches = pinned_current_command(predecessor) in SHELL_COMMANDS
            predecessor_session = record.get("predecessor_session_id")
        protected_current = current_pin(protected)
        protected_session = session_from_process(protected)
        protected_current = protected_current and current_pin(protected)
        predecessor_owners = authoritative_active_target_task_paths(root, predecessor.target)
    except (CustodyError, OSError, RuntimeError, TaskFrontmatterError, UnicodeError, ValueError):
        return False
    return (
        audit_text.encode() == canonical_json(record)
        and record.get("schema") in {SCHEMA, SOURCE1485_SCHEMA}
        and record.get("operation") == OPERATION
        and record.get("state") == "prepared"
        and record.get("close_proof_commitment") == commitment
        and record.get("audit") == str(audit_path).removesuffix(".prepared")
        and predecessor == PanePin(target, pane_id, pane_pid, pane_start_ticks)
        and predecessor.target != protected.target
        and predecessor_state_matches
        and protected_current
        and protected_session == record.get("protected_session_id")
        and predecessor_session == record.get("predecessor_session_id")
        and manager is not None
        and manager.is_manager
        and manager.runat == record.get("manager_target")
        and manager.status in {"running", "long_running"}
        and observed_manager == historical_manager
        and attestation.get("replay_id") == record.get("report_replay_id")
        and attestation.get("attestation_id") == record.get("report_attestation_id")
        and sha256(running) == record.get("predecessor_running_sha256")
        and sha256(done) == record.get("predecessor_done_sha256")
        and not predecessor_owners
        and record.get("inputs") == expected_inputs
        and sha256(task_data) == record.get("task_sha256")
        and sha256(todo_data) == record.get("todo_sha256")
        and SESSION_RE.fullmatch(str(record.get("predecessor_session_id", ""))) is not None
        and SESSION_RE.fullmatch(str(record.get("protected_session_id", ""))) is not None
        and all(
            SHA256_RE.fullmatch(str(record.get(key, ""))) is not None
            for key in (
                "task_sha256",
                "todo_sha256",
                "manager_task_sha256",
                "consumed_export_sha256",
                "predecessor_running_sha256",
                "predecessor_done_sha256",
                "report_replay_id",
                "report_attestation_id",
                "close_proof_commitment",
                "packet_sha256",
                "binding_id",
                "helper_sha256",
            )
        )
    )


def audit_authorizes(
    audit_text: str,
    audit: object,
    commitment: str,
    target: str,
    pane_id: str,
    pane_pid: int,
    pane_start_ticks: int,
    audit_path: Path,
) -> bool:
    """Authorize a pre-kill audit for the exact predecessor's exited shell."""

    return _audit_authorizes(
        audit_text,
        audit,
        commitment,
        target,
        pane_id,
        pane_pid,
        pane_start_ticks,
        audit_path,
        predecessor_absent=False,
    )


def audit_authorizes_after_close(
    audit_text: str,
    audit: object,
    commitment: str,
    target: str,
    pane_id: str,
    pane_pid: int,
    pane_start_ticks: int,
    audit_path: Path,
) -> bool:
    """Authorize proof promotion only after the exact predecessor is absent."""

    return _audit_authorizes(
        audit_text,
        audit,
        commitment,
        target,
        pane_id,
        pane_pid,
        pane_start_ticks,
        audit_path,
        predecessor_absent=True,
    )


def review(args: argparse.Namespace) -> None:
    data = read_private(args.packet, args.packet_sha256, "stale-predecessor packet")
    packet = validate_packet(data, args.packet_sha256)
    predecessor, protected = live_evidence(packet)
    print(
        json.dumps(
            {
                "schema": REVIEW_SCHEMA,
                "verdict": "PASS",
                "packet_sha256": args.packet_sha256,
                "predecessor_session_id": packet["predecessor_session_id"],
                "protected_session_id": packet["protected_session_id"],
                "predecessor_pane": pane_record(predecessor),
                "protected_pane": pane_record(protected),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def validate_review(path: Path, expected_sha256: str, packet: dict[str, object], packet_sha256: str) -> None:
    review_record = canonical_object(read_private(path, expected_sha256, "independent review"), "independent review")
    expected = {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": packet_sha256,
        "predecessor_session_id": packet["predecessor_session_id"],
        "protected_session_id": packet["protected_session_id"],
        "predecessor_pane": packet["predecessor_pane"],
        "protected_pane": packet["protected_pane"],
    }
    if review_record != expected:
        raise TaskFrontmatterError("independent review does not PASS this exact live packet.")


def execute(args: argparse.Namespace) -> None:
    data = read_private(args.packet, args.packet_sha256, "stale-predecessor packet")
    packet = validate_packet(data, args.packet_sha256)
    if args.review_report is None:
        raise TaskFrontmatterError("execute requires one independent PASS report.")
    validate_review(args.review_report, args.review_report_sha256, packet, args.packet_sha256)
    root = Path(str(packet["root"]))
    input_records = packet.get("inputs")
    if not isinstance(input_records, list):
        raise TaskFrontmatterError("packet input set is invalid.")
    with task_target_lock(root, str(packet["predecessor_target"])), task_target_lock(root, str(packet["protected_target"])), ExitStack() as stack:
        for path in sorted({Path(str(packet["task"])), Path(str(packet["todo"])), Path(str(packet["manager_task"]))}, key=str):
            stack.enter_context(task_file_lock(path))
        held = []
        for item in input_records:
            record = object_map(item, "packet input")
            identity = file_identity_from(record["file"], "packet input")
            raw_ancestors = record["ancestors"]
            if not isinstance(raw_ancestors, Iterable) or isinstance(raw_ancestors, (str, bytes, dict)):
                raise TaskFrontmatterError("packet input ancestors are invalid.")
            ancestors = tuple(directory_identity_from(value, "packet input ancestor") for value in cast(Iterable[object], raw_ancestors))
            current = hold_absolute(identity, ancestors)
            held.append(current)
            stack.callback(os.close, current.descriptor)
            for descriptor in reversed(current.directories):
                stack.callback(os.close, descriptor)
        for current in held:
            validate_held_absolute(current)
        prepared_path = Path(f"{packet['audit']}.prepared")
        proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
        recorded_predecessor = parse_pin(packet["predecessor_pane"], "predecessor pane")
        recovering_absence = pin_is_absent(recorded_predecessor) and (path_entry_exists(done_live_close_started_path(prepared_path)) or path_entry_exists(proof_path))
        recovering_shell = not recovering_absence and path_entry_exists(prepared_path) and current_pin(recorded_predecessor) and pinned_current_command(recorded_predecessor) in SHELL_COMMANDS
        predecessor, protected = live_evidence(
            packet,
            predecessor_absent=recovering_absence,
            predecessor_shell=recovering_shell,
        )
        audit_data = prepared_audit(packet, args.packet_sha256)
        audit_sha = sha256(audit_data)
        publish_or_validate(prepared_path, audit_data, "prepared stale-predecessor audit")

        predecessor_was_ready = False

        def unchanged() -> None:
            nonlocal predecessor_was_ready
            for current in held:
                validate_held_absolute(current)
            if not current_pin(protected):
                raise TaskFrontmatterError("protected successor changed before guarded close.")
            predecessor_command = pinned_current_command(predecessor)
            if predecessor_command in SHELL_COMMANDS:
                if not predecessor_was_ready:
                    raise TaskFrontmatterError("completed predecessor resumed or changed.")
            else:
                validate_ready_predecessor(predecessor)
                predecessor_was_ready = True

        def protected_unchanged() -> None:
            for current in held:
                validate_held_absolute(current)
            if not current_pin(protected) or session_from_process(protected) != packet["protected_session_id"] or not current_pin(protected):
                raise TaskFrontmatterError("protected successor changed during guarded close.")

        def shell_unchanged() -> None:
            protected_unchanged()
            if pinned_current_command(predecessor) not in SHELL_COMMANDS:
                raise TaskFrontmatterError("completed predecessor shell changed before guarded close.")

        close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), audit_sha, OPERATION)
        if exact_pane_id(predecessor.target) == "" and path_entry_exists(done_live_close_started_path(prepared_path)) and not close_proven:
            promote_done_live_close_started(
                proof_path,
                prepared_path,
                str(packet["close_proof_commitment"]),
                audit_sha,
                predecessor.target,
                predecessor.pane_id,
                predecessor.pane_pid,
                predecessor.pane_start_ticks,
                OPERATION,
            )
            close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), audit_sha, OPERATION)
        if exact_pane_id(predecessor.target) == predecessor.pane_id:
            if recovering_shell:
                shell_unchanged()
                close_bound_tmux_target(
                    predecessor.pane_id,
                    lambda: current_pin(predecessor),
                    predecessor.target,
                    predecessor.pane_id,
                    str(proof_path),
                    str(prepared_path),
                    str(packet["close_proof_secret"]),
                    str(packet["close_proof_commitment"]),
                    predecessor.pane_pid,
                    predecessor.pane_start_ticks,
                    shell_unchanged,
                    OPERATION,
                    audit_sha,
                )
            else:
                unchanged()
                observed = guarded_codex_stop(
                    CodexStopArgs(
                        target=predecessor.target,
                        wait_s=10.0,
                        lines=2000,
                        dry_run=False,
                        allow_self=False,
                        no_feedback=True,
                        bound_symbolic_target=predecessor.target,
                        bound_pane_id=predecessor.pane_id,
                        bound_pane_pid=predecessor.pane_pid,
                        bound_pane_start_ticks=predecessor.pane_start_ticks,
                        bound_expected_session_id=str(packet["predecessor_session_id"]),
                        bound_pre_input_check=unchanged,
                        bound_close_proof_path=str(proof_path),
                        bound_close_audit_path=str(prepared_path),
                        bound_close_proof_secret=str(packet["close_proof_secret"]),
                        bound_close_proof_commitment=str(packet["close_proof_commitment"]),
                        bound_close_operation=OPERATION,
                        bound_close_audit_sha256=audit_sha,
                    )
                )
                if observed.lower() != packet["predecessor_session_id"]:
                    raise TaskFrontmatterError("guarded close returned a different predecessor session.")
            close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), audit_sha, OPERATION)
        if not close_proven or exact_pane_id(predecessor.target) or process_start_ticks(predecessor.pane_pid) is not None:
            raise TaskFrontmatterError("stale predecessor closure lacks exact durable absence proof.")
        protected_unchanged()
        complete = canonical_json(
            {
                **canonical_object(audit_data, "prepared audit"),
                "state": "complete",
                "prepared_audit_sha256": audit_sha,
                "owner_stopped_proof": str(proof_path),
            }
        )
        publish_or_validate(Path(str(packet["audit"])), complete, "complete stale-predecessor audit")
    print(packet["audit"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--review", action="store_true")
    modes.add_argument("--execute", action="store_true")
    result.add_argument("--root", type=Path, default=Path("/ssd1/sichangheagent/work_logs"))
    result.add_argument("--task", type=Path)
    result.add_argument("--predecessor-target", default="")
    result.add_argument("--protected-target", default="")
    result.add_argument("--manager-target", default="")
    result.add_argument("--consumed-export", type=Path)
    result.add_argument("--consumed-export-sha256", default="")
    result.add_argument("--source1485-root-audit", type=Path)
    result.add_argument("--source1485-root-audit-sha256", default="")
    result.add_argument("--audit", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--packet", type=Path)
    result.add_argument("--packet-sha256", default="")
    result.add_argument("--review-report", type=Path)
    result.add_argument("--review-report-sha256", default="")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.prepare:
            if any(value is None for value in (args.task, args.consumed_export, args.audit, args.output)):
                raise TaskFrontmatterError("prepare requires task, consumed export, audit, and output.")
            prepare(args)
        elif args.review:
            if args.packet is None:
                raise TaskFrontmatterError("review requires packet.")
            review(args)
        else:
            if args.packet is None:
                raise TaskFrontmatterError("execute requires packet.")
            execute(args)
    except (CustodyError, OSError, TaskFrontmatterError, UnicodeError, ValueError) as exc:
        print(f"omo_stale_predecessor_close.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
