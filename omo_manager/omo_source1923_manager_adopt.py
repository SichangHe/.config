#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Adopt the exact live Source-1923 DeepWiki successor transactionally."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import Args as CodexStopArgs
from omo_manager.omo_codex_stop import (
    bound_guarded_read,
    capture,
    codex_status,
    done_live_close_started_path,
    has_bound_close_proof,
    path_entry_exists,
    promote_done_live_close_started,
    query_status_session_id,
    stop as guarded_codex_stop,
)
from omo_manager.omo_exported_agent_close import read_regular, read_regular_unbound, rename_exchange
from omo_manager.omo_repository_custody import (
    CustodyError,
    DirectoryIdentity,
    FileIdentity,
    HeldAbsolute,
    absolute_file_binding,
    directory_identity_from,
    existing_exact,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_stale_predecessor_close import PanePin as ProcessPanePin
from omo_manager.omo_stale_predecessor_close import session_from_process
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, TaskMetadata, parse_task_metadata
from omo_manager.omo_task import manager_owner_migration_text
from omo_manager.omo_task_status import (
    active_child_task_refs,
    authoritative_active_target_task_paths,
    cleared_pending_task_text,
    has_pending_marker,
    reconcile_done_todo_text,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)

SCHEMA = "omo-source1923-manager-adopt/v1"
REVIEW_SCHEMA = "omo-source1923-manager-adopt-review/v1"
CLOSE_OPERATION = "source1923-manager-adopt"
TRUSTED_ROOT_ENV = "OMO_SOURCE1923_WORK_LOGS_ROOT"
TRUSTED_ROOT_DEVICE = 66307
TRUSTED_ROOT_INODE = 84569809
EXECUTOR_TARGET_ENV = "OMO_AGENT_TMUX_TARGET"
EXECUTOR_SESSION_ENV = "CODEX_SESSION_ID"

# 🧑 Source `manager_mail/85c5dff58359-1923.txt:3-4`: "Replace this agent. They simply told me they accepted the task but did not delivery anything or start any worker agent who tells me they are responsible\nI need shit done, not acks"
SOURCE_TASK = "new_dw_manager.md"
SOURCE_TARGET = "dw:0"
SUCCESSOR_TASK = "dw_cleanup_mgr.md"
SUCCESSOR_TARGET = "dw:33"
PARENT_TARGET = "wl:1"
HISTORICAL_TASK = "202608/dw_recon_live_mgr.md"
HISTORICAL_MANAGER = "dw:31"
HISTORICAL_BLOCKER = "paused by direct human shutdown instruction routed to pending_task_items_0912.md; non-human pane dw:33 closed and task record preserved for explicit resume"
HISTORICAL_SHA256 = "3b3ffbb286a2397ad2c422510be002e0e7bbba09c24f3c5d3df2b1705009f5c8"
ARCHIVE_INDEX = "202608/old_todos.md"
AUTHORITY_REF = "manager_mail/85c5dff58359-1923.txt"
AUTHORITY_ENVELOPE_TASK = "dw_mgr_replace.md"
AUTHORITY_LINES = (1, 10)
AUTHORITY_SHA256 = "87aa06a4882cf300f39711a8aba992b8bacce2b7e3988a48df5622fb054bb510"
AUTHORITY_TEXT = """Subject: Re: Accepted: DW status and collaborator-meeting follow-up — dw_gen_submgr.md

Replace this agent. They simply told me they accepted the task but did not delivery anything or start any worker agent who tells me they are responsible
I need shit done, not acks

> On Sep 16, 2026, at 11:04, sichangheagent@gmail.com wrote:
>\x20
> Accepted. We will identify the tasks produced by the previous collaborator meeting transcript, map each active agent to its work, summarize the overall direction, and state any concrete action needed from you. The existing DW submanager will own this status synthesis and email you the result directly.
>\x20
"""
SOURCE_BLOCKER = "existing live successor dw:33 conflicts with archived historical dw:33 ownership and installed helpers cannot yet reconcile/adopt it safely"
SOURCE1923_ITEM = (
    "🧑 Source-1923 (manager_mail/85c5dff58359-1923.txt): replace new_dw_manager.md at dw:0 because it only acknowledged the collaborator-meeting status synthesis; "
    "deliver that synthesis through responsible workers rather than another acknowledgment, and email the Human in the original Source-1923 thread."
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PANE_RE = re.compile(r"^%[0-9]+$")
TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
TASK_RE = re.compile(r"^[A-Za-z0-9_./-]+\.md$")
ENVELOPE_RE = re.compile(r'(?ms)^<human_instruction[ \t]+authoritative="true"[ \t]+source="(?P<source>[^"\r\n]+)">\r?\n(?P<body>.*?)\r?\n</human_instruction>[ \t]*(?:\r?\n|$)')
PACKET_KEYS = {
    "schema",
    "root",
    "source_task",
    "source_target",
    "successor_task",
    "successor_target",
    "parent_target",
    "historical_task",
    "archive_index",
    "executor_target",
    "helper_sha256",
    "authority",
    "authority_sha256",
    "authority_lines",
    "authority_envelope",
    "authority_envelope_sha256",
    "source_sha256",
    "successor_sha256",
    "historical_sha256",
    "todo_sha256",
    "archive_index_sha256",
    "source_pane",
    "source_session_id",
    "successor_pane",
    "successor_session_id",
    "executor_pane",
    "executor_session_id",
    "tree",
    "live_nodes",
    "direct_source_children",
    "direct_successor_children",
    "source_queue_sha256",
    "successor_queue_sha256",
    "final_successor_queue_sha256",
    "files",
    "inputs",
    "packet",
    "audit",
    "preparer",
    "close_proof_secret",
    "close_proof_commitment",
    "binding_id",
}


@dataclass(frozen=True)
class PanePin:
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int


@dataclass(frozen=True)
class TaskPin:
    task: str
    sha256: str


@dataclass(frozen=True)
class LiveNodePin:
    task: str
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    status: str
    session_id: str


@dataclass(frozen=True)
class HeldParent:
    directories: tuple[int, ...]
    directory_identities: tuple[DirectoryIdentity, ...]
    leaf_name: str
    path: Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def json_digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decode(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise TaskFrontmatterError(f"{label} is not canonical base64 text.")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TaskFrontmatterError(f"{label} is not canonical base64 text.") from exc


def parse_pane_pin(value: str) -> PanePin:
    fields = value.split("=")
    if len(fields) != 4:
        raise argparse.ArgumentTypeError("pane pin must be TARGET=PANE_ID=PANE_PID=START_TICKS")
    target, pane_id, raw_pid, raw_ticks = fields
    try:
        pid, ticks = int(raw_pid), int(raw_ticks)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pane pid and start ticks must be integers") from exc
    if TARGET_RE.fullmatch(target) is None or PANE_RE.fullmatch(pane_id) is None or pid <= 1 or ticks <= 0:
        raise argparse.ArgumentTypeError("pane pin identity is invalid")
    return PanePin(target, pane_id, pid, ticks)


def parse_task_pin(value: str) -> TaskPin:
    task, separator, digest = value.partition("=")
    if not separator or TASK_RE.fullmatch(task) is None or SHA256_RE.fullmatch(digest) is None:
        raise argparse.ArgumentTypeError("task pin must be TASK.md=SHA256")
    return TaskPin(task, digest)


def parse_live_node_pin(value: str) -> LiveNodePin:
    fields = value.split("=")
    if len(fields) != 7:
        raise argparse.ArgumentTypeError("live-node pin must be TASK.md=TARGET=PANE_ID=PANE_PID=START_TICKS=STATUS=SESSION_ID")
    task, target, pane_id, raw_pid, raw_ticks, status, session_id = fields
    pane = parse_pane_pin("=".join((target, pane_id, raw_pid, raw_ticks)))
    if TASK_RE.fullmatch(task) is None or status not in {"ready", "running"} or SESSION_RE.fullmatch(session_id) is None:
        raise argparse.ArgumentTypeError("live-node pin identity is invalid")
    return LiveNodePin(task, pane.target, pane.pane_id, pane.pane_pid, pane.pane_start_ticks, status, session_id.lower())


def metadata(data: bytes, root: Path, label: str) -> TaskMetadata:
    try:
        value = parse_task_metadata(data.decode(), root)
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError(f"{label} is not UTF-8.") from exc
    if value is None:
        raise TaskFrontmatterError(f"{label} has no task metadata.")
    return value


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


def trusted_root() -> Path:
    value = os.environ.get(TRUSTED_ROOT_ENV, "")
    root = Path(value)
    if not value or not root.is_absolute():
        raise TaskFrontmatterError(f"{TRUSTED_ROOT_ENV} must name the exact absolute Source-1923 work-log root.")
    resolved = root.resolve(strict=True)
    details = resolved.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or (details.st_dev, details.st_ino) != (TRUSTED_ROOT_DEVICE, TRUSTED_ROOT_INODE):
        raise TaskFrontmatterError("configured work-log root is not the exact Source-1923 repository identity.")
    return resolved


def visible_session_id(target: str) -> str:
    matches = set(re.findall(r"Session:\s*([0-9a-fA-F-]{36})", capture(target, 2000)))
    valid = {value.lower() for value in matches if SESSION_RE.fullmatch(value)}
    return next(iter(valid)) if len(valid) == 1 else ""


def live_session_id(pin: PanePin, protected: tuple[PanePin, ...], *, may_query: bool) -> str:
    def stable() -> bool:
        return current_pin(pin) and all(current_pin(item) for item in protected)

    if not stable():
        raise TaskFrontmatterError(f"live target process identity changed: {pin.target}")
    try:
        process_session = session_from_process(ProcessPanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks))
    except TaskFrontmatterError:
        process_session = ""
    if process_session:
        if SESSION_RE.fullmatch(process_session) is None or not stable():
            raise TaskFrontmatterError(f"live Codex session id could not be authenticated for `{pin.target}`.")
        return process_session.lower()
    visible = visible_session_id(pin.target)
    if visible:
        if not stable():
            raise TaskFrontmatterError(f"live target process identity changed: {pin.target}")
        return visible
    if not may_query or codex_status(pin.target) != "ready":
        raise TaskFrontmatterError(f"live Codex session id is not uniquely visible for `{pin.target}`.")

    session_id, _ = query_status_session_id(
        pin.target,
        2000,
        10.0,
        identity_is_current=stable,
        tmux_guard=(pin.target, pin.pane_id),
        strict_status_response=True,
        expected_pane_pid=pin.pane_pid,
        pre_input_check=lambda: None if stable() else (_ for _ in ()).throw(TaskFrontmatterError("a protected target changed before session query.")),
    )
    if SESSION_RE.fullmatch(session_id) is None or not stable():
        raise TaskFrontmatterError(f"live Codex session id could not be authenticated for `{pin.target}`.")
    return session_id.lower()


def executor_session_id(pin: PanePin) -> str:
    session_id = os.environ.get(EXECUTOR_SESSION_ENV, "").lower()
    if os.environ.get(EXECUTOR_TARGET_ENV) != pin.target or SESSION_RE.fullmatch(session_id) is None or not current_pin(pin):
        raise TaskFrontmatterError("executor environment does not authenticate the exact current Codex target/session.")
    return session_id


def authority_text(path: Path, expected_sha256: str, lines: tuple[int, int]) -> str:
    if path != (trusted_root() / AUTHORITY_REF).resolve(strict=True) or expected_sha256 != AUTHORITY_SHA256 or lines != AUTHORITY_LINES:
        raise TaskFrontmatterError("authority is not the exact Source-1923 file, digest, and line range.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskFrontmatterError("Source-1923 authority must remain owner-private.")
    data = path.read_bytes()
    selected = "\n".join(data.decode().splitlines()[lines[0] - 1 : lines[1]])
    if sha256(data) != expected_sha256 or selected != AUTHORITY_TEXT:
        raise TaskFrontmatterError("Source-1923 authority bytes changed.")
    return selected


def authority_envelope(path: Path, expected_sha256: str, selected: str) -> None:
    if path != (trusted_root() / AUTHORITY_ENVELOPE_TASK).resolve(strict=True):
        raise TaskFrontmatterError("authority envelope is not the exact Source-1923 lifecycle task.")
    data = read_regular(path, expected_sha256)[0]
    matches = [match for match in ENVELOPE_RE.finditer(data.decode()) if match.group("source") == f"{AUTHORITY_REF}:1-10"]
    body = matches[0].group("body").replace("\r\n", "\n").replace("\r", "\n") if len(matches) == 1 else ""
    if len(matches) != 1 or body != selected:
        raise TaskFrontmatterError("authority envelope does not contain the one exact Source-1923 block.")


def task_body(data: bytes) -> bytes:
    parts = data.split(b"\n---\n", 1)
    if len(parts) != 2:
        raise TaskFrontmatterError("task frontmatter boundary is malformed.")
    return parts[1]


def replace_frontmatter_value(text: str, field: str, expected: str, replacement: str, root: Path) -> str:
    lines = text.splitlines(keepends=True)
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise TaskFrontmatterError("task frontmatter boundary is malformed.")
    matches = [index for index in range(1, closing) if lines[index].partition(":")[0] == field]
    if len(matches) != 1:
        raise TaskFrontmatterError(f"task requires exactly one `{field}` field.")
    index = matches[0]
    raw = lines[index].rstrip("\r\n")
    ending = lines[index][len(raw) :]
    key, separator, value = raw.partition(":")
    if value.strip() != expected:
        raise TaskFrontmatterError(f"task `{field}` changed from the exact expected value.")
    lines[index] = f"{key}{separator} {replacement}{ending}"
    updated = "".join(lines)
    if parse_task_metadata(updated, root) is None:
        raise TaskFrontmatterError("updated task metadata is invalid.")
    return updated


def source_after(root: Path, data: bytes) -> bytes:
    cleared = cleared_pending_task_text(data.decode(), root)
    done = update_frontmatter_status(cleared, "done", "", root).rstrip()
    return (f"{done}\n\n(Human-authorized Source-1923 live-successor adoption closed the exact guarded `{SOURCE_TARGET}` pane after all ownership invariants validated.)\n").encode()


def successor_after(root: Path, data: bytes, source_queue: tuple[str, ...]) -> bytes:
    current = metadata(data, root, "successor task")
    merged = (SOURCE1923_ITEM, *current.pending_task_items, *source_queue)
    if len(set(merged)) != len(merged):
        raise TaskFrontmatterError("source, successor, and Source-1923 queues overlap; exact-once preservation is ambiguous.")
    owned = manager_owner_migration_text(data.decode(), SOURCE_TARGET, PARENT_TARGET, root)
    updated = render_pending_items(owned, merged).rstrip()
    envelope = f'<human_instruction authoritative="true" source="{AUTHORITY_REF}:1-10">\n{AUTHORITY_TEXT}\n</human_instruction>'
    return f"{updated}\n{envelope}\n".encode()


def historical_after(root: Path, data: bytes) -> bytes:
    updated = replace_frontmatter_value(data.decode(), "runat", SUCCESSOR_TARGET, "retired", root).encode()
    if task_body(updated) != task_body(data):
        raise TaskFrontmatterError("historical Human hold/evidence body changed during target retirement.")
    return updated


def archive_after(root: Path, data: bytes) -> bytes:
    del root
    text = data.decode()
    lines = text.splitlines(keepends=True)
    rows = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == "dw_recon_live_mgr.md dw:33"]
    if len(rows) != 1 or lines[rows[0]].rstrip("\r\n") != "dw_recon_live_mgr.md dw:33":
        raise TaskFrontmatterError("historical archive index does not contain one exact dw:33 custody row.")
    ending = lines[rows[0]][len(lines[rows[0]].rstrip("\r\n")) :]
    lines[rows[0]] = f"dw_recon_live_mgr.md retired{ending}"
    return "".join(lines).encode()


def todo_after(root: Path, data: bytes) -> bytes:
    return reconcile_done_todo_text(root, root / SOURCE_TASK, data.decode(), SOURCE_TARGET).encode()


def rows_for(root: Path, task: Path, text: str) -> list[tuple[str, str]]:
    section = ""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.strip().endswith(":"):
            section = line.strip()[:-1].casefold()
        elif task in todo_row_task_paths(root, line):
            rows.append((section, line))
    return rows


def collect_tree(root: Path, start_target: str) -> list[dict[str, object]]:
    seen: set[str] = set()
    records: list[dict[str, object]] = []

    def walk(target: str, depth: int) -> None:
        for ref in active_child_task_refs(root, root / SOURCE_TASK, target):
            if ref in seen:
                raise TaskFrontmatterError(f"active ownership tree repeats or cycles at `{ref}`.")
            seen.add(ref)
            data = read_regular_unbound(root / ref)[0]
            item = metadata(data, root, f"active tree task {ref}")
            records.append(
                {
                    "task": ref,
                    "sha256": sha256(data),
                    "queue_sha256": json_digest(list(item.pending_task_items)),
                    "status": item.status,
                    "runat": item.runat,
                    "managerat": item.managerat,
                    "tool": item.tool,
                    "is_manager": item.is_manager,
                    "depth": depth,
                }
            )
            walk(item.runat, depth + 1)

    walk(start_target, 0)
    return sorted(records, key=lambda item: str(item["task"]))


def tree_records(root: Path) -> tuple[list[dict[str, object]], tuple[str, ...], tuple[str, ...]]:
    records = collect_tree(root, SOURCE_TARGET)
    direct_source = tuple(sorted(str(item["task"]) for item in records if item["depth"] == 0))
    successor_records = [item for item in records if item["task"] == SUCCESSOR_TASK]
    if len(successor_records) != 1 or successor_records[0]["runat"] != SUCCESSOR_TARGET:
        raise TaskFrontmatterError("the exact live successor is not one direct source child.")
    direct_successor = tuple(sorted(active_child_task_refs(root, root / SUCCESSOR_TASK, SUCCESSOR_TARGET)))
    return records, direct_source, direct_successor


def validate_tree_pins(records: list[dict[str, object]], pins: tuple[TaskPin, ...]) -> None:
    expected = {pin.task: pin.sha256 for pin in pins}
    observed = {str(item["task"]): str(item["sha256"]) for item in records}
    if len(expected) != len(pins) or expected != observed:
        raise TaskFrontmatterError("complete active child/descendant task set or digest changed.")


def validate_live_node_pins(records: list[dict[str, object]], pins: tuple[LiveNodePin, ...]) -> None:
    by_task = {str(item["task"]): item for item in records}
    supplied = {pin.task: pin for pin in pins}
    if len(supplied) != len(pins):
        raise TaskFrontmatterError("live child/descendant pins contain duplicates.")
    live_tasks: set[str] = set()
    for task, item in by_task.items():
        if task == SUCCESSOR_TASK:
            continue
        target = str(item["runat"])
        pane = exact_pane_id(target)
        if not pane:
            continue
        live_tasks.add(task)
        pin = supplied.get(task)
        if pin is None or pin.target != target or not current_pin(PanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks)):
            raise TaskFrontmatterError(f"live child/descendant process binding changed: {task}")
        if codex_status(target) != pin.status:
            raise TaskFrontmatterError(f"live child/descendant Codex status changed: {task}")
    if set(supplied) != live_tasks:
        raise TaskFrontmatterError("live child/descendant pin set is incomplete or contains an absent target.")


def validate_live_node_sessions(pins: tuple[LiveNodePin, ...], protected: tuple[PanePin, ...]) -> None:
    for pin in pins:
        pane = PanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
        if codex_status(pin.target) != pin.status:
            raise TaskFrontmatterError(f"live child/descendant Codex status changed: {pin.task}")
        if live_session_id(pane, protected, may_query=pin.status == "ready") != pin.session_id:
            raise TaskFrontmatterError(f"live child/descendant Codex session changed: {pin.task}")
        if codex_status(pin.target) != pin.status:
            raise TaskFrontmatterError(f"live child/descendant Codex status changed: {pin.task}")


def validate_initial_shape(
    root: Path,
    source_data: bytes,
    successor_data: bytes,
    historical_data: bytes,
    todo_data: bytes,
    archive_data: bytes,
) -> None:
    source = metadata(source_data, root, "source task")
    successor = metadata(successor_data, root, "successor task")
    historical = metadata(historical_data, root, "historical shared-target task")
    if (
        source.status != "blocked"
        or source.blocked_on != SOURCE_BLOCKER
        or source.runat != SOURCE_TARGET
        or source.managerat != PARENT_TARGET
        or source.tool != "codex"
        or not source.is_manager
        or not source.pending_task_items
        or has_pending_marker(source_data.decode())
    ):
        raise TaskFrontmatterError("source is not the exact blocked Source-1923 predecessor shape.")
    if (
        successor.status != "long_running"
        or successor.runat != SUCCESSOR_TARGET
        or successor.managerat != SOURCE_TARGET
        or successor.tool != "codex"
        or not successor.is_manager
        or not successor.pending_task_items
        or has_pending_marker(successor_data.decode())
    ):
        raise TaskFrontmatterError("successor is not the exact existing live plain-Codex manager shape.")
    if (
        sha256(historical_data) != HISTORICAL_SHA256
        or historical.status != "blocked"
        or historical.blocked_on != HISTORICAL_BLOCKER
        or historical.runat != SUCCESSOR_TARGET
        or historical.managerat != HISTORICAL_MANAGER
        or historical.tool != "codex"
        or not historical.is_manager
        or historical.pending_task_items
        or has_pending_marker(historical_data.decode())
        or "manager closed Codex agent 09-12 09:43 PDT; tmux target `dw:33`" not in historical_data.decode()
    ):
        raise TaskFrontmatterError("historical dw:33 record does not match its exact preserved Human-hold evidence.")
    todo_text = todo_data.decode()
    if rows_for(root, root / SOURCE_TASK, todo_text) != [("current", f"{SOURCE_TASK} {SOURCE_TARGET}")]:
        raise TaskFrontmatterError("source TODO custody is not one exact current dw:0 row.")
    if rows_for(root, root / SUCCESSOR_TASK, todo_text) != [("current", f"{SUCCESSOR_TASK} {SUCCESSOR_TARGET}")]:
        raise TaskFrontmatterError("successor TODO custody is not one exact current dw:33 row.")
    if rows_for(root, root / HISTORICAL_TASK, todo_text):
        raise TaskFrontmatterError("historical dw:33 record unexpectedly appears in root TODO.")
    _ = archive_after(root, archive_data)
    owners = authoritative_active_target_task_paths(root, SUCCESSOR_TARGET)
    if set(owners) != {root / SUCCESSOR_TASK, root / HISTORICAL_TASK}:
        raise TaskFrontmatterError("dw:33 does not have exactly the live successor and historical shared-target records.")
    if authoritative_active_target_task_paths(root, SOURCE_TARGET) != (root / SOURCE_TASK,):
        raise TaskFrontmatterError("dw:0 is not owned solely by the exact Source-1923 predecessor.")


def file_record(task: str, before: bytes, after: bytes) -> dict[str, object]:
    return {
        "task": task,
        "before_base64": encode(before),
        "before_sha256": sha256(before),
        "after_base64": encode(after),
        "after_sha256": sha256(after),
    }


def mutation_files(
    root: Path,
    source_data: bytes,
    successor_data: bytes,
    historical_data: bytes,
    todo_data: bytes,
    archive_data: bytes,
    direct_source: tuple[str, ...],
) -> list[dict[str, object]]:
    source = metadata(source_data, root, "source task")
    files = [
        file_record(SOURCE_TASK, source_data, source_after(root, source_data)),
        file_record(SUCCESSOR_TASK, successor_data, successor_after(root, successor_data, source.pending_task_items)),
        file_record(HISTORICAL_TASK, historical_data, historical_after(root, historical_data)),
    ]
    for ref in direct_source:
        if ref == SUCCESSOR_TASK:
            continue
        before = read_regular_unbound(root / ref)[0]
        files.append(file_record(ref, before, manager_owner_migration_text(before.decode(), SOURCE_TARGET, SUCCESSOR_TARGET, root).encode()))
    files.extend(
        (
            file_record("TODO.md", todo_data, todo_after(root, todo_data)),
            file_record(ARCHIVE_INDEX, archive_data, archive_after(root, archive_data)),
        )
    )
    return files


def packet_plan_state(packet: dict[str, object]) -> tuple[Path, list[dict[str, object]], list[dict[str, object]], tuple[str, ...], tuple[str, ...]]:
    root = validate_exact_scope(packet)
    selected = authority_text(
        Path(str(packet["authority"])).resolve(strict=True),
        str(packet["authority_sha256"]),
        packet_authority_lines(packet),
    )
    authority_envelope(Path(str(packet["authority_envelope"])).resolve(strict=True), str(packet["authority_envelope_sha256"]), selected)
    source_data = read_regular(root / SOURCE_TASK, str(packet["source_sha256"]))[0]
    successor_data = read_regular(root / SUCCESSOR_TASK, str(packet["successor_sha256"]))[0]
    historical_data = read_regular(root / HISTORICAL_TASK, str(packet["historical_sha256"]))[0]
    todo_data = read_regular(root / "TODO.md", str(packet["todo_sha256"]))[0]
    archive_data = read_regular(root / ARCHIVE_INDEX, str(packet["archive_index_sha256"]))[0]
    validate_initial_shape(root, source_data, successor_data, historical_data, todo_data, archive_data)
    records, direct_source, direct_successor = tree_records(root)
    if records != list(packet_tree(packet)) or direct_source != packet_string_list(packet, "direct_source_children") or direct_successor != packet_string_list(packet, "direct_successor_children"):
        raise TaskFrontmatterError("packet no longer binds the exact active child/descendant tree.")
    source = metadata(source_data, root, "source task")
    successor = metadata(successor_data, root, "successor task")
    expected = mutation_files(root, source_data, successor_data, historical_data, todo_data, archive_data, direct_source)
    supplied = file_entries(packet)
    if (
        supplied != expected
        or packet.get("source_queue_sha256") != json_digest(list(source.pending_task_items))
        or packet.get("successor_queue_sha256") != json_digest(list(successor.pending_task_items))
        or packet.get("final_successor_queue_sha256") != json_digest(list(metadata(decode(expected[1]["after_base64"], "successor after image"), root, "updated successor").pending_task_items))
    ):
        raise TaskFrontmatterError("packet mutation plan is not the exact Source-1923 transition.")
    return root, records, expected, direct_source, direct_successor


def private_output(path: Path, inputs: set[Path], label: str) -> Path:
    result = path.expanduser().resolve(strict=False)
    if not result.is_absolute() or result in inputs:
        raise TaskFrontmatterError(f"{label} must be one distinct absolute private output.")
    parent = result.parent.resolve(strict=True)
    info = parent.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskFrontmatterError(f"{label} parent must be owner-private.")
    return result


def read_private(path: Path, expected_sha256: str, label: str) -> bytes:
    resolved = path.resolve(strict=True)
    parent = resolved.parent.stat()
    data, state = read_regular(resolved, expected_sha256)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077 or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o600:
        raise TaskFrontmatterError(f"{label} must remain one owner-private regular file.")
    return data


def helper_sha256() -> str:
    return sha256(Path(__file__).resolve().read_bytes())


def validate_exact_scope(packet: dict[str, object]) -> Path:
    root = Path(str(packet.get("root", ""))).resolve(strict=True)
    fixed = (
        packet.get("source_task"),
        packet.get("source_target"),
        packet.get("successor_task"),
        packet.get("successor_target"),
        packet.get("parent_target"),
        packet.get("historical_task"),
        packet.get("archive_index"),
    )
    if root != trusted_root() or fixed != (
        SOURCE_TASK,
        SOURCE_TARGET,
        SUCCESSOR_TASK,
        SUCCESSOR_TARGET,
        PARENT_TARGET,
        HISTORICAL_TASK,
        ARCHIVE_INDEX,
    ):
        raise TaskFrontmatterError("packet is outside the exact Source-1923 adoption scope.")
    if packet.get("helper_sha256") != helper_sha256():
        raise TaskFrontmatterError("Source-1923 adoption helper bytes changed.")
    return root


def packet_pane(value: object, label: str) -> PanePin:
    if not isinstance(value, dict) or set(value) != {"target", "pane_id", "pane_pid", "pane_start_ticks"}:
        raise TaskFrontmatterError(f"{label} pane binding is malformed.")
    return parse_pane_pin("=".join(str(value[key]) for key in ("target", "pane_id", "pane_pid", "pane_start_ticks")))


def packet_live_nodes(value: object) -> tuple[LiveNodePin, ...]:
    if not isinstance(value, list):
        raise TaskFrontmatterError("packet live-node bindings are malformed.")
    result: list[LiveNodePin] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"task", "target", "pane_id", "pane_pid", "pane_start_ticks", "status", "session_id"}:
            raise TaskFrontmatterError("packet live-node binding is malformed.")
        result.append(parse_live_node_pin("=".join(str(item[key]) for key in ("task", "target", "pane_id", "pane_pid", "pane_start_ticks", "status", "session_id"))))
    return tuple(result)


def packet_string_list(packet: dict[str, object], key: str) -> tuple[str, ...]:
    value = packet.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TaskFrontmatterError(f"packet {key.replace('_', '-')} binding is malformed.")
    return tuple(value)


def packet_tree(packet: dict[str, object]) -> tuple[dict[str, object], ...]:
    value = packet.get("tree")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TaskFrontmatterError("packet tree binding is malformed.")
    return tuple(dict(item) for item in value)


def packet_input_bindings(packet: dict[str, object]) -> dict[Path, tuple[FileIdentity, tuple[DirectoryIdentity, ...]]]:
    values = packet.get("inputs")
    if not isinstance(values, list):
        raise TaskFrontmatterError("packet input identity set is malformed.")
    result: dict[Path, tuple[FileIdentity, tuple[DirectoryIdentity, ...]]] = {}
    for item in values:
        if not isinstance(item, dict):
            raise TaskFrontmatterError("packet input identity is malformed.")
        identity = file_identity_from(item.get("file"), "Source-1923 input")
        raw_ancestors = item.get("ancestors")
        if not isinstance(raw_ancestors, list):
            raise TaskFrontmatterError("packet input ancestor set is malformed.")
        ancestors = tuple(directory_identity_from(value, "Source-1923 input ancestor") for value in raw_ancestors)
        path = Path(identity.path)
        if path in result:
            raise TaskFrontmatterError("packet input identity set contains duplicates.")
        result[path] = identity, ancestors
    return result


def expected_packet_inputs(root: Path, packet: dict[str, object], tree: tuple[dict[str, object], ...]) -> set[Path]:
    return {
        root / SOURCE_TASK,
        root / SUCCESSOR_TASK,
        root / HISTORICAL_TASK,
        root / "TODO.md",
        root / ARCHIVE_INDEX,
        Path(str(packet["authority"])),
        Path(str(packet["authority_envelope"])),
        *(root / str(item["task"]) for item in tree),
    }


def validate_packet_input_files(packet: dict[str, object], root: Path, tree: tuple[dict[str, object], ...]) -> None:
    bindings = packet_input_bindings(packet)
    expected = {path.resolve() for path in expected_packet_inputs(root, packet, tree)}
    if set(bindings) != expected:
        raise TaskFrontmatterError("packet input identity set is incomplete.")
    with ExitStack() as held_files:
        values: list[HeldAbsolute] = []
        for identity, ancestors in bindings.values():
            held = hold_absolute(identity, ancestors)
            values.append(held)
            held_files.callback(os.close, held.descriptor)
            for descriptor in reversed(held.directories):
                held_files.callback(os.close, descriptor)
        for held in values:
            validate_held_absolute(held)


def packet_authority_lines(packet: dict[str, object]) -> tuple[int, int]:
    value = packet.get("authority_lines")
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(item, int) for item in value):
        raise TaskFrontmatterError("packet authority-line binding is malformed.")
    return value[0], value[1]


def validate_packet(value: object, data: bytes) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != PACKET_KEYS or value.get("schema") != SCHEMA:
        raise TaskFrontmatterError("Source-1923 adoption packet is malformed.")
    record = dict(value)
    binding = record.pop("binding_id", None)
    if binding != sha256(canonical_json(record)) or data != canonical_json(value):
        raise TaskFrontmatterError("Source-1923 adoption packet binding changed.")
    return value


def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    if root != trusted_root():
        raise TaskFrontmatterError("Source-1923 adoption requires the exact trusted work-log root.")
    if (ns.source_task, ns.successor_task, ns.historical_task, ns.source_target, ns.successor_target, ns.parent_target) != (
        Path(SOURCE_TASK),
        Path(SUCCESSOR_TASK),
        Path(HISTORICAL_TASK),
        SOURCE_TARGET,
        SUCCESSOR_TARGET,
        PARENT_TARGET,
    ):
        raise TaskFrontmatterError("Source-1923 adoption arguments are outside the exact authorized mapping.")
    if ns.executor_target in {SOURCE_TARGET, SUCCESSOR_TARGET} or ns.executor_pin.target != ns.executor_target:
        raise TaskFrontmatterError("executor must be one distinct pinned target.")
    paths = {
        root / SOURCE_TASK,
        root / SUCCESSOR_TASK,
        root / HISTORICAL_TASK,
        root / "TODO.md",
        root / ARCHIVE_INDEX,
        ns.authority.resolve(strict=True),
        ns.authority_envelope.resolve(strict=True),
    }
    packet_path = private_output(ns.packet, paths, "packet")
    audit_path = private_output(ns.audit, paths | {packet_path}, "audit")
    selected = authority_text(ns.authority.resolve(strict=True), ns.authority_sha256, ns.authority_lines)
    authority_envelope(ns.authority_envelope.resolve(strict=True), ns.authority_envelope_sha256, selected)
    with root_membership_lock(root), ExitStack() as locks:
        records, direct_source, direct_successor = tree_records(root)
        lock_targets = {
            SOURCE_TARGET,
            SUCCESSOR_TARGET,
            ns.executor_target,
            *(str(item["runat"]) for item in records),
            *(pin.target for pin in ns.live_node),
        }
        for target in sorted(lock_targets):
            locks.enter_context(task_target_lock(root, target))
        validate_tree_pins(records, ns.node)
        input_paths = paths | {root / str(item["task"]) for item in records}
        for path in sorted(input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        source_data = read_regular(root / SOURCE_TASK, ns.source_sha256)[0]
        successor_data = read_regular(root / SUCCESSOR_TASK, ns.successor_sha256)[0]
        historical_data = read_regular(root / HISTORICAL_TASK, ns.historical_sha256)[0]
        todo_data = read_regular(root / "TODO.md", ns.todo_sha256)[0]
        archive_data = read_regular(root / ARCHIVE_INDEX, ns.archive_index_sha256)[0]
        validate_initial_shape(root, source_data, successor_data, historical_data, todo_data, archive_data)
        current_records, current_direct_source, current_direct_successor = tree_records(root)
        if (current_records, current_direct_source, current_direct_successor) != (records, direct_source, direct_successor):
            raise TaskFrontmatterError("active child/descendant tree changed under lifecycle locks.")
        validate_live_node_pins(records, ns.live_node)
        if not current_pin(ns.source_pin) or ns.source_pin.target != SOURCE_TARGET or codex_status(SOURCE_TARGET) != "ready":
            raise TaskFrontmatterError("stale dw:0 is not the exact ready source pane.")
        if not current_pin(ns.successor_pin) or ns.successor_pin.target != SUCCESSOR_TARGET or codex_status(SUCCESSOR_TARGET) not in {"ready", "running"}:
            raise TaskFrontmatterError("dw:33 is not the exact live successor pane.")
        if not current_pin(ns.executor_pin) or codex_status(ns.executor_target) not in {"ready", "running"}:
            raise TaskFrontmatterError("executor is not the exact live Codex pane.")
        protected = (ns.successor_pin, ns.executor_pin, *(PanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks) for pin in ns.live_node))
        all_live = (ns.source_pin, *protected)
        source_session = live_session_id(ns.source_pin, all_live, may_query=True)
        successor_session = live_session_id(ns.successor_pin, all_live, may_query=codex_status(SUCCESSOR_TARGET) == "ready")
        executor_session = executor_session_id(ns.executor_pin)
        validate_live_node_sessions(tuple(ns.live_node), all_live)
        source = metadata(source_data, root, "source task")
        successor = metadata(successor_data, root, "successor task")
        files = mutation_files(root, source_data, successor_data, historical_data, todo_data, archive_data, direct_source)
        identities = []
        for path in sorted(input_paths, key=str):
            _, identity, ancestors = absolute_file_binding(path.resolve(), f"Source-1923 input {path}")
            identities.append({"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]})
        secret = secrets.token_hex(32)
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(root),
            "source_task": SOURCE_TASK,
            "source_target": SOURCE_TARGET,
            "successor_task": SUCCESSOR_TASK,
            "successor_target": SUCCESSOR_TARGET,
            "parent_target": PARENT_TARGET,
            "historical_task": HISTORICAL_TASK,
            "archive_index": ARCHIVE_INDEX,
            "executor_target": ns.executor_target,
            "helper_sha256": helper_sha256(),
            "authority": str(ns.authority.resolve(strict=True)),
            "authority_sha256": ns.authority_sha256,
            "authority_lines": list(ns.authority_lines),
            "authority_envelope": str(ns.authority_envelope.resolve(strict=True)),
            "authority_envelope_sha256": ns.authority_envelope_sha256,
            "source_sha256": ns.source_sha256,
            "successor_sha256": ns.successor_sha256,
            "historical_sha256": ns.historical_sha256,
            "todo_sha256": ns.todo_sha256,
            "archive_index_sha256": ns.archive_index_sha256,
            "source_pane": asdict(ns.source_pin),
            "source_session_id": source_session,
            "successor_pane": asdict(ns.successor_pin),
            "successor_session_id": successor_session,
            "executor_pane": asdict(ns.executor_pin),
            "executor_session_id": executor_session,
            "tree": records,
            "live_nodes": [asdict(pin) for pin in sorted(ns.live_node, key=lambda item: item.task)],
            "direct_source_children": list(direct_source),
            "direct_successor_children": list(direct_successor),
            "source_queue_sha256": json_digest(list(source.pending_task_items)),
            "successor_queue_sha256": json_digest(list(successor.pending_task_items)),
            "final_successor_queue_sha256": json_digest(list(metadata(decode(files[1]["after_base64"], "successor after image"), root, "updated successor").pending_task_items)),
            "files": files,
            "inputs": identities,
            "packet": str(packet_path),
            "audit": str(audit_path),
            "preparer": ns.preparer,
            "close_proof_secret": secret,
            "close_proof_commitment": sha256(secret.encode()),
        }
        packet["binding_id"] = sha256(canonical_json(packet))
        publish_or_validate(packet_path, canonical_json(packet), "Source-1923 adoption packet")
    print(packet_path)


def review_record(path: Path, expected_sha256: str, packet: dict[str, object], packet_sha256: str) -> dict[str, object]:
    data = read_private(path, expected_sha256, "independent review")
    try:
        value: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("independent review is not canonical JSON.") from exc
    expected_keys = {"schema", "verdict", "packet_sha256", "helper_sha256", "reviewer"}
    if not isinstance(value, dict) or set(value) != expected_keys or data != canonical_json(value):
        raise TaskFrontmatterError("independent review is malformed.")
    reviewer = value.get("reviewer")
    if (
        value.get("schema") != REVIEW_SCHEMA
        or value.get("verdict") != "PASS"
        or value.get("packet_sha256") != packet_sha256
        or value.get("helper_sha256") != packet.get("helper_sha256")
        or not isinstance(reviewer, str)
        or not reviewer.strip()
        or reviewer == packet.get("preparer")
        or reviewer in {SOURCE_TARGET, SUCCESSOR_TARGET, packet.get("executor_target")}
    ):
        raise TaskFrontmatterError("independent review does not PASS this exact packet and helper.")
    return {str(key): item for key, item in value.items()}


def prepared_audit(packet: dict[str, object], packet_sha256: str, review_path: Path, review_sha256: str, reviewer: str) -> bytes:
    retained = {key: packet[key] for key in packet if key not in {"close_proof_secret", "files", "inputs"}}
    return canonical_json(
        {
            **retained,
            "operation": CLOSE_OPERATION,
            "state": "prepared",
            "packet_sha256": packet_sha256,
            "review": str(review_path.resolve(strict=True)),
            "review_sha256": review_sha256,
            "reviewer": reviewer,
        }
    )


def validate_close_authority_file(
    audit_path: Path,
    commitment: str,
    target: str,
    pane_id: str,
    pane_pid: int,
    pane_start_ticks: int,
    expected_audit_sha256: str,
    *,
    closed_identity: bool = False,
) -> None:
    del closed_identity
    data, state = read_regular_unbound(audit_path)
    try:
        record: object = json.loads(data)
        if not isinstance(record, dict):
            raise RuntimeError("Source-1923 close audit is not an object.")
        packet_path = Path(str(record.get("packet", ""))).resolve(strict=True)
        packet_sha256 = str(record.get("packet_sha256", ""))
        packet_data = read_private(packet_path, packet_sha256, "Source-1923 packet")
        packet = validate_packet(json.loads(packet_data), packet_data)
        if Path(str(packet["packet"])).resolve(strict=True) != packet_path:
            raise RuntimeError("Source-1923 packet path binding changed.")
        configured_audit = Path(str(packet["audit"]))
        expected_audit_path = configured_audit.with_name(f"{configured_audit.name}.prepared")
        if audit_path.resolve(strict=True) != expected_audit_path.resolve(strict=False):
            raise RuntimeError("Source-1923 prepared audit path binding changed.")
        review_path = Path(str(record.get("review", ""))).resolve(strict=True)
        review_sha256 = str(record.get("review_sha256", ""))
        review = review_record(review_path, review_sha256, packet, packet_sha256)
        if data != prepared_audit(packet, packet_sha256, review_path, review_sha256, str(review["reviewer"])):
            raise RuntimeError("Source-1923 close audit does not bind its exact packet and review.")
        plan_root, plan_tree, _, _, _ = packet_plan_state(packet)
        validate_packet_input_files(packet, plan_root, tuple(plan_tree))
    except (CustodyError, TaskFrontmatterError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Source-1923 close audit is invalid.") from exc
    source_pane = record.get("source_pane")
    if (
        data != canonical_json(record)
        or stat.S_IMODE(state.st_mode) != 0o600
        or state.st_uid != os.getuid()
        or sha256(data) != expected_audit_sha256
        or record.get("schema") != SCHEMA
        or record.get("operation") != CLOSE_OPERATION
        or record.get("state") != "prepared"
        or Path(str(record.get("root", ""))).resolve(strict=True) != trusted_root()
        or record.get("source_task") != SOURCE_TASK
        or record.get("source_target") != SOURCE_TARGET
        or record.get("successor_task") != SUCCESSOR_TASK
        or record.get("successor_target") != SUCCESSOR_TARGET
        or record.get("historical_task") != HISTORICAL_TASK
        or record.get("authority_sha256") != AUTHORITY_SHA256
        or record.get("authority_lines") != list(AUTHORITY_LINES)
        or record.get("close_proof_commitment") != commitment
        or source_pane != {"target": target, "pane_id": pane_id, "pane_pid": pane_pid, "pane_start_ticks": pane_start_ticks}
        or target != SOURCE_TARGET
    ):
        raise RuntimeError("Source-1923 close audit drifted before exact pane kill.")


def stop_source(
    pin: PanePin,
    session_id: str,
    proof_path: Path,
    audit_path: Path,
    secret: str,
    commitment: str,
    audit_sha256: str,
    pre_input_check: Callable[[], None],
) -> None:
    observed = guarded_codex_stop(
        CodexStopArgs(
            target=SOURCE_TARGET,
            wait_s=10.0,
            lines=2000,
            dry_run=False,
            allow_self=False,
            no_feedback=True,
            bound_symbolic_target=SOURCE_TARGET,
            bound_pane_id=pin.pane_id,
            bound_pane_pid=pin.pane_pid,
            bound_pane_start_ticks=pin.pane_start_ticks,
            bound_expected_session_id=session_id,
            bound_pre_input_check=pre_input_check,
            bound_close_proof_path=str(proof_path),
            bound_close_audit_path=str(audit_path),
            bound_close_proof_secret=secret,
            bound_close_proof_commitment=commitment,
            bound_close_operation=CLOSE_OPERATION,
            bound_close_audit_sha256=audit_sha256,
        )
    )
    if observed.lower() != session_id.lower():
        raise TaskFrontmatterError("guarded Source-1923 close returned a different Codex session.")


def stage_name(task: str, binding_id: str) -> str:
    return f".{Path(task).name}.omo-source1923-{binding_id[:24]}"


def directory_values(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.S_IMODE(value.st_mode), value.st_dev, value.st_ino, value.st_uid, value.st_gid


def entry_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def hold_parent(identity: FileIdentity, ancestors: tuple[DirectoryIdentity, ...]) -> HeldParent:
    path = Path(identity.path)
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise CustodyError(f"bound mutation is not one normalized absolute path: {identity.path}")
    components = path.parts[1:]
    descriptors: list[int] = []
    observed: list[DirectoryIdentity] = []
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        descriptors.append(parent)
        details = os.fstat(parent)
        observed.append(DirectoryIdentity("/", stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino, details.st_uid, details.st_gid))
        current = Path("/")
        for component in components[:-1]:
            parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            descriptors.append(parent)
            current /= component
            details = os.fstat(parent)
            observed.append(DirectoryIdentity(str(current), stat.S_IMODE(details.st_mode), details.st_dev, details.st_ino, details.st_uid, details.st_gid))
        if tuple(observed) != ancestors:
            raise CustodyError(f"bound mutation parent identity drifted: {identity.path}")
        held = HeldParent(tuple(descriptors), ancestors, components[-1], path.parent)
        validate_held_parent(held)
        return held
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def validate_held_parent(held: HeldParent) -> None:
    for index, (descriptor, expected) in enumerate(zip(held.directories, held.directory_identities, strict=True)):
        linked = os.stat(
            "/" if index == 0 else Path(expected.path).name,
            dir_fd=None if index == 0 else held.directories[index - 1],
            follow_symlinks=False,
        )
        expected_values = expected.mode, expected.device, expected.inode, expected.uid, expected.gid
        if directory_values(os.fstat(descriptor)) != expected_values or directory_values(linked) != expected_values or not stat.S_ISDIR(linked.st_mode):
            raise CustodyError(f"bound mutation parent identity drifted: {expected.path}")


def entry_exists_at(held: HeldParent, name: str) -> bool:
    validate_held_parent(held)
    try:
        os.stat(name, dir_fd=held.directories[-1], follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def read_at(held: HeldParent, name: str, label: str) -> bytes:
    validate_held_parent(held)
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=held.directories[-1])
    try:
        before = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=held.directories[-1], follow_symlinks=False)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=held.directories[-1], follow_symlinks=False)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or entry_identity(before) != entry_identity(linked)
        or entry_identity(before) != entry_identity(after)
        or entry_identity(before) != entry_identity(rebound)
    ):
        raise TaskFrontmatterError(f"{label} changed while read.")
    validate_held_parent(held)
    return b"".join(chunks)


def recover_stages(root: Path, files: list[dict[str, object]], binding_id: str, held_parents: dict[Path, HeldParent]) -> None:
    actions: list[tuple[HeldParent, str, str, bytes, bytes]] = []
    for entry in reversed(files):
        task = str(entry["task"])
        path = root / task
        held = held_parents[path]
        staged_name = stage_name(task, binding_id)
        if not entry_exists_at(held, staged_name):
            continue
        path_data = read_at(held, held.leaf_name, f"Source-1923 task {task}")
        staged_data = read_at(held, staged_name, f"Source-1923 stage {task}")
        before = decode(entry["before_base64"], f"{task} before image")
        after = decode(entry["after_base64"], f"{task} after image")
        if path_data == after and staged_data == before:
            actions.append((held, staged_name, "exchange", before, after))
        elif path_data == before and staged_data == after:
            actions.append((held, staged_name, "unlink", before, after))
        else:
            raise TaskFrontmatterError(f"interrupted Source-1923 stage is not recoverable: {task}")
    for held in held_parents.values():
        validate_held_parent(held)
    for held, staged_name, action, before, after in actions:
        path_data = read_at(held, held.leaf_name, f"recovering Source-1923 task {held.leaf_name}")
        staged_data = read_at(held, staged_name, f"recovering Source-1923 stage {held.leaf_name}")
        if action == "exchange":
            if path_data != after or staged_data != before:
                raise TaskFrontmatterError(f"interrupted Source-1923 stage changed before recovery: {held.leaf_name}")
            validate_held_parent(held)
            rename_exchange(held.directories[-1], staged_name, held.leaf_name)
            os.fsync(held.directories[-1])
            path_data = read_at(held, held.leaf_name, f"restored Source-1923 task {held.leaf_name}")
            staged_data = read_at(held, staged_name, f"restored Source-1923 stage {held.leaf_name}")
        elif path_data != before or staged_data != after:
            raise TaskFrontmatterError(f"interrupted Source-1923 stage changed before recovery: {held.leaf_name}")
        if path_data != before or staged_data != after:
            raise TaskFrontmatterError(f"interrupted Source-1923 stage recovery failed: {held.leaf_name}")
        validate_held_parent(held)
        os.unlink(staged_name, dir_fd=held.directories[-1])
        os.fsync(held.directories[-1])
        validate_held_parent(held)


def exchange_files(
    root: Path,
    files: list[dict[str, object]],
    binding_id: str,
    held_by_path: dict[Path, HeldAbsolute],
    final_check: Callable[[], None],
) -> None:
    staged: list[tuple[HeldAbsolute, str]] = []
    swapped: list[tuple[Path, str]] = []
    try:
        for entry in files:
            task = str(entry["task"])
            path = root / task
            held = held_by_path[path]
            validate_held_absolute(held)
            name = stage_name(task, binding_id)
            parent_fd = held.directories[-1]
            after = decode(entry["after_base64"], f"{task} after image")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), held.identity.mode, dir_fd=parent_fd)
            staged.append((held, name))
            try:
                remaining = memoryview(after)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise TaskFrontmatterError("Source-1923 staging made no write progress.")
                    remaining = remaining[written:]
                os.fchmod(fd, held.identity.mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent_fd)
        for held in held_by_path.values():
            validate_held_absolute(held)
        for entry in files:
            task = str(entry["task"])
            path = root / task
            held = held_by_path[path]
            name = stage_name(task, binding_id)
            rename_exchange(held.directories[-1], name, held.leaf_name)
            swapped.append((path, name))
            os.fsync(held.directories[-1])
            parent = HeldParent(held.directories, held.directory_identities, held.leaf_name, path.parent)
            if read_at(parent, held.leaf_name, f"published Source-1923 task {task}") != decode(entry["after_base64"], f"{task} after image"):
                raise TaskFrontmatterError(f"Source-1923 exchange did not publish exact bytes: {task}")
        final_check()
    except Exception as exc:
        failures: list[str] = []
        for path, name in reversed(swapped):
            try:
                held = held_by_path[path]
                rename_exchange(held.directories[-1], name, held.leaf_name)
                os.fsync(held.directories[-1])
            except Exception as rollback_error:
                failures.append(f"{path}: {rollback_error}")
        if not failures:
            for held, name in staged:
                try:
                    os.unlink(name, dir_fd=held.directories[-1])
                except FileNotFoundError:
                    pass
                except OSError as cleanup_error:
                    failures.append(f"{held.identity.path}: {cleanup_error}")
        if failures:
            raise TaskFrontmatterError(f"Source-1923 rollback failed: {'; '.join(failures)}") from exc
        raise


def cleanup_committed_stages(root: Path, files: list[dict[str, object]], binding_id: str, held_parents: dict[Path, HeldParent]) -> None:
    removable: list[tuple[HeldParent, str]] = []
    for entry in files:
        task = str(entry["task"])
        path = root / task
        held = held_parents[path]
        staged_name = stage_name(task, binding_id)
        if not entry_exists_at(held, staged_name):
            continue
        if read_at(held, staged_name, f"committed Source-1923 stage {task}") != decode(entry["before_base64"], f"{task} before image"):
            raise TaskFrontmatterError(f"committed Source-1923 stage is not the exact displaced before image: {task}")
        removable.append((held, staged_name))
    for held in held_parents.values():
        validate_held_parent(held)
    for held, staged_name in removable:
        validate_held_parent(held)
        os.unlink(staged_name, dir_fd=held.directories[-1])
        os.fsync(held.directories[-1])
        validate_held_parent(held)


def cleanup_committed_close_markers(proof_path: Path, prepared_path: Path, commitment: str, prepared_sha: str) -> None:
    started_path = done_live_close_started_path(prepared_path)
    proof_exists = path_entry_exists(proof_path)
    started_exists = path_entry_exists(started_path)
    if proof_exists and not has_bound_close_proof(proof_path, commitment, prepared_sha, CLOSE_OPERATION):
        raise TaskFrontmatterError("committed Source-1923 close proof is malformed.")
    if started_exists:
        if not proof_exists:
            raise TaskFrontmatterError("committed Source-1923 close-started marker lacks its final proof.")
        proof_state = proof_path.lstat()
        started_state = started_path.lstat()
        if (proof_state.st_dev, proof_state.st_ino) != (started_state.st_dev, started_state.st_ino):
            raise TaskFrontmatterError("committed Source-1923 close markers are not one atomic promotion.")
        started_path.unlink()
    if proof_exists:
        proof_path.unlink()
    if proof_exists or started_exists:
        parent_fd = os.open(prepared_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def file_entries(packet: dict[str, object]) -> list[dict[str, object]]:
    values = packet.get("files")
    if not isinstance(values, list) or not values:
        raise TaskFrontmatterError("packet file images are malformed.")
    result: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"task", "before_base64", "before_sha256", "after_base64", "after_sha256"}:
            raise TaskFrontmatterError("packet file image is malformed.")
        task = value.get("task")
        if not isinstance(task, str) or TASK_RE.fullmatch(task) is None:
            raise TaskFrontmatterError("packet file task is malformed.")
        before = decode(value.get("before_base64"), f"{task} before image")
        after = decode(value.get("after_base64"), f"{task} after image")
        if sha256(before) != value.get("before_sha256") or sha256(after) != value.get("after_sha256"):
            raise TaskFrontmatterError("packet file image digest changed.")
        result.append({str(key): item for key, item in value.items()})
    if len({str(item["task"]) for item in result}) != len(result):
        raise TaskFrontmatterError("packet file image set contains duplicates.")
    return result


def validate_post_exchange_bindings(
    held_parents: dict[Path, HeldParent],
    held_inputs: dict[Path, HeldAbsolute],
    mutated_paths: set[Path],
) -> None:
    for held in held_parents.values():
        validate_held_parent(held)
    for path, held in held_inputs.items():
        if path not in mutated_paths:
            validate_held_absolute(held)


def task_namespace_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    records: list[tuple[object, ...]] = []
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(root).as_posix()
            details = current_path.lstat()
            if not stat.S_ISDIR(details.st_mode):
                raise TaskFrontmatterError(f"task namespace directory changed: {relative}")
            records.append(
                (
                    "directory",
                    relative,
                    details.st_dev,
                    details.st_ino,
                    stat.S_IMODE(details.st_mode),
                    details.st_uid,
                    details.st_gid,
                    details.st_nlink,
                    details.st_mtime_ns,
                    details.st_ctime_ns,
                )
            )
            directories[:] = sorted(directories)
            for name in sorted(item for item in files if item.endswith(".md")):
                path = current_path / name
                item = path.lstat()
                records.append(
                    (
                        "task",
                        path.relative_to(root).as_posix(),
                        item.st_dev,
                        item.st_ino,
                        stat.S_IMODE(item.st_mode),
                        item.st_uid,
                        item.st_gid,
                        item.st_size,
                        item.st_mtime_ns,
                        item.st_ctime_ns,
                    )
                )
    except OSError as exc:
        raise TaskFrontmatterError(f"task namespace changed while scanned: {exc}") from exc
    return tuple(records)


def final_state_check(
    root: Path,
    packet: dict[str, object],
    files: list[dict[str, object]],
    protected: tuple[PanePin, ...],
    held_parents: dict[Path, HeldParent],
    held_inputs: dict[Path, HeldAbsolute],
) -> None:
    mutated_paths = {root / str(entry["task"]) for entry in files}
    validate_post_exchange_bindings(held_parents, held_inputs, mutated_paths)
    for entry in files:
        path = root / str(entry["task"])
        held = held_parents[path]
        if sha256(read_at(held, held.leaf_name, f"committed Source-1923 task {entry['task']}")) != entry["after_sha256"]:
            raise TaskFrontmatterError(f"committed Source-1923 bytes changed: {entry['task']}")
    source_held = held_parents[root / SOURCE_TASK]
    successor_held = held_parents[root / SUCCESSOR_TASK]
    historical_held = held_parents[root / HISTORICAL_TASK]
    source = metadata(read_at(source_held, source_held.leaf_name, "committed source"), root, "committed source")
    successor = metadata(read_at(successor_held, successor_held.leaf_name, "committed successor"), root, "committed successor")
    historical_data = read_at(historical_held, historical_held.leaf_name, "committed historical record")
    historical = metadata(historical_data, root, "committed historical record")
    if source.status != "done" or source.pending_task_items or source.runat != SOURCE_TARGET:
        raise TaskFrontmatterError("committed source is not the exact closed predecessor.")
    if (
        successor.status != "long_running"
        or successor.runat != SUCCESSOR_TARGET
        or successor.managerat != PARENT_TARGET
        or json_digest(list(successor.pending_task_items)) != packet["final_successor_queue_sha256"]
    ):
        raise TaskFrontmatterError("committed successor ownership or queue changed.")
    if (
        historical.status != "blocked"
        or historical.runat != "retired"
        or historical.blocked_on != HISTORICAL_BLOCKER
        or task_body(historical_data) != task_body(decode(next(item for item in files if item["task"] == HISTORICAL_TASK)["before_base64"], "historical before image"))
    ):
        raise TaskFrontmatterError("historical Human hold/evidence was not preserved exactly.")
    if exact_pane_id(SOURCE_TARGET) or authoritative_active_target_task_paths(root, SOURCE_TARGET):
        raise TaskFrontmatterError("stale dw:0 remains live or authoritatively active.")
    if active_child_task_refs(root, root / SOURCE_TASK, SOURCE_TARGET):
        raise TaskFrontmatterError("stale dw:0 retains an active child after Source-1923 adoption.")
    if authoritative_active_target_task_paths(root, SUCCESSOR_TARGET) != (root / SUCCESSOR_TASK,):
        raise TaskFrontmatterError("dw:33 is not the singular active successor owner.")
    expected_direct = set(packet_string_list(packet, "direct_source_children")) | set(packet_string_list(packet, "direct_successor_children"))
    expected_direct.discard(SUCCESSOR_TASK)
    if set(active_child_task_refs(root, root / SUCCESSOR_TASK, SUCCESSOR_TARGET)) != expected_direct:
        raise TaskFrontmatterError("successor does not own the complete exact direct-child union.")
    current_tree = collect_tree(root, SUCCESSOR_TARGET)
    current_by_task = {str(item["task"]): item for item in current_tree}
    prior_tree = packet_tree(packet)
    expected_tasks = {str(item["task"]) for item in prior_tree if item["task"] != SUCCESSOR_TASK}
    if set(current_by_task) != expected_tasks:
        raise TaskFrontmatterError("active descendant set changed during Source-1923 adoption.")
    transformed = {str(item["task"]): str(item["after_sha256"]) for item in files}
    for raw in prior_tree:
        if not isinstance(raw, dict):
            raise TaskFrontmatterError("packet tree binding is malformed.")
        task = str(raw["task"])
        if task == SUCCESSOR_TASK:
            continue
        current = current_by_task.get(task)
        expected_sha256 = transformed.get(task, str(raw["sha256"]))
        if current is None or current["sha256"] != expected_sha256 or current["queue_sha256"] != raw["queue_sha256"] or current["runat"] != raw["runat"]:
            raise TaskFrontmatterError(f"active descendant or ordered queue changed: {task}")
    if any(not current_pin(pin) for pin in protected):
        raise TaskFrontmatterError("a protected live target changed during Source-1923 adoption.")
    successor_pin = packet_pane(packet["successor_pane"], "successor")
    executor_pin = packet_pane(packet["executor_pane"], "executor")
    if executor_session_id(executor_pin) != packet["executor_session_id"]:
        raise TaskFrontmatterError("executor Codex session changed during Source-1923 adoption.")
    if live_session_id(successor_pin, protected, may_query=codex_status(SUCCESSOR_TARGET) == "ready") != packet["successor_session_id"]:
        raise TaskFrontmatterError("dw:33 successor Codex session changed during Source-1923 adoption.")
    validate_live_node_sessions(packet_live_nodes(packet["live_nodes"]), protected)
    for entry in files:
        path = root / str(entry["task"])
        held = held_parents[path]
        if sha256(read_at(held, held.leaf_name, f"committed Source-1923 task {entry['task']}")) != entry["after_sha256"]:
            raise TaskFrontmatterError(f"committed Source-1923 bytes changed: {entry['task']}")
    validate_post_exchange_bindings(held_parents, held_inputs, mutated_paths)


def execute(ns: argparse.Namespace) -> None:
    packet_data = read_private(ns.packet, ns.packet_sha256, "Source-1923 packet")
    packet = validate_packet(json.loads(packet_data), packet_data)
    root = validate_exact_scope(packet)
    if Path(str(packet["packet"])).resolve(strict=True) != ns.packet.resolve(strict=True):
        raise TaskFrontmatterError("packet does not bind its exact reviewed path.")
    review = review_record(ns.review, ns.review_sha256, packet, ns.packet_sha256)
    files = file_entries(packet)
    source_pin = packet_pane(packet["source_pane"], "source")
    successor_pin = packet_pane(packet["successor_pane"], "successor")
    executor_pin = packet_pane(packet["executor_pane"], "executor")
    live_nodes = packet_live_nodes(packet["live_nodes"])
    tree = packet_tree(packet)
    protected = (successor_pin, executor_pin, *(PanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks) for pin in live_nodes))
    audit_path = Path(str(packet["audit"]))
    prepared_data = prepared_audit(packet, ns.packet_sha256, ns.review.resolve(strict=True), ns.review_sha256, str(review["reviewer"]))
    prepared_path = audit_path.with_name(f"{audit_path.name}.prepared")
    prepared_sha = sha256(prepared_data)
    proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
    committed_data = canonical_json({**json.loads(prepared_data), "state": "committed"})
    input_bindings = packet_input_bindings(packet)
    expected_input_paths = expected_packet_inputs(root, packet, tree)
    if set(input_bindings) != {path.resolve() for path in expected_input_paths}:
        raise TaskFrontmatterError("packet input identity set is incomplete.")
    targets = {
        SOURCE_TARGET,
        SUCCESSOR_TARGET,
        str(packet["executor_target"]),
        *(str(item["runat"]) for item in tree),
        *(pin.target for pin in live_nodes),
    }
    with root_membership_lock(root), ExitStack() as locks:
        for target in sorted(targets):
            locks.enter_context(task_target_lock(root, target))
        for path in sorted(expected_input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        held_parents: dict[Path, HeldParent] = {}
        try:
            for path, (identity, ancestors) in input_bindings.items():
                held_parents[path] = hold_parent(identity, ancestors)
        except Exception:
            for held in held_parents.values():
                for descriptor in reversed(held.directories):
                    os.close(descriptor)
            raise
        for held in held_parents.values():
            for descriptor in reversed(held.directories):
                locks.callback(os.close, descriptor)
        for held in held_parents.values():
            validate_held_parent(held)
        committed_exists = existing_exact(audit_path, committed_data, "Source-1923 committed audit") if path_entry_exists(audit_path) else False
        prepared_exists = existing_exact(prepared_path, prepared_data, "Source-1923 prepared audit") if path_entry_exists(prepared_path) else False
        if committed_exists:
            cleanup_committed_stages(root, files, str(packet["binding_id"]), held_parents)
            cleanup_committed_close_markers(proof_path, prepared_path, str(packet["close_proof_commitment"]), prepared_sha)
            print(audit_path)
            return
        if not committed_exists:
            recover_stages(root, files, str(packet["binding_id"]), held_parents)
        current_file_states = {
            str(entry["task"]): sha256(
                read_at(
                    held_parents[root / str(entry["task"])],
                    held_parents[root / str(entry["task"])].leaf_name,
                    f"Source-1923 task {entry['task']}",
                )
            )
            for entry in files
        }
        before_state = all(current_file_states[str(entry["task"])] == entry["before_sha256"] for entry in files)
        after_state = all(current_file_states[str(entry["task"])] == entry["after_sha256"] for entry in files)
        if not before_state and not after_state:
            raise TaskFrontmatterError("lifecycle files match neither the exact initial nor committed Source-1923 state.")
        mutated_paths = {root / str(entry["task"]) for entry in files}
        paths_to_hold = set(input_bindings) if before_state else set(input_bindings) - mutated_paths
        held_by_path: dict[Path, HeldAbsolute] = {}
        for path in paths_to_hold:
            identity, ancestors = input_bindings[path]
            held = hold_absolute(identity, ancestors)
            held_by_path[path] = held
            locks.callback(os.close, held.descriptor)
            for descriptor in reversed(held.directories):
                locks.callback(os.close, descriptor)
        if set(held_by_path) != paths_to_hold:
            raise TaskFrontmatterError("held Source-1923 input set changed.")
        for held in held_by_path.values():
            validate_held_absolute(held)
        if before_state:
            selected = authority_text(
                Path(str(packet["authority"])).resolve(strict=True),
                str(packet["authority_sha256"]),
                packet_authority_lines(packet),
            )
            authority_envelope(Path(str(packet["authority_envelope"])).resolve(strict=True), str(packet["authority_envelope_sha256"]), selected)
            source_data = read_regular_unbound(root / SOURCE_TASK)[0]
            successor_data = read_regular_unbound(root / SUCCESSOR_TASK)[0]
            historical_data = read_regular_unbound(root / HISTORICAL_TASK)[0]
            todo_data = read_regular_unbound(root / "TODO.md")[0]
            archive_data = read_regular_unbound(root / ARCHIVE_INDEX)[0]
            validate_initial_shape(root, source_data, successor_data, historical_data, todo_data, archive_data)
            records, direct_source, direct_successor = tree_records(root)
            if records != list(tree) or direct_source != packet_string_list(packet, "direct_source_children") or direct_successor != packet_string_list(packet, "direct_successor_children"):
                raise TaskFrontmatterError("active child/descendant tree changed after packet preparation.")
            _, rebound_records, rebound_files, rebound_direct_source, rebound_direct_successor = packet_plan_state(packet)
            if rebound_records != records or rebound_files != files or rebound_direct_source != direct_source or rebound_direct_successor != direct_successor:
                raise TaskFrontmatterError("Source-1923 packet plan changed after lifecycle lock acquisition.")
            validate_live_node_pins(records, live_nodes)
            validate_live_node_sessions(live_nodes, protected)
            existing_proof = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION)
            source_live = current_pin(source_pin) and codex_status(SOURCE_TARGET) == "ready"
            if not source_live and exact_pane_id(SOURCE_TARGET) == "" and not existing_proof and prepared_exists and path_entry_exists(done_live_close_started_path(prepared_path)):
                promote_done_live_close_started(
                    proof_path,
                    prepared_path,
                    str(packet["close_proof_commitment"]),
                    prepared_sha,
                    SOURCE_TARGET,
                    source_pin.pane_id,
                    source_pin.pane_pid,
                    source_pin.pane_start_ticks,
                    CLOSE_OPERATION,
                )
                existing_proof = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION)
            if not source_live and not (exact_pane_id(SOURCE_TARGET) == "" and existing_proof):
                raise TaskFrontmatterError("stale dw:0 pane/process changed before guarded close.")
            if not current_pin(successor_pin) or codex_status(SUCCESSOR_TARGET) not in {"ready", "running"}:
                raise TaskFrontmatterError("dw:33 successor pane/process changed before guarded close.")
            if not current_pin(executor_pin) or codex_status(executor_pin.target) not in {"ready", "running"} or any(not current_pin(pin) for pin in protected):
                raise TaskFrontmatterError("a protected pane/process changed before guarded close.")
            if executor_session_id(executor_pin) != packet["executor_session_id"]:
                raise TaskFrontmatterError("executor Codex session changed after packet preparation.")
            if source_live and live_session_id(source_pin, protected, may_query=True) != packet["source_session_id"]:
                raise TaskFrontmatterError("stale dw:0 Codex session changed after packet preparation.")
            retained_nodes = tuple(PanePin(pin.target, pin.pane_id, pin.pane_pid, pin.pane_start_ticks) for pin in live_nodes)
            successor_protected = (source_pin, executor_pin, *retained_nodes) if source_live else (executor_pin, *retained_nodes)
            if live_session_id(successor_pin, successor_protected, may_query=codex_status(SUCCESSOR_TARGET) == "ready") != packet["successor_session_id"]:
                raise TaskFrontmatterError("dw:33 successor Codex session changed after packet preparation.")
        elif not prepared_exists or not has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION):
            raise TaskFrontmatterError("committed lifecycle bytes lack the exact prepared audit and guarded close proof.")
        publish_or_validate(prepared_path, prepared_data, "Source-1923 prepared audit")
        close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION)
        pane = exact_pane_id(SOURCE_TARGET)
        if prepared_exists and pane == "" and not close_proven and path_entry_exists(done_live_close_started_path(prepared_path)):
            promote_done_live_close_started(
                proof_path,
                prepared_path,
                str(packet["close_proof_commitment"]),
                prepared_sha,
                SOURCE_TARGET,
                source_pin.pane_id,
                source_pin.pane_pid,
                source_pin.pane_start_ticks,
                CLOSE_OPERATION,
            )
            close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION)
        if before_state and pane == source_pin.pane_id:

            def pre_input_check() -> None:
                for held in held_by_path.values():
                    validate_held_absolute(held)
                if authoritative_active_target_task_paths(root, SOURCE_TARGET) != (root / SOURCE_TASK,):
                    raise TaskFrontmatterError("dw:0 ownership changed before guarded pane input.")
                if set(authoritative_active_target_task_paths(root, SUCCESSOR_TARGET)) != {root / SUCCESSOR_TASK, root / HISTORICAL_TASK}:
                    raise TaskFrontmatterError("dw:33 shared-target ownership changed before guarded pane input.")
                current_records, current_direct_source, current_direct_successor = tree_records(root)
                if (
                    current_records != list(tree)
                    or current_direct_source != packet_string_list(packet, "direct_source_children")
                    or current_direct_successor != packet_string_list(packet, "direct_successor_children")
                ):
                    raise TaskFrontmatterError("active child/descendant tree changed immediately before guarded pane input.")
                validate_live_node_pins(current_records, live_nodes)
                validate_live_node_sessions(live_nodes, protected)
                if any(not current_pin(pin) for pin in protected):
                    raise TaskFrontmatterError("a protected pane changed before guarded pane input.")

            stop_source(
                source_pin,
                str(packet["source_session_id"]),
                proof_path,
                prepared_path,
                str(packet["close_proof_secret"]),
                str(packet["close_proof_commitment"]),
                prepared_sha,
                pre_input_check,
            )
            close_proven = has_bound_close_proof(proof_path, str(packet["close_proof_commitment"]), prepared_sha, CLOSE_OPERATION)
        elif before_state and not (pane == "" and close_proven):
            raise TaskFrontmatterError("stale dw:0 pane identity changed before reviewed closure.")
        if exact_pane_id(SOURCE_TARGET) or not close_proven:
            raise TaskFrontmatterError("stale dw:0 closure lacks exact pane/process absence proof.")

        def validate_and_commit() -> None:
            namespace = task_namespace_snapshot(root)
            final_state_check(root, packet, files, protected, held_parents, held_by_path)
            if task_namespace_snapshot(root) != namespace:
                raise TaskFrontmatterError("task namespace changed across final Source-1923 verification.")
            validate_post_exchange_bindings(held_parents, held_by_path, mutated_paths)
            for entry in files:
                path = root / str(entry["task"])
                held = held_parents[path]
                if sha256(read_at(held, held.leaf_name, f"precommit Source-1923 task {entry['task']}")) != entry["after_sha256"]:
                    raise TaskFrontmatterError(f"precommit Source-1923 bytes changed: {entry['task']}")
            validate_post_exchange_bindings(held_parents, held_by_path, mutated_paths)
            if task_namespace_snapshot(root) != namespace:
                raise TaskFrontmatterError("task namespace changed before Source-1923 commit.")
            try:
                publish_or_validate(audit_path, committed_data, "Source-1923 committed audit")
            except Exception:
                if not path_entry_exists(audit_path) or not existing_exact(audit_path, committed_data, "Source-1923 committed audit"):
                    raise

        if before_state:
            exchange_files(
                root,
                files,
                str(packet["binding_id"]),
                held_by_path,
                validate_and_commit,
            )
        else:
            validate_and_commit()
        cleanup_committed_stages(root, files, str(packet["binding_id"]), held_parents)
        cleanup_committed_close_markers(proof_path, prepared_path, str(packet["close_proof_commitment"]), prepared_sha)
    print(audit_path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = result.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True, type=Path)
    prepare_parser.add_argument("--source-task", required=True, type=Path)
    prepare_parser.add_argument("--successor-task", required=True, type=Path)
    prepare_parser.add_argument("--historical-task", required=True, type=Path)
    prepare_parser.add_argument("--source-target", required=True)
    prepare_parser.add_argument("--successor-target", required=True)
    prepare_parser.add_argument("--parent-target", required=True)
    prepare_parser.add_argument("--executor-target", required=True)
    prepare_parser.add_argument("--source-sha256", required=True)
    prepare_parser.add_argument("--successor-sha256", required=True)
    prepare_parser.add_argument("--historical-sha256", required=True)
    prepare_parser.add_argument("--todo-sha256", required=True)
    prepare_parser.add_argument("--archive-index-sha256", required=True)
    prepare_parser.add_argument("--authority", required=True, type=Path)
    prepare_parser.add_argument("--authority-sha256", required=True)
    prepare_parser.add_argument("--authority-lines", required=True, type=lambda value: tuple(map(int, value.split(":"))))
    prepare_parser.add_argument("--authority-envelope", required=True, type=Path)
    prepare_parser.add_argument("--authority-envelope-sha256", required=True)
    prepare_parser.add_argument("--source-pin", required=True, type=parse_pane_pin)
    prepare_parser.add_argument("--successor-pin", required=True, type=parse_pane_pin)
    prepare_parser.add_argument("--executor-pin", required=True, type=parse_pane_pin)
    prepare_parser.add_argument("--node", required=True, action="append", type=parse_task_pin)
    prepare_parser.add_argument("--live-node", action="append", default=[], type=parse_live_node_pin)
    prepare_parser.add_argument("--packet", required=True, type=Path)
    prepare_parser.add_argument("--audit", required=True, type=Path)
    prepare_parser.add_argument("--preparer", required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--packet", required=True, type=Path)
    execute_parser.add_argument("--packet-sha256", required=True)
    execute_parser.add_argument("--review", required=True, type=Path)
    execute_parser.add_argument("--review-sha256", required=True)
    return result


def main() -> int:
    try:
        ns = parser().parse_args()
        for key, value in vars(ns).items():
            if key.endswith("sha256") and SHA256_RE.fullmatch(str(value)) is None:
                raise TaskFrontmatterError(f"--{key.replace('_', '-')} must be one lowercase SHA-256.")
        if ns.command == "prepare":
            if not ns.preparer.strip():
                raise TaskFrontmatterError("--preparer must be nonempty.")
            prepare(ns)
        else:
            execute(ns)
    except (TaskFrontmatterError, CustodyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"omo_source1923_manager_adopt.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
