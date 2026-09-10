#!/usr/bin/env python3
"""Prepare and review one TODO-CAS recovery for the WebConf shell closer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import omo_manager.omo_codex_stop as codex_stop_module
import omo_manager.omo_task_lock as task_lock_module
import omo_manager.omo_task_metadata as task_metadata_module
import omo_manager.omo_task_status as task_status_module
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import process_start_ticks
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_status import reserve_private_audit
from omo_manager.omo_task_status import root_membership_lock
from omo_manager.omo_webconf_exited_shell_close import Args as CloseArgs
from omo_manager.omo_webconf_exited_shell_close import EXPECTED_ROOT
from omo_manager.omo_webconf_exited_shell_close import INCIDENT_TASK_SHA256
from omo_manager.omo_webconf_exited_shell_close import MANAGER_TARGET
from omo_manager.omo_webconf_exited_shell_close import REPORT_COMMITMENT_PATH
from omo_manager.omo_webconf_exited_shell_close import REPORT_COMMITMENT_SHA256
from omo_manager.omo_webconf_exited_shell_close import REPORT_REPLAY_ID
from omo_manager.omo_webconf_exited_shell_close import SESSION_ID
from omo_manager.omo_webconf_exited_shell_close import TARGET
from omo_manager.omo_webconf_exited_shell_close import TASK_REF
from omo_manager.omo_webconf_exited_shell_close import validate_report_commitment
from omo_manager.omo_webconf_exited_shell_close import validate_source_task

pane_id = codex_stop_module.pane_id
validate_exited_codex_shell_with_consumed_report = codex_stop_module.validate_exited_codex_shell_with_consumed_report

SCHEMA = "omo-webconf-exited-shell-cas-recovery/v1"
REVIEW_SCHEMA = "omo-webconf-exited-shell-cas-recovery-review/v1"
# 🧑 "Email me the paper title, authors, year, official URL, then URL that searches it in Google Scholar, in a list"
RECOVERY_PANE_ID = "%898"
RECOVERY_PANE_PID = 644165
RECOVERY_PANE_START_TICKS = 33542113
FAILED_OBSERVED_CAPTURE_SHA256 = "fa4d6c04aaa7f41f6751fb411b1dac460651c4bb5990d911d2c81e2c183168af"
FAILED_RELEASE_REPLAY_ID = "13be99bb7e519fca72066ab77b39990a6ee921f4c874bcbc594fbff29bb92caf"
FAILED_RELEASE_TODO_SHA256 = "f881ba07a5ea909e72bceeafc7db98477963187c7b6d7e3e0dbace7166c5a5d7"
FAILED_OBSERVED_TODO_SHA256 = "ef50b913db017d9dd3d0ff6628a72fc049076ee908e59bbcff652835d9d62699"
FAILED_RELEASE_CAPTURE_SHA256 = "7a8f478eb5d5416cbaeff0265232066a4a22875fda28a13f22ccfc87a365faa2"
MAX_BYTES = 1_000_000
SHA256_RE = re.compile(r"[0-9a-f]{64}")


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


def read_snapshot(path: Path, label: str, *, private: bool = False) -> tuple[bytes, Snapshot]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise TaskFrontmatterError(f"{label} is unavailable: {error}") from error
    try:
        before = os.fstat(fd)
        data = os.read(fd, MAX_BYTES + 1)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
    finally:
        os.close(fd)
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
        raise TaskFrontmatterError(f"{label} changed or has an unsafe file identity")
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
    keys = {"ctime_ns", "dev", "inode", "mode", "mtime_ns", "nlink", "path", "sha256", "size", "uid"}
    if not isinstance(value, dict) or set(value) != keys:
        raise TaskFrontmatterError(f"{label} identity schema is invalid")
    try:
        result = Snapshot(
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
    except (TypeError, ValueError) as error:
        raise TaskFrontmatterError(f"{label} identity fields are invalid") from error
    if SHA256_RE.fullmatch(result.sha256) is None:
        raise TaskFrontmatterError(f"{label} SHA-256 is invalid")
    return result


def assert_snapshot(expected: Snapshot, label: str, *, private: bool = False) -> bytes:
    data, observed = read_snapshot(Path(expected.path), label, private=private)
    if observed != expected:
        raise TaskFrontmatterError(f"{label} changed after recovery preparation")
    return data


def partition_todo(data: bytes) -> tuple[bytes, bytes, bytes]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TaskFrontmatterError("TODO is not UTF-8") from error
    expected = f"{TASK_REF} {TARGET}"
    section = ""
    matches: list[tuple[int, int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        elif TASK_REF.as_posix() in line:
            matches.append((cursor, cursor + len(line.encode()), section))
        cursor += len(line.encode())
    if len(matches) != 1:
        raise TaskFrontmatterError("WebConf recovery requires one exact TODO row")
    start, end, row_section = matches[0]
    row = data[start:end]
    if row_section != "current" or row.decode("utf-8").rstrip("\r\n") != expected or not row.endswith(b"\n"):
        raise TaskFrontmatterError("WebConf recovery requires exact current-row custody")
    return data[:start], row, data[end:]


def control_paths(audit_output: Path) -> set[Path]:
    audit = audit_output.resolve(strict=False)
    authority = audit.with_name(f".{audit.name}.close-authority")
    return {
        audit,
        authority,
        authority.with_name(f".{authority.name}.owner-stopped"),
        codex_stop_module.done_live_close_started_path(authority),
    }


def validate_output(path: Path, forbidden: set[Path], label: str) -> Path:
    candidate = path.resolve(strict=False)
    try:
        parent = candidate.parent.resolve(strict=True)
        parent_state = parent.stat()
    except OSError as error:
        raise TaskFrontmatterError(f"{label} parent is unavailable: {error}") from error
    if candidate in forbidden or not stat.S_ISDIR(parent_state.st_mode) or parent_state.st_uid != os.getuid() or stat.S_IMODE(parent_state.st_mode) & 0o077:
        raise TaskFrontmatterError(f"{label} must be a distinct owner-private path")
    return candidate


def decode_partition(packet: dict[str, object]) -> tuple[bytes, bytes, bytes]:
    value = packet.get("todo_partition")
    if not isinstance(value, dict) or set(value) != {"before_b64", "row_b64", "after_b64"}:
        raise TaskFrontmatterError("TODO partition schema is invalid")
    try:
        return (
            base64.b64decode(str(value["before_b64"]), validate=True),
            base64.b64decode(str(value["row_b64"]), validate=True),
            base64.b64decode(str(value["after_b64"]), validate=True),
        )
    except (TypeError, ValueError) as error:
        raise TaskFrontmatterError("TODO partition encoding is invalid") from error


def close_args(todo_sha256: str, capture_sha256: str, audit_output: Path) -> CloseArgs:
    return CloseArgs(
        EXPECTED_ROOT,
        TASK_REF,
        INCIDENT_TASK_SHA256,
        todo_sha256,
        RECOVERY_PANE_ID,
        RECOVERY_PANE_PID,
        RECOVERY_PANE_START_TICKS,
        capture_sha256,
        REPORT_COMMITMENT_PATH,
        REPORT_COMMITMENT_SHA256,
        audit_output,
    )


def source_paths() -> dict[str, Path]:
    return {
        "recovery_helper_input": Path(__file__).resolve(strict=True),
        "close_helper_input": Path(__file__).with_name("omo_webconf_exited_shell_close.py").resolve(strict=True),
        "codex_stop_input": Path(codex_stop_module.__file__).resolve(strict=True),
        "task_lock_input": Path(task_lock_module.__file__).resolve(strict=True),
        "task_metadata_input": Path(task_metadata_module.__file__).resolve(strict=True),
        "task_status_input": Path(task_status_module.__file__).resolve(strict=True),
    }


def validate_packet(packet: dict[str, object]) -> None:
    expected_keys = {
        "schema",
        "root",
        "task",
        "target",
        "manager_target",
        "failed_release",
        "task_input",
        "todo_input",
        "todo_partition",
        *source_paths(),
        "close_audit_output",
        "terminal",
        "packet_id",
    }
    if set(packet) != expected_keys or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("WebConf recovery packet schema is invalid")
    unsigned = dict(packet)
    packet_id = unsigned.pop("packet_id", None)
    if packet_id != digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()):
        raise TaskFrontmatterError("WebConf recovery packet content identity is invalid")
    terminal_value = packet.get("terminal")
    if not isinstance(terminal_value, dict):
        raise TaskFrontmatterError("WebConf recovery packet terminal binding is invalid")
    terminal: dict[str, object] = {}
    for key, value in terminal_value.items():
        if not isinstance(key, str):
            raise TaskFrontmatterError("WebConf recovery packet terminal binding is invalid")
        terminal[key] = value
    if (
        packet.get("root") != str(EXPECTED_ROOT)
        or packet.get("task") != TASK_REF.as_posix()
        or packet.get("target") != TARGET
        or packet.get("manager_target") != MANAGER_TARGET
        or packet.get("failed_release")
        != {
            "bound_capture_sha256": FAILED_RELEASE_CAPTURE_SHA256,
            "bound_todo_sha256": FAILED_RELEASE_TODO_SHA256,
            "observed_capture_sha256": FAILED_OBSERVED_CAPTURE_SHA256,
            "observed_todo_sha256": FAILED_OBSERVED_TODO_SHA256,
            "replay_id": FAILED_RELEASE_REPLAY_ID,
        }
    ):
        raise TaskFrontmatterError("WebConf recovery packet incident binding is invalid")
    if (
        set(terminal) != {"capture_sha256", "pane_id", "pane_pid", "pane_start_ticks", "report_replay_id", "session_id"}
        or SHA256_RE.fullmatch(str(terminal.get("capture_sha256"))) is None
        or (terminal.get("pane_id"), terminal.get("pane_pid"), terminal.get("pane_start_ticks")) != (RECOVERY_PANE_ID, RECOVERY_PANE_PID, RECOVERY_PANE_START_TICKS)
        or terminal.get("report_replay_id") != REPORT_REPLAY_ID
        or terminal.get("session_id") != SESSION_ID
    ):
        raise TaskFrontmatterError("WebConf recovery packet terminal binding is invalid")
    task = parse_snapshot(packet["task_input"], "task")
    todo = parse_snapshot(packet["todo_input"], "TODO")
    if task.path != str((EXPECTED_ROOT / TASK_REF).resolve(strict=False)) or task.sha256 != INCIDENT_TASK_SHA256 or todo.path != str((EXPECTED_ROOT / "TODO.md").resolve(strict=False)):
        raise TaskFrontmatterError("WebConf recovery packet task or TODO binding is invalid")
    for label, path in source_paths().items():
        if parse_snapshot(packet[label], label).path != str(path):
            raise TaskFrontmatterError(f"{label} path is invalid")
    before, row, after = decode_partition(packet)
    if digest(before + row + after) != todo.sha256 or partition_todo(before + row + after) != (before, row, after):
        raise TaskFrontmatterError("WebConf recovery packet does not bind the exact TODO partition")
    audit_output = Path(str(packet["close_audit_output"]))
    forbidden = {Path(item.path) for item in (task, todo, *(parse_snapshot(packet[label], label) for label in source_paths()))}
    validate_output(audit_output, forbidden, "close audit output")


def load_canonical(path: Path, expected_sha256: str, label: str) -> tuple[bytes, dict[str, object]]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise TaskFrontmatterError(f"{label} digest is invalid")
    data, _identity = read_snapshot(path.resolve(strict=True), label, private=True)
    if digest(data) != expected_sha256:
        raise TaskFrontmatterError(f"{label} digest is invalid")
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TaskFrontmatterError(f"{label} is not canonical JSON") from error
    if not isinstance(value, dict) or data != canonical(value):
        raise TaskFrontmatterError(f"{label} is not canonical JSON")
    return data, value


def load_packet(path: Path, expected_sha256: str) -> dict[str, object]:
    _data, packet = load_canonical(path, expected_sha256, "recovery packet")
    validate_packet(packet)
    return packet


def review_record(packet: dict[str, object], packet_sha256: str) -> dict[str, object]:
    return {
        "schema": REVIEW_SCHEMA,
        "packet_id": packet["packet_id"],
        "packet_sha256": packet_sha256,
        "result": "PASS",
        "reviewed_guards": [
            "exact-WebConf-task-report-session-and-pane",
            "current-whole-TODO-and-exact-current-row",
            "immutable-helper-inputs",
            "unchanged-exited-shell-capture",
            "execution-time-input-and-capture-rechecks",
        ],
    }


def validate_review(path: Path, expected_sha256: str, packet: dict[str, object], packet_sha256: str) -> None:
    data, review = load_canonical(path, expected_sha256, "independent review")
    if review != review_record(packet, packet_sha256) or data != canonical(review_record(packet, packet_sha256)):
        raise TaskFrontmatterError("independent review does not PASS this recovery packet")


def validate_current_inputs(packet: dict[str, object]) -> None:
    task = parse_snapshot(packet["task_input"], "task")
    todo = parse_snapshot(packet["todo_input"], "TODO")
    task_data = assert_snapshot(task, "task")
    todo_data = assert_snapshot(todo, "TODO")
    if partition_todo(todo_data) != decode_partition(packet):
        raise TaskFrontmatterError("TODO row or unrelated bytes changed after recovery preparation")
    audit_output = Path(str(packet["close_audit_output"]))
    terminal = packet["terminal"]
    if not isinstance(terminal, dict):
        raise TaskFrontmatterError("WebConf recovery terminal binding is invalid")
    capture_expected = str(terminal.get("capture_sha256", ""))
    args = close_args(todo.sha256, capture_expected, audit_output)
    validate_report_commitment(args, Path(task.path))
    validate_source_task(args, Path(task.path), task_data.decode("utf-8"), todo_data.decode("utf-8"))
    if pane_id(TARGET) != RECOVERY_PANE_ID or pane_id(RECOVERY_PANE_ID) != RECOVERY_PANE_ID or process_start_ticks(RECOVERY_PANE_PID) != RECOVERY_PANE_START_TICKS:
        raise TaskFrontmatterError("WebConf recovery pane/process identity changed")
    capture_sha256 = validate_exited_codex_shell_with_consumed_report(TARGET, RECOVERY_PANE_ID, SESSION_ID, REPORT_REPLAY_ID)
    if capture_sha256 != capture_expected:
        raise TaskFrontmatterError("WebConf recovery exited-shell capture changed")
    _ = assert_snapshot(task, "task")
    _ = assert_snapshot(todo, "TODO")


def validate_recovery_files(
    packet_path: Path,
    packet_sha256: str,
    review_path: Path,
    review_sha256: str,
    *,
    expected_task_sha256: str,
    expected_todo_sha256: str,
    expected_capture_sha256: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    audit_output: Path,
    assert_current_inputs: bool,
) -> None:
    packet = load_packet(packet_path, packet_sha256)
    validate_review(review_path, review_sha256, packet, packet_sha256)
    task = parse_snapshot(packet["task_input"], "task")
    todo = parse_snapshot(packet["todo_input"], "TODO")
    terminal = packet["terminal"]
    if not isinstance(terminal, dict):
        raise TaskFrontmatterError("WebConf recovery terminal binding is invalid")
    if (
        (task.sha256, todo.sha256, terminal.get("capture_sha256")) != (expected_task_sha256, expected_todo_sha256, expected_capture_sha256)
        or (terminal.get("pane_id"), terminal.get("pane_pid"), terminal.get("pane_start_ticks")) != (expected_pane_id, expected_pane_pid, expected_pane_start_ticks)
        or Path(str(packet["close_audit_output"])) != audit_output
    ):
        raise TaskFrontmatterError("WebConf recovery invocation does not match its reviewed packet")
    for label in source_paths():
        _ = assert_snapshot(parse_snapshot(packet[label], label), label)
    if assert_current_inputs:
        validate_current_inputs(packet)


def prepare(packet_output: Path, close_audit_output: Path) -> None:
    task = EXPECTED_ROOT / TASK_REF
    todo = EXPECTED_ROOT / "TODO.md"
    sources = source_paths()
    forbidden = {task, todo, *sources.values()}
    audit_output = validate_output(close_audit_output, forbidden, "close audit output")
    packet_path = validate_output(packet_output, forbidden | control_paths(audit_output), "packet output")
    if packet_path.exists() or any(path.exists() for path in control_paths(audit_output)):
        raise TaskFrontmatterError("recovery outputs must be initially absent")
    with root_membership_lock(EXPECTED_ROOT), task_target_lock(EXPECTED_ROOT, TARGET), ExitStack() as locks:
        for path in sorted({task, todo}, key=str):
            locks.enter_context(task_file_lock(path))
        task_data, task_snapshot = read_snapshot(task, "task")
        todo_data, todo_snapshot = read_snapshot(todo, "TODO")
        before, row, after = partition_todo(todo_data)
        source_snapshots = {label: read_snapshot(path, label)[1] for label, path in sources.items()}
        if pane_id(TARGET) != RECOVERY_PANE_ID or pane_id(RECOVERY_PANE_ID) != RECOVERY_PANE_ID or process_start_ticks(RECOVERY_PANE_PID) != RECOVERY_PANE_START_TICKS:
            raise TaskFrontmatterError("WebConf recovery pane/process identity changed")
        capture_sha256 = validate_exited_codex_shell_with_consumed_report(TARGET, RECOVERY_PANE_ID, SESSION_ID, REPORT_REPLAY_ID)
        if SHA256_RE.fullmatch(capture_sha256) is None:
            raise TaskFrontmatterError("WebConf recovery capture digest is invalid")
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(EXPECTED_ROOT),
            "task": TASK_REF.as_posix(),
            "target": TARGET,
            "manager_target": MANAGER_TARGET,
            "failed_release": {
                "bound_capture_sha256": FAILED_RELEASE_CAPTURE_SHA256,
                "bound_todo_sha256": FAILED_RELEASE_TODO_SHA256,
                "observed_capture_sha256": FAILED_OBSERVED_CAPTURE_SHA256,
                "observed_todo_sha256": FAILED_OBSERVED_TODO_SHA256,
                "replay_id": FAILED_RELEASE_REPLAY_ID,
            },
            "task_input": snapshot_record(task_snapshot),
            "todo_input": snapshot_record(todo_snapshot),
            "todo_partition": {
                "after_b64": base64.b64encode(after).decode(),
                "before_b64": base64.b64encode(before).decode(),
                "row_b64": base64.b64encode(row).decode(),
            },
            **{label: snapshot_record(value) for label, value in source_snapshots.items()},
            "close_audit_output": str(audit_output),
            "terminal": {
                "capture_sha256": capture_sha256,
                "pane_id": RECOVERY_PANE_ID,
                "pane_pid": RECOVERY_PANE_PID,
                "pane_start_ticks": RECOVERY_PANE_START_TICKS,
                "report_replay_id": REPORT_REPLAY_ID,
                "session_id": SESSION_ID,
            },
        }
        packet["packet_id"] = digest(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode())
        validate_packet(packet)
        validate_current_inputs(packet)
        if task_data != assert_snapshot(task_snapshot, "task") or todo_data != assert_snapshot(todo_snapshot, "TODO"):
            raise TaskFrontmatterError("WebConf task or TODO changed before recovery packet publication")
        reserve_private_audit(packet_path, canonical(packet).decode())


def review(packet_path: Path, packet_sha256: str, review_output: Path) -> None:
    packet = load_packet(packet_path, packet_sha256)
    forbidden = {
        packet_path.resolve(strict=True),
        Path(str(packet["close_audit_output"])),
        *(Path(parse_snapshot(packet[label], label).path) for label in ("task_input", "todo_input", *source_paths())),
    }
    output = validate_output(review_output, forbidden, "review output")
    if output.exists():
        raise TaskFrontmatterError("review output must be initially absent")
    with root_membership_lock(EXPECTED_ROOT), task_target_lock(EXPECTED_ROOT, TARGET), ExitStack() as locks:
        task = EXPECTED_ROOT / TASK_REF
        todo = EXPECTED_ROOT / "TODO.md"
        for path in sorted({task, todo}, key=str):
            locks.enter_context(task_file_lock(path))
        validate_current_inputs(packet)
        for label in source_paths():
            _ = assert_snapshot(parse_snapshot(packet[label], label), label)
        _ = load_packet(packet_path, packet_sha256)
        reserve_private_audit(output, canonical(review_record(packet, packet_sha256)).decode())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--review", action="store_true")
    result.add_argument("--packet-output", type=Path)
    result.add_argument("--close-audit-output", type=Path)
    result.add_argument("--packet", type=Path)
    result.add_argument("--packet-sha256", default="")
    result.add_argument("--review-output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.prepare:
            if args.packet_output is None or args.close_audit_output is None or args.packet is not None or args.packet_sha256 or args.review_output is not None:
                raise TaskFrontmatterError("prepare requires only packet and close-audit output paths")
            prepare(args.packet_output, args.close_audit_output)
        else:
            if args.packet is None or not args.packet_sha256 or args.review_output is None or args.packet_output is not None or args.close_audit_output is not None:
                raise TaskFrontmatterError("review requires only packet, packet SHA-256, and review output")
            review(args.packet, args.packet_sha256, args.review_output)
        return 0
    except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError, TaskFrontmatterError) as error:
        print(f"omo_webconf_exited_shell_recovery.py: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
