#!/usr/bin/env python3
"""Prepare one Source-1290 carrier for a later evidence-bound close packet."""

from __future__ import annotations

import argparse
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_stop import ExitedCodexShell
from omo_manager.omo_codex_stop import SHELL_COMMANDS
from omo_manager.omo_codex_stop import current_command
from omo_manager.omo_codex_stop import terminalize_bound_codex_to_shell
from omo_manager.omo_codex_stop import validate_exited_codex_shell
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_report_receipt import ACCEPTANCE_SCHEMA
from omo_manager.omo_report_receipt import BINDING_SCHEMA
from omo_manager.omo_report_receipt import LEGACY_TRANSACTION_COMMITMENT_SCHEMA
from omo_manager.omo_report_receipt import OwnerPrefixBinding
from omo_manager.omo_report_receipt import Plan
from omo_manager.omo_report_receipt import RECEIPT_PUBLICATION_SCHEMA
from omo_manager.omo_report_receipt import RECEIPT_SCHEMA
from omo_manager.omo_report_receipt import ReceiptError
from omo_manager.omo_report_receipt import TRANSACTION_COMMITMENT_SCHEMA
from omo_manager.omo_report_receipt import TRANSFER_RECEIPT_SCHEMA
from omo_manager.omo_report_receipt import bound_receipt_id
from omo_manager.omo_report_receipt import canonical_json
from omo_manager.omo_report_receipt import extract_report_context
from omo_manager.omo_report_receipt import manager_acknowledgment_key
from omo_manager.omo_report_receipt import path_state
from omo_manager.omo_report_receipt import replay_routing_identity
from omo_manager.omo_report_receipt import safe_label
from omo_manager.omo_report_receipt import safe_part
from omo_manager.omo_report_receipt import validate_receipt_bytes
from omo_manager.omo_task_lock import canonical_target
from omo_manager.omo_task_lock import process_start_ticks
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_file_lock_path
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_lock import watcher_report_manager_temporary
from omo_manager.omo_task_lock import watcher_report_state_maintenance_temporary
from omo_manager.omo_task_lock import watcher_report_state_temporary
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata


CANONICAL_CARRIER = Path("mem1290_auth.md")
DUPLICATE_CARRIER = Path("memory_auth_1290.md")
ARCHIVE_TODO = Path("202608/old_todos.md")
ARCHIVED_MEMORY = Path("202608/memory_research_mgr.md")
ARCHIVED_TRANSCRIPTION = Path("202608/transcription_sw.md")
ARCHIVED_INTERRUPTED_EVAL = Path("202608/mem1290_eval.md")
ARCHIVED_INTERRUPTED_FIX = Path("202608/mem1290_fix.md")
TARGET = "vlcontext_recovery:2"
CANONICAL_CARRIER_BLOCKER = "waiting_for_promoted_done_close_recovery_invocation"
CANONICAL_CARRIER_OPEN_ITEMS = (
    "Establish evidence-bound carrier terminalization with an accepted private report and stabilize/authenticate canonical TODO current-row custody through supported tooling; fail closed if prerequisites remain unavailable; do not execute carrier recovery.",
    "Stabilize and authenticate this canonical carrier task/queue and sole canonical TODO current row; emit exactly one accepted private blocked/terminalization-ready report; generate the bounded ownership manifest bound to stable current state and installed HEAD; stop after preflight evidence or report one supported blocker.",
)
CANONICAL_CARRIER_OPEN_ITEMS_SHA256 = "57e13091c5b8ec0a942fdb81da6611c164057e405d81f8224678f7555f7ee5fa"
CANONICAL_REPORT_STATUS = "blocked"
DUPLICATE_CARRIER_BLOCKER = "duplicate authority carrier created during concurrent routing; canonical carrier is mem1290_auth.md; no production ownership"
SOURCE1290_AUDIT_SHA256 = "eafa5c27d35ea2dacb4c94a0c53619f06acfb66bef703bf63dc569ac7af5fedf"
SOURCE1290_AUTHORITY = "202608/manager_mail/85c5dff58359-1290.txt:3-4"
SOURCE1290_EXCERPT = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
POST_ARCHIVE_SHA256 = {
    CANONICAL_CARRIER: "f3d0e041d72ac26cf421b914e9d154a93d8db6304503338f25995119e8d3fc4a",
    DUPLICATE_CARRIER: "3a0291e6ea4c6aa8ef59055d65e97c53a8468d1a29d6e41c9aad7e760f59c811",
    ARCHIVED_MEMORY: "d2ae03a9e19f981ec43c6b8527fca1475a31a7c0593611c8ac6f36dbb392e705",
    ARCHIVED_TRANSCRIPTION: "a01fec08cfdcab16755a5d44c5ae78fde5110b05967ed7b0c324bba55cc6bea1",
    ARCHIVED_INTERRUPTED_EVAL: "62d641ddcaede3417b5bb024c676d0c3322f8d3bdbac31aa03b9e269259a19cf",
    ARCHIVED_INTERRUPTED_FIX: "ee0429ecf458721f24d4965e285dd59ade51742359b717fd1d697067826d35d5",
}
INTERRUPTED_TARGETS = {
    ARCHIVED_INTERRUPTED_EVAL: "vldr:2",
    ARCHIVED_INTERRUPTED_FIX: "vldr:1",
}
POST_ARCHIVE_FILES = (
    DUPLICATE_CARRIER,
    ARCHIVED_MEMORY,
    ARCHIVED_TRANSCRIPTION,
    ARCHIVED_INTERRUPTED_EVAL,
    ARCHIVED_INTERRUPTED_FIX,
    ARCHIVE_TODO,
)
STATE_SCHEMA = "omo-source1290-lifecycle-prerequisite/v3"
OWNERSHIP_MANIFEST_SCHEMA = "omo-source1290-ownership-manifest/v1"
TODO_SECTIONS = ("current", "human pending", "low priority", "previous")
MAX_INDEXED_TASKS = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PANE_RE = re.compile(r"%[0-9]+\Z")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
TODO_ROW_RE = re.compile(r"([A-Za-z0-9_./-]+\.md)(?:[ \t]+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?|retired)(?:[ \t]+(retired))?)?\Z")
AUTHORITY_RE = re.compile(
    r'<human_instruction[ \t]+authoritative="true"[ \t]+source="([^"\r\n]+)">\r?\n(.*?)</human_instruction>',
    re.DOTALL,
)
SOURCE_FILES = (
    Path("omo_manager/omo_source1290_prerequisite.py"),
    Path("omo_manager/omo_codex_stop.py"),
    Path("omo_manager/omo_codex_status.py"),
    Path("omo_manager/omo_report_receipt.py"),
    Path("omo_manager/omo_task_lock.py"),
    Path("omo_manager/omo_task_metadata.py"),
)


CANONICAL_SOURCE_ROOT = Path("/home/sichangheagent/.config")


class PrerequisiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Args:
    root: Path
    task_sha256: str
    todo_sha256: str
    archive_todo_sha256: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    session_id: str
    report_file: Path
    report_sha256: str
    report_status: str
    acceptance_file: Path
    acceptance_sha256: str
    completed_audit: Path
    completed_audit_sha256: str
    ownership_manifest: Path
    ownership_manifest_sha256: str
    source_head: str
    terminal_receipt: Path
    wait_s: float
    lines: int


@dataclass(frozen=True)
class OwnershipPreflightArgs:
    root: Path
    todo_sha256: str


@dataclass(frozen=True)
class Snapshot:
    path: Path
    payload: bytes
    parent_device: int
    parent_inode: int
    parent_mode: int
    parent_uid: int
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def record(self) -> dict[str, object]:
        return {
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "inode": self.inode,
            "mode": f"{self.mode:04o}",
            "mtime_ns": self.mtime_ns,
            "path": str(self.path),
            "parent": {
                "device": self.parent_device,
                "inode": self.parent_inode,
                "mode": f"{self.parent_mode:04o}",
                "path": str(self.path.parent),
                "uid": self.parent_uid,
            },
            "sha256": self.sha256,
            "size": self.size,
            "uid": self.uid,
        }


@dataclass(frozen=True)
class ReportPaths:
    receipt: Path
    publication: Path
    commitment: Path
    route_evidence: tuple[Path, ...]


@dataclass(frozen=True)
class TodoTask:
    path: Path
    relative: str
    section: str
    target: str


@dataclass(frozen=True)
class IndexedTask:
    todo: TodoTask
    snapshot: Snapshot
    status: str
    target: str


def stable_owned_read(
    path: Path,
    *,
    label: str,
    exact_mode: int | None = None,
    private_parent: bool = False,
) -> Snapshot:
    """Read one unchanged owner-controlled regular file and bind its parent."""

    def parent_identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_mode, value.st_uid

    try:
        parent_before = path.parent.lstat()
    except OSError as exc:
        raise PrerequisiteError(f"cannot inspect {label} parent") from exc
    if not stat.S_ISDIR(parent_before.st_mode) or parent_before.st_uid != os.getuid() or (private_parent and stat.S_IMODE(parent_before.st_mode) != 0o700):
        raise PrerequisiteError(f"{label} parent custody is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PrerequisiteError(f"cannot open {label}") from exc
    try:
        before = os.fstat(fd)
        before_mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or (private_parent and before.st_nlink != 1)
            or (exact_mode is not None and before_mode != exact_mode)
            or (exact_mode is None and before_mode & 0o022)
        ):
            raise PrerequisiteError(f"{label} identity, ownership, mode, or bytes changed")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise PrerequisiteError(f"{label} exceeds the bounded size")
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        named = path.lstat()
        parent_after = path.parent.lstat()
    except OSError as exc:
        raise PrerequisiteError(f"{label} or its parent disappeared while being read") from exc

    def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    mode = stat.S_IMODE(before.st_mode)
    if (
        parent_identity(parent_before) != parent_identity(parent_after)
        or identity(before) != identity(after)
        or identity(before) != identity(named)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or (private_parent and before.st_nlink != 1)
        or (exact_mode is not None and mode != exact_mode)
        or (exact_mode is None and mode & 0o022)
    ):
        raise PrerequisiteError(f"{label} identity, ownership, mode, or bytes changed")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise PrerequisiteError(f"{label} size changed while being read")
    return Snapshot(
        path,
        payload,
        parent_before.st_dev,
        parent_before.st_ino,
        stat.S_IMODE(parent_before.st_mode),
        parent_before.st_uid,
        before.st_dev,
        before.st_ino,
        mode,
        before.st_uid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def json_snapshot(
    path: Path,
    *,
    label: str,
    expected_sha256: str = "",
) -> tuple[dict[str, object], Snapshot]:
    snapshot = stable_owned_read(path, label=label, exact_mode=0o600, private_parent=True)
    if expected_sha256 and snapshot.sha256 != expected_sha256:
        raise PrerequisiteError(f"{label} bytes do not match the supplied digest")
    try:
        value = json.loads(snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != snapshot.payload:
        raise PrerequisiteError(f"{label} is not canonical JSON")
    return value, snapshot


def todo_tasks(root: Path, todo: Snapshot) -> tuple[TodoTask, ...]:
    """Return the complete canonical task index from authenticated TODO bytes."""

    try:
        text = todo.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteError("work-log TODO is not UTF-8") from exc
    section = ""
    tasks: list[TodoTask] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {f"{name}:" for name in TODO_SECTIONS}:
            section = stripped[:-1]
            continue
        if not stripped:
            continue
        if stripped.endswith(":"):
            raise PrerequisiteError("work-log TODO contains an unknown task section")
        if not section:
            continue
        match = TODO_ROW_RE.fullmatch(stripped)
        if match is None:
            raise PrerequisiteError(f"work-log TODO contains a malformed {section} task row")
        raw_path, target, retired_marker = match.groups()
        if retired_marker is not None and (section != "previous" or target == "retired"):
            raise PrerequisiteError(f"work-log TODO contains a malformed {section} task row")
        relative = Path(raw_path)
        if relative.is_absolute() or relative.as_posix() != raw_path or any(part in {"", ".", ".."} for part in relative.parts) or relative == Path("TODO.md"):
            raise PrerequisiteError("work-log TODO contains an unsafe task path")
        path = root / relative
        if path.resolve(strict=False) != path:
            raise PrerequisiteError(f"indexed task path is aliased or escapes the work-log root: {raw_path}")
        if raw_path in seen:
            raise PrerequisiteError(f"work-log TODO contains duplicate task membership: {raw_path}")
        seen.add(raw_path)
        tasks.append(TodoTask(path, raw_path, section, target or ""))
        if len(tasks) > MAX_INDEXED_TASKS:
            raise PrerequisiteError("work-log TODO task index exceeds the bounded manifest size")
    if not tasks:
        raise PrerequisiteError("work-log TODO has no authoritative task index")
    return tuple(sorted(tasks, key=lambda task: task.relative))


def indexed_task(todo_task: TodoTask, root: Path) -> IndexedTask:
    snapshot = stable_owned_read(todo_task.path, label=f"indexed task {todo_task.relative}")
    try:
        text = snapshot.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteError(f"indexed task is not UTF-8: {todo_task.relative}") from exc
    try:
        metadata = parse_task_metadata(text, root)
    except TaskFrontmatterError as exc:
        raise PrerequisiteError(f"indexed task has invalid frontmatter: {todo_task.relative}") from exc
    if metadata is None:
        raise PrerequisiteError(f"indexed TODO member is not a task: {todo_task.relative}")
    if todo_task.target and canonical_target(todo_task.target) != canonical_target(metadata.runat):
        raise PrerequisiteError(f"indexed task target differs between TODO and frontmatter: {todo_task.relative}")
    return IndexedTask(todo_task, snapshot, metadata.status, metadata.runat)


def ownership_manifest_value(root: Path, todo: Snapshot) -> tuple[dict[str, object], tuple[IndexedTask, ...]]:
    indexed = tuple(indexed_task(task, root) for task in todo_tasks(root, todo))
    identities: set[tuple[int, int]] = set()
    for item in indexed:
        identity = (item.snapshot.device, item.snapshot.inode)
        if identity in identities:
            raise PrerequisiteError("indexed task membership contains filesystem aliases")
        identities.add(identity)
    tasks = [
        {
            "path": item.todo.relative,
            "section": item.todo.section,
            "snapshot": item.snapshot.record(),
            "status": item.status,
            "target": item.target,
            "todo_target": item.todo.target,
        }
        for item in indexed
    ]
    return {
        "root": str(root),
        "schema": OWNERSHIP_MANIFEST_SCHEMA,
        "tasks": tasks,
        "todo": todo.record(),
    }, indexed


def ownership_preflight(root: Path, todo_sha256: str) -> bytes:
    """Build one canonical bounded ownership manifest without writing files."""

    todo = stable_owned_read(root / "TODO.md", label="work-log TODO")
    if todo.sha256 != todo_sha256:
        raise PrerequisiteError("work-log TODO bytes do not match the ownership preflight digest")
    value, _ = ownership_manifest_value(root, todo)
    return canonical_json(value)


def manifest_paths(args: Args) -> tuple[Path, ...]:
    value, _ = json_snapshot(args.ownership_manifest, label="ownership manifest", expected_sha256=args.ownership_manifest_sha256)
    if set(value) != {"root", "schema", "tasks", "todo"} or value.get("schema") != OWNERSHIP_MANIFEST_SCHEMA or value.get("root") != str(args.root):
        raise PrerequisiteError("ownership manifest identity is invalid")
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > MAX_INDEXED_TASKS:
        raise PrerequisiteError("ownership manifest task set is invalid")
    paths: list[Path] = []
    names: list[str] = []
    for item in raw_tasks:
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw_path, str):
            raise PrerequisiteError("ownership manifest task path is malformed")
        relative = Path(raw_path)
        path = args.root / relative
        if relative.is_absolute() or relative.as_posix() != raw_path or any(part in {"", ".", ".."} for part in relative.parts) or path.resolve(strict=False) != path:
            raise PrerequisiteError("ownership manifest task path is unsafe")
        names.append(raw_path)
        paths.append(path)
    if names != sorted(names) or len(names) != len(set(names)):
        raise PrerequisiteError("ownership manifest task set is omitted, duplicated, or unordered")
    return tuple(paths)


def validate_ownership_manifest(args: Args, todo: Snapshot) -> tuple[dict[str, object], tuple[IndexedTask, ...]]:
    supplied, snapshot = json_snapshot(args.ownership_manifest, label="ownership manifest", expected_sha256=args.ownership_manifest_sha256)
    expected, indexed = ownership_manifest_value(args.root, todo)
    if supplied != expected:
        raise PrerequisiteError("ownership manifest drifted from the authoritative TODO task index")
    return {"artifact": snapshot.record(), "index": expected}, indexed


def todo_rows(text: str, name: str) -> tuple[tuple[str, str], ...]:
    section = ""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        elif stripped.split()[:1] == [name]:
            rows.append((section, stripped))
    return tuple(rows)


def has_pending_marker(text: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and stripped == "(pending)":
            return True
    return False


def open_items_record(items: tuple[str, ...]) -> dict[str, object]:
    values = list(items)
    return {"items": values, "sha256": hashlib.sha256(canonical_json(values).rstrip(b"\n")).hexdigest()}


def validate_task_and_todo(args: Args, task: Snapshot, todo: Snapshot) -> dict[str, object]:
    if task.sha256 != args.task_sha256 or task.sha256 != POST_ARCHIVE_SHA256[CANONICAL_CARRIER] or todo.sha256 != args.todo_sha256:
        raise PrerequisiteError("carrier or TODO bytes do not match the supplied digests")
    try:
        task_text = task.payload.decode("utf-8")
        todo_text = todo.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteError("carrier or TODO is not UTF-8") from exc
    metadata = parse_task_metadata(task_text, args.root)
    authority = tuple((source, body.replace("\r\n", "\n")) for source, body in AUTHORITY_RE.findall(task_text))
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != CANONICAL_CARRIER_BLOCKER
        or metadata.runat != TARGET
        or metadata.tool != "codex"
        or metadata.is_manager
        or metadata.pending_task_items != CANONICAL_CARRIER_OPEN_ITEMS
        or open_items_record(metadata.pending_task_items)["sha256"] != CANONICAL_CARRIER_OPEN_ITEMS_SHA256
        or has_pending_marker(task_text)
        or authority != ((SOURCE1290_AUTHORITY, SOURCE1290_EXCERPT),)
    ):
        raise PrerequisiteError("canonical carrier is not the exact blocked/open-item Source-1290 prerequisite")
    if (
        todo_rows(todo_text, CANONICAL_CARRIER.as_posix()) != (("human pending", f"{CANONICAL_CARRIER} {TARGET}"),)
        or todo_rows(todo_text, DUPLICATE_CARRIER.as_posix()) != (("human pending", "memory_auth_1290.md agent_managers:78"),)
        or todo_rows(todo_text, ARCHIVED_INTERRUPTED_EVAL.as_posix()) != (("previous", "202608/mem1290_eval.md vldr:2"),)
        or todo_rows(todo_text, ARCHIVED_INTERRUPTED_FIX.as_posix()) != (("previous", "202608/mem1290_fix.md vldr:1"),)
        or todo_rows(todo_text, "memory_research_mgr.md")
        or todo_rows(todo_text, ARCHIVED_MEMORY.as_posix())
        or todo_rows(todo_text, "transcription_sw.md")
        or todo_rows(todo_text, ARCHIVED_TRANSCRIPTION.as_posix())
    ):
        raise PrerequisiteError("Source-1290 TODO placement is not canonical human-pending custody")
    return {
        "blocked_on": metadata.blocked_on,
        "pending_task_items": open_items_record(metadata.pending_task_items),
        "status": metadata.status,
        "todo_section": "human pending",
        "transition": "none",
    }


def validate_post_archive_state(
    args: Args,
) -> dict[str, object]:
    snapshots = {relative: stable_owned_read(args.root / relative, label=f"Source-1290 post-archive record {relative}") for relative in POST_ARCHIVE_FILES}
    archive_todo = snapshots[ARCHIVE_TODO]
    if archive_todo.sha256 != args.archive_todo_sha256:
        raise PrerequisiteError("archived TODO bytes do not match the supplied digest")
    if any(snapshots[relative].sha256 != expected for relative, expected in POST_ARCHIVE_SHA256.items() if relative != CANONICAL_CARRIER):
        raise PrerequisiteError("Source-1290 post-archive record bytes drifted")
    for original in (Path("memory_research_mgr.md"), Path("transcription_sw.md")):
        if os.path.lexists(args.root / original):
            raise PrerequisiteError("Source-1290 pre-archive task unexpectedly exists")
    try:
        texts = {relative: snapshot.payload.decode("utf-8") for relative, snapshot in snapshots.items()}
    except UnicodeDecodeError as exc:
        raise PrerequisiteError("Source-1290 post-archive record is not UTF-8") from exc
    memory = parse_task_metadata(texts[ARCHIVED_MEMORY], args.root)
    transcription = parse_task_metadata(texts[ARCHIVED_TRANSCRIPTION], args.root)
    duplicate = parse_task_metadata(texts[DUPLICATE_CARRIER], args.root)
    duplicate_authority = tuple((source, body.replace("\r\n", "\n")) for source, body in AUTHORITY_RE.findall(texts[DUPLICATE_CARRIER]))
    if (
        memory is None
        or memory.status != "done"
        or not memory.is_manager
        or memory.runat != "wl:32"
        or memory.pending_task_items
        or has_pending_marker(texts[ARCHIVED_MEMORY])
        or transcription is None
        or transcription.status != "done"
        or transcription.is_manager
        or transcription.runat != "wl:32"
        or transcription.pending_task_items
        or has_pending_marker(texts[ARCHIVED_TRANSCRIPTION])
        or duplicate is None
        or duplicate.status != "blocked"
        or duplicate.blocked_on != DUPLICATE_CARRIER_BLOCKER
        or duplicate.is_manager
        or duplicate.runat != "agent_managers:78"
        or duplicate.pending_task_items
        or has_pending_marker(texts[DUPLICATE_CARRIER])
        or duplicate_authority != ((SOURCE1290_AUTHORITY, SOURCE1290_EXCERPT),)
    ):
        raise PrerequisiteError("Source-1290 post-archive memory, transcription, or duplicate custody drifted")
    for relative, target in INTERRUPTED_TARGETS.items():
        metadata = parse_task_metadata(texts[relative], args.root)
        if (
            metadata is None
            or metadata.status != "done"
            or metadata.blocked_on
            or metadata.is_manager
            or metadata.runat != target
            or metadata.pending_task_items
            or has_pending_marker(texts[relative])
        ):
            raise PrerequisiteError("Source-1290 archived interrupted-task custody drifted")
    archive_section = "archived from todo.md previous on 2026-09-01"
    if todo_rows(texts[ARCHIVE_TODO], "memory_research_mgr.md") != ((archive_section, "memory_research_mgr.md wl:32"),) or todo_rows(texts[ARCHIVE_TODO], "transcription_sw.md") != (
        (archive_section, "transcription_sw.md wl:32"),
    ):
        raise PrerequisiteError("Source-1290 archived TODO custody drifted")
    return {relative.as_posix(): snapshots[relative].record() for relative in POST_ARCHIVE_FILES}


def active_target_owners(
    indexed: tuple[IndexedTask, ...],
    target: str = TARGET,
) -> tuple[str, ...]:
    canonical = canonical_target(target)
    return tuple(item.todo.relative for item in indexed if item.status != "done" and canonical_target(item.target) == canonical)


def git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(CANONICAL_SOURCE_ROOT), "rev-parse", "--verify", "HEAD"],
        encoding="ascii",
        capture_output=True,
        timeout=10,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    value = result.stdout.strip()
    if result.returncode != 0 or COMMIT_RE.fullmatch(value) is None:
        raise PrerequisiteError("cannot authenticate the source repository HEAD")
    return value


def require_source_head(source_head: str) -> None:
    if COMMIT_RE.fullmatch(source_head) is None:
        raise PrerequisiteError("--source-head is not one full lowercase Git SHA")
    if git_head() != source_head:
        raise PrerequisiteError("canonical source repository HEAD differs from --source-head")


def source_binding(args: Args) -> dict[str, object]:
    expected_helper = (CANONICAL_SOURCE_ROOT / SOURCE_FILES[0]).resolve(strict=False)
    if Path(__file__).resolve() != expected_helper:
        raise PrerequisiteError("executed prerequisite helper is outside the bound source repository")
    require_source_head(args.source_head)
    files = [stable_owned_read((CANONICAL_SOURCE_ROOT / relative).resolve(), label=f"source file {relative}").record() for relative in SOURCE_FILES]
    require_source_head(args.source_head)
    return {"files": files, "head": args.source_head, "root": str(CANONICAL_SOURCE_ROOT)}


def route_state(path: Path) -> dict[str, object]:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "path": str(path)}
    snapshot = stable_owned_read(path, label="report route evidence")
    try:
        _ = snapshot.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteError("report route evidence is not UTF-8") from exc
    return {"exists": True, "path": str(path), "sha256": snapshot.sha256, "size_bytes": snapshot.size}


def validate_bound_id(value: dict[str, object], field: str, label: str) -> None:
    unsigned = dict(value)
    identifier = unsigned.pop(field, None)
    if not isinstance(identifier, str) or identifier != bound_receipt_id(unsigned):
        raise PrerequisiteError(f"{label} content binding is invalid")


def report_paths(args: Args) -> ReportPaths:
    acceptance, _ = json_snapshot(args.acceptance_file, label="report acceptance", expected_sha256=args.acceptance_sha256)
    if acceptance.get("schema") != ACCEPTANCE_SCHEMA or acceptance.get("accepted") is not True or acceptance.get("manager_acknowledged") is not True:
        raise PrerequisiteError("report acceptance is not accepted:true with manager acknowledgment")
    raw_receipt = acceptance.get("receipt_path")
    raw_publication = acceptance.get("publication_path")
    if not isinstance(raw_receipt, str) or not isinstance(raw_publication, str):
        raise PrerequisiteError("accepted report is missing its durable receipt paths")
    receipt = Path(raw_receipt)
    publication = Path(raw_publication)
    if (
        not receipt.is_absolute()
        or not publication.is_absolute()
        or receipt.resolve(strict=False) != receipt
        or publication.resolve(strict=False) != publication
        or receipt.parent != publication.parent
        or receipt.parent.name != "report-receipts"
        or receipt.parent.parent.name != "omo-manager"
    ):
        raise PrerequisiteError("accepted report receipt paths are unsafe")
    receipt_value, _ = json_snapshot(receipt, label="durable report receipt")
    replay_id = receipt_value.get("replay_id")
    routing = receipt_value.get("routing")
    evidence = routing.get("route_evidence") if isinstance(routing, dict) else None
    if not isinstance(replay_id, str) or HASH_RE.fullmatch(replay_id) is None or not isinstance(evidence, list):
        raise PrerequisiteError("durable report receipt cannot identify its transaction")
    route_paths: list[Path] = []
    for item in evidence:
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute() or Path(raw_path).resolve(strict=False) != Path(raw_path):
            raise PrerequisiteError("durable report route evidence contains an unsafe path")
        route_paths.append(Path(raw_path))
    return ReportPaths(receipt, publication, receipt.parent / f"{replay_id}.commitment", tuple(route_paths))


def validate_report_helper(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict) or value.get("execution") != "immutable-pipe-and-memory-compiled-sources":
        raise PrerequisiteError("durable report helper identity is invalid")
    identities: list[tuple[str, str]] = []
    for path_key, digest_key in (("path", "sha256"), ("receiver_path", "receiver_sha256")):
        raw_path = value.get(path_key)
        digest = value.get(digest_key)
        if not isinstance(raw_path, str) or not isinstance(digest, str) or not Path(raw_path).is_absolute() or HASH_RE.fullmatch(digest) is None:
            raise PrerequisiteError("durable report helper identity is malformed")
        identities.append((raw_path, digest))
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise PrerequisiteError("durable report helper dependencies are missing")
    for dependency in dependencies.values():
        raw_path = dependency.get("path") if isinstance(dependency, dict) else None
        digest = dependency.get("sha256") if isinstance(dependency, dict) else None
        if not isinstance(raw_path, str) or not isinstance(digest, str) or not Path(raw_path).is_absolute() or HASH_RE.fullmatch(digest) is None:
            raise PrerequisiteError("durable report helper dependency is malformed")
        identities.append((raw_path, digest))
    records: list[dict[str, object]] = []
    for raw_path, expected in identities:
        snapshot = stable_owned_read(Path(raw_path), label="durable report helper source")
        if snapshot.sha256 != expected:
            raise PrerequisiteError("durable report helper source drifted")
        records.append(snapshot.record())
    return records


def canonical_report_plan(
    args: Args,
    paths: ReportPaths,
    report: Snapshot,
    receipt: dict[str, object],
) -> Plan:
    """Reconstruct the canonical report plan needed by its own validator."""

    helper = receipt.get("helper")
    routing = receipt.get("routing")
    effects = receipt.get("side_effects")
    input_info = receipt.get("input")
    report_context = receipt.get("report_context")
    replay_id = receipt.get("replay_id")
    effect_names = {
        "durable_receipt",
        "locks",
        "manager_acknowledgment",
        "manager_file",
        "private_allocation",
        "private_envelope",
        "receipt_publication",
    }
    routing_names = {
        "agent",
        "manager",
        "producer_target",
        "requested_manager_target",
        "resolved_manager_target",
        "root",
        "route_kind",
        "route_evidence",
        "route_evidence_sha256",
        "route_local_date",
        "route_note",
        "task",
        "tmux",
    }
    if (
        not isinstance(helper, dict)
        or not isinstance(routing, dict)
        or set(routing) != routing_names
        or not isinstance(effects, dict)
        or set(effects) != effect_names
        or not all(isinstance(effects[name], (dict, list)) for name in effect_names)
        or not isinstance(input_info, dict)
        or not isinstance(report_context, dict)
        or not isinstance(replay_id, str)
    ):
        raise PrerequisiteError("durable report receipt does not satisfy the canonical side-effect schema")
    try:
        report_text = report.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrerequisiteError("private report is not UTF-8") from exc
    if report_context != extract_report_context(report_text):
        raise PrerequisiteError("durable report context does not match the held private report")

    def absolute_recorded_path(value: object, label: str) -> Path:
        if not isinstance(value, str):
            raise PrerequisiteError(f"canonical report {label} path is missing")
        path = Path(value)
        if not path.is_absolute() or path.resolve(strict=False) != path:
            raise PrerequisiteError(f"canonical report {label} path is unsafe")
        return path

    task = absolute_recorded_path(routing.get("task"), "task")
    manager = absolute_recorded_path(routing.get("manager"), "manager")
    if task != args.root / CANONICAL_CARRIER or not manager.is_relative_to(args.root):
        raise PrerequisiteError("canonical report task or manager route is outside Source-1290 custody")
    agent = routing.get("agent")
    route_note = routing.get("route_note")
    evidence = routing.get("route_evidence")
    if (
        not isinstance(agent, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", agent) is None
        or agent in {".", ".."}
        or not isinstance(route_note, str)
        or not isinstance(evidence, list)
        or not all(isinstance(item, dict) for item in evidence)
    ):
        raise PrerequisiteError("canonical report routing identity is malformed")
    route_evidence = tuple(dict(item) for item in evidence)
    route_targets = tuple(sorted({Path(str(item["path"])) for item in route_evidence} | {manager}, key=str))
    route_locks = tuple((target, task_file_lock_path(target)) for target in route_targets)
    task_lock = next((lock for target, lock in route_locks if target == manager), None)
    if task_lock is None:
        raise PrerequisiteError("canonical report manager route has no task lock")

    manager_effect = effects["manager_file"]
    if not isinstance(manager_effect, dict):
        raise PrerequisiteError("canonical report manager effect is malformed")
    owner_record = manager_effect.get("owner_prefix")
    if not isinstance(owner_record, dict) or set(owner_record) != {
        "manager_path_sha256",
        "separator_bytes",
        "sha256",
        "size_bytes",
    }:
        raise PrerequisiteError("canonical report owner-prefix binding is malformed")
    manager_path_sha256 = owner_record.get("manager_path_sha256")
    owner_sha256 = owner_record.get("sha256")
    owner_size = owner_record.get("size_bytes")
    separator_bytes = owner_record.get("separator_bytes")
    if (
        not isinstance(manager_path_sha256, str)
        or manager_path_sha256 != hashlib.sha256(str(manager).encode()).hexdigest()
        or not isinstance(owner_sha256, str)
        or HASH_RE.fullmatch(owner_sha256) is None
        or not isinstance(owner_size, int)
        or isinstance(owner_size, bool)
        or owner_size < 0
        or separator_bytes not in {1, 2}
    ):
        raise PrerequisiteError("canonical report owner-prefix binding is invalid")
    owner_prefix = OwnerPrefixBinding(manager_path_sha256, owner_sha256, owner_size, int(separator_bytes))

    report_key_parts = [
        report.payload,
        agent.encode(),
        args.report_status.encode(),
        safe_label(TARGET).encode(),
        str(task).encode(),
    ]
    if route_note:
        report_key_parts.append(route_note.encode())
    report_key = hashlib.sha256(b"\0".join(report_key_parts)).hexdigest()
    envelope_directory = Path("/tmp") / f"omo-agent-messages-{os.getuid()}"
    envelope_final = envelope_directory / f"{safe_part(agent)}_{safe_part(args.report_status)}_{report_key}.md"
    pointer = f"(from agent {TARGET} {envelope_final})"
    acknowledgment_state = paths.receipt.parent.parent / "pending-watch-consumed-reports.tsv"
    acknowledgment_lock = acknowledgment_state.with_name(f".{acknowledgment_state.name}.lock")
    acknowledgment_key = manager_acknowledgment_key(args.root, envelope_final, report.sha256)
    acknowledgment_authority_lock = acknowledgment_state.parent / "pending-watch-authority" / f"{hashlib.sha256(acknowledgment_key.encode()).hexdigest()}.lock"
    binding = {
        "helper": helper,
        "input": input_info,
        "owner_prefix": owner_record,
        "report_context": report_context,
        "routing": replay_routing_identity(routing),
        "schema": BINDING_SCHEMA,
        "status": args.report_status,
    }
    expected_replay_id = hashlib.sha256(canonical_json(binding).rstrip(b"\n")).hexdigest()
    if replay_id != expected_replay_id:
        raise PrerequisiteError("canonical report replay identity is invalid")
    helper_path = absolute_recorded_path(helper.get("path"), "helper")
    receiver_path = absolute_recorded_path(helper.get("receiver_path"), "receiver")
    return Plan(
        mode="submit",
        root=args.root,
        task=task,
        manager=manager,
        helper_path=helper_path,
        receiver_path=receiver_path,
        message_path=args.report_file,
        message_identity=(report.device, report.inode),
        message_fd=-1,
        message=report.payload,
        status=args.report_status,
        input_info=input_info,
        report_context=report_context,
        routing=routing,
        route_evidence=route_evidence,
        manager_route_selection="sole-active",
        manager_frontmatter_sha256="0" * 64,
        description_manager_snapshot=None,
        route_locks=route_locks,
        helper=helper,
        replay_id=replay_id,
        owner_prefix=owner_prefix,
        report_lock=Path(f"{manager}.omo_report.lock"),
        task_lock=task_lock,
        manager_temporary=manager.parent / f".{manager.name}.omo-report-{replay_id}.tmp",
        manager_watcher_temporary=watcher_report_manager_temporary(manager, acknowledgment_key),
        envelope_directory=envelope_directory,
        envelope_temporary=envelope_directory / f".{envelope_final.name}.{replay_id}.tmp",
        envelope_final=envelope_final,
        pointer=pointer,
        acknowledgment_state=acknowledgment_state,
        acknowledgment_lock=acknowledgment_lock,
        acknowledgment_temporary=watcher_report_state_temporary(acknowledgment_state, acknowledgment_key),
        acknowledgment_maintenance_temporary=watcher_report_state_maintenance_temporary(acknowledgment_state),
        acknowledgment_key=acknowledgment_key,
        acknowledgment_authority_lock=acknowledgment_authority_lock,
        acknowledgment_authority_completion=acknowledgment_authority_lock.with_name(f"{acknowledgment_authority_lock.name}.complete"),
        receipt_directory=paths.receipt.parent,
        transaction_commitment_temporary=paths.receipt.parent / f".{replay_id}.commitment.tmp",
        transaction_commitment_final=paths.commitment,
        receipt_temporary=paths.receipt.parent / f".{replay_id}.tmp",
        receipt_final=paths.receipt,
        receipt_publication_temporary=paths.receipt.parent / f".{replay_id}.publication.tmp",
        receipt_publication_final=paths.publication,
    )


def validate_canonical_receipt(
    args: Args,
    paths: ReportPaths,
    report: Snapshot,
    receipt: dict[str, object],
    receipt_snapshot: Snapshot,
) -> None:
    plan = canonical_report_plan(args, paths, report, receipt)
    try:
        validated = validate_receipt_bytes(
            plan,
            receipt_snapshot.payload,
            require_current_route_evidence=True,
        )
    except ReceiptError as exc:
        raise PrerequisiteError(f"canonical durable report validation failed: {exc}") from exc
    if validated != receipt:
        raise PrerequisiteError("canonical durable report validation returned different receipt bytes")


def validate_report(args: Args, paths: ReportPaths, task: Snapshot, todo: Snapshot) -> dict[str, object]:
    if args.report_status != CANONICAL_REPORT_STATUS:
        raise PrerequisiteError("Source-1290 terminalization report status is not blocked")
    report = stable_owned_read(args.report_file, label="private report", exact_mode=0o600, private_parent=True)
    if report.sha256 != args.report_sha256:
        raise PrerequisiteError("private report bytes drifted")
    acceptance, acceptance_snapshot = json_snapshot(args.acceptance_file, label="report acceptance", expected_sha256=args.acceptance_sha256)
    receipt, receipt_snapshot = json_snapshot(paths.receipt, label="durable report receipt")
    publication, publication_snapshot = json_snapshot(paths.publication, label="report receipt publication")
    commitment, commitment_snapshot = json_snapshot(paths.commitment, label="report transaction commitment")
    acceptance_keys = {
        "accepted",
        "accepted_at_utc",
        "input",
        "manager_acknowledged",
        "publication_id",
        "publication_path",
        "publication_state",
        "receipt_id",
        "receipt_path",
        "receipt_state",
        "reason",
        "replay_id",
        "retry_required",
        "routing",
        "schema",
        "status",
        "transfer_receipt",
    }
    if (
        set(acceptance) != acceptance_keys
        or acceptance.get("schema") != ACCEPTANCE_SCHEMA
        or acceptance.get("accepted") is not True
        or acceptance.get("manager_acknowledged") is not True
        or acceptance.get("reason") != "manager acknowledged routed report"
        or acceptance.get("retry_required") is not False
        or acceptance.get("status") != args.report_status
    ):
        raise PrerequisiteError("report acceptance is not one exact accepted private report")
    receipt_keys = {
        "accepted",
        "accepted_at_utc",
        "helper",
        "input",
        "preflight",
        "receipt_id",
        "receipt_record",
        "replay_id",
        "report_context",
        "routing",
        "schema",
        "side_effects",
        "status",
    }
    validate_bound_id(receipt, "receipt_id", "durable report receipt")
    input_record = {"sha256": report.sha256, "size_bytes": report.size}
    replay_id = receipt.get("replay_id")
    if (
        set(receipt) != receipt_keys
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("accepted") is not True
        or receipt.get("status") != args.report_status
        or receipt.get("input") != input_record
        or not isinstance(replay_id, str)
        or HASH_RE.fullmatch(replay_id) is None
        or paths.receipt.name != f"{replay_id}.json"
        or paths.publication.name != f"{replay_id}.publication.json"
        or paths.commitment.name != f"{replay_id}.commitment"
    ):
        raise PrerequisiteError("durable report receipt identity is invalid")
    receipt_record = receipt.get("receipt_record")
    expected_receipt_record = {
        "application_directory": str(paths.receipt.parent.parent),
        "commit": "write-fsync-rename-fsync-directory",
        "directory": str(paths.receipt.parent),
        "directory_mode": "0700",
        "file_mode": "0600",
        "final": str(paths.receipt),
        "publication_final": str(paths.publication),
        "publication_temporary": str(paths.receipt.parent / f".{replay_id}.publication.tmp"),
        "state_home": str(paths.receipt.parent.parent.parent),
        "temporary": str(paths.receipt.parent / f".{replay_id}.tmp"),
    }
    if receipt_record != expected_receipt_record:
        raise PrerequisiteError("durable report receipt location is not canonical")
    routing = receipt.get("routing")
    tmux = routing.get("tmux") if isinstance(routing, dict) else None
    evidence = routing.get("route_evidence") if isinstance(routing, dict) else None
    expected_task_state = {"exists": True, "path": str(task.path), "sha256": task.sha256, "size_bytes": task.size}
    expected_todo_state = {"exists": True, "path": str(todo.path), "sha256": todo.sha256, "size_bytes": todo.size}
    if (
        not isinstance(routing, dict)
        or routing.get("root") != str(args.root)
        or routing.get("task") != str(task.path)
        or routing.get("producer_target") != TARGET
        or not isinstance(tmux, dict)
        or tmux.get("pane_id") != args.pane_id
        or not isinstance(evidence, list)
        or expected_task_state not in evidence
        or expected_todo_state not in evidence
        or routing.get("route_evidence_sha256") != hashlib.sha256(canonical_json(evidence).rstrip(b"\n")).hexdigest()
    ):
        raise PrerequisiteError("durable report routing does not bind the canonical carrier, TODO, and pane")
    observed_paths: set[Path] = set()
    for item in evidence:
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw_path, str):
            raise PrerequisiteError("durable report route evidence is malformed")
        path = Path(raw_path)
        if path in observed_paths or route_state(path) != item:
            raise PrerequisiteError("durable report route evidence drifted")
        observed_paths.add(path)
    validate_canonical_receipt(args, paths, report, receipt, receipt_snapshot)
    preflight = receipt.get("preflight")
    if not isinstance(preflight, dict):
        raise PrerequisiteError("durable report preflight is missing")
    preflight_unsigned = dict(preflight)
    preflight_sha256 = preflight_unsigned.pop("sha256", None)
    allocation = preflight.get("allocation")
    if (
        not isinstance(preflight_sha256, str)
        or preflight_sha256 != hashlib.sha256(canonical_json(preflight_unsigned).rstrip(b"\n")).hexdigest()
        or preflight.get("routing_sources") != evidence
        or not isinstance(allocation, dict)
        or allocation.get("file") != str(args.report_file)
        or allocation.get("file_sha256") != report.sha256
        or allocation.get("file_size_bytes") != report.size
    ):
        raise PrerequisiteError("durable report preflight binding is invalid")
    commitment_schema = commitment.get("schema")
    commitment_keys = {"allocation", "commitment", "commitment_id", "preflight", "replay_id", "schema"}
    if commitment_schema == TRANSACTION_COMMITMENT_SCHEMA:
        commitment_keys.add("transfer")
    validate_bound_id(commitment, "commitment_id", "report transaction commitment")
    committed_allocation = commitment.get("allocation")
    file_at_submission = committed_allocation.get("file_at_submission") if isinstance(committed_allocation, dict) else None
    if (
        commitment_schema not in {LEGACY_TRANSACTION_COMMITMENT_SCHEMA, TRANSACTION_COMMITMENT_SCHEMA}
        or set(commitment) != commitment_keys
        or commitment.get("replay_id") != replay_id
        or commitment.get("preflight") != preflight
        or not isinstance(file_at_submission, dict)
        or file_at_submission.get("dev") != report.device
        or file_at_submission.get("inode") != report.inode
        or file_at_submission.get("size") != report.size
    ):
        raise PrerequisiteError("report transaction commitment does not bind the held private report")
    publication_keys = {"publication_id", "receipt_id", "receipt_path", "receipt_state", "replay_id", "schema"}
    validate_bound_id(publication, "publication_id", "report receipt publication")
    if (
        set(publication) != publication_keys
        or publication.get("schema") != RECEIPT_PUBLICATION_SCHEMA
        or publication.get("receipt_id") != receipt.get("receipt_id")
        or publication.get("receipt_path") != str(paths.receipt)
        or publication.get("replay_id") != replay_id
        or publication.get("receipt_state") != path_state(paths.receipt)
    ):
        raise PrerequisiteError("report receipt publication is invalid")
    transfer = acceptance.get("transfer_receipt")
    if not isinstance(transfer, dict):
        raise PrerequisiteError("accepted report transfer receipt is missing")
    validate_bound_id(transfer, "transfer_id", "accepted report transfer receipt")
    expected_transfer_schema = TRANSFER_RECEIPT_SCHEMA if commitment_schema == TRANSACTION_COMMITMENT_SCHEMA else "omo-report-transfer-receipt/legacy-v1"
    committed_transfer = commitment.get("transfer")
    expected_transfer = {key: value for key, value in transfer.items() if key not in {"commitment_id", "transfer_id"}}
    if (
        transfer.get("schema") != expected_transfer_schema
        or transfer.get("commitment_id") != commitment.get("commitment_id")
        or transfer.get("routing") != acceptance.get("routing")
        or (commitment_schema == TRANSACTION_COMMITMENT_SCHEMA and committed_transfer != expected_transfer)
    ):
        raise PrerequisiteError("accepted report transfer receipt is invalid")
    if (
        acceptance.get("accepted_at_utc") != receipt.get("accepted_at_utc")
        or acceptance.get("input") != input_record
        or acceptance.get("receipt_id") != receipt.get("receipt_id")
        or acceptance.get("receipt_path") != str(paths.receipt)
        or acceptance.get("receipt_state") != publication.get("receipt_state")
        or acceptance.get("publication_id") != publication.get("publication_id")
        or acceptance.get("publication_path") != str(paths.publication)
        or acceptance.get("publication_state") != path_state(paths.publication)
        or acceptance.get("replay_id") != replay_id
    ):
        raise PrerequisiteError("accepted report output drifted from its durable receipt")
    effects = receipt.get("side_effects")
    acknowledgment = effects.get("manager_acknowledgment") if isinstance(effects, dict) else None
    if not isinstance(acknowledgment, dict) or acknowledgment.get("schema") != "omo-pending-watch-consumed-report/v1":
        raise PrerequisiteError("durable report lacks manager acknowledgment evidence")
    helper_records = validate_report_helper(receipt.get("helper"))
    return {
        "acceptance": acceptance_snapshot.record(),
        "commitment": commitment_snapshot.record(),
        "helper_files": helper_records,
        "publication": publication_snapshot.record(),
        "receipt": receipt_snapshot.record(),
        "receipt_id": receipt["receipt_id"],
        "replay_id": replay_id,
        "report": report.record(),
        "route_evidence_sha256": routing["route_evidence_sha256"],
        "status": args.report_status,
    }


def membership_record(indexed: tuple[IndexedTask, ...]) -> dict[str, object]:
    names = [item.todo.relative for item in indexed]
    return {"paths": names, "sha256": hashlib.sha256(canonical_json(names).rstrip(b"\n")).hexdigest()}


def build_binding(args: Args, paths: ReportPaths) -> dict[str, object]:
    task = stable_owned_read(args.root / CANONICAL_CARRIER, label="canonical Source-1290 carrier")
    todo = stable_owned_read(args.root / "TODO.md", label="work-log TODO")
    carrier_lifecycle = validate_task_and_todo(args, task, todo)
    post_archive = validate_post_archive_state(args)
    ownership_manifest, indexed = validate_ownership_manifest(args, todo)
    owners = active_target_owners(indexed)
    if owners != (CANONICAL_CARRIER.as_posix(),):
        raise PrerequisiteError(f"canonical Source-1290 carrier is not the sole target owner: {owners or ('none',)}")
    interrupted_owners = {target: list(active_target_owners(indexed, target)) for target in INTERRUPTED_TARGETS.values()}
    if any(interrupted_owners.values()):
        raise PrerequisiteError("Source-1290 archived helper targets regained active ownership")
    audit = stable_owned_read(args.completed_audit, label="completed Source-1290 audit", exact_mode=0o600, private_parent=True)
    if args.completed_audit_sha256 != SOURCE1290_AUDIT_SHA256 or audit.sha256 != args.completed_audit_sha256:
        raise PrerequisiteError("completed Source-1290 audit drifted")
    return {
        "audit": audit.record(),
        "carrier": task.record(),
        "carrier_lifecycle": carrier_lifecycle,
        "membership": membership_record(indexed),
        "ownership_manifest": ownership_manifest,
        "ownership": {"owners": list(owners), "target": TARGET},
        "pane": {
            "id": args.pane_id,
            "pid": args.pane_pid,
            "process_start_ticks": args.pane_start_ticks,
            "session_id": args.session_id.lower(),
            "target": TARGET,
        },
        "post_archive": post_archive,
        "report": validate_report(args, paths, task, todo),
        "source": source_binding(args),
        "terminal_receipt": str(args.terminal_receipt),
        "todo": todo.record(),
        "todo_custody": {
            "duplicate_section": "human pending",
            "interrupted_owners": interrupted_owners,
            "section": "human pending",
        },
    }


def state_payload(value: dict[str, object], id_field: str) -> bytes:
    return canonical_json({**value, id_field: bound_receipt_id(value)})


def prepared_payload(binding: dict[str, object]) -> bytes:
    return state_payload({"binding": binding, "phase": "prepared", "schema": STATE_SCHEMA}, "intent_id")


def terminalized_payload(binding: dict[str, object], intent_id: str, shell: ExitedCodexShell) -> bytes:
    value: dict[str, object] = {
        "binding": binding,
        "intent_id": intent_id,
        "phase": "terminalized",
        "schema": STATE_SCHEMA,
        "terminal": {
            "capture_sha256": shell.capture_sha256,
            "session_id": shell.session_id,
            "status": "authenticated-exited-shell",
        },
    }
    return state_payload(value, "receipt_id")


def optional_state(path: Path) -> tuple[dict[str, object], Snapshot] | None:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return None
    return json_snapshot(path, label="Source-1290 terminal prerequisite state")


def require_no_terminal_temporary(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        raise PrerequisiteError("terminalized receipt has unexpected transaction residue")


def write_new_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise PrerequisiteError("terminal receipt write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_state(path: Path, expected: bytes | None, desired: bytes) -> None:
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise PrerequisiteError("terminal receipt parent must be owner-private mode 0700")
    temporary = path.with_name(f".{path.name}.tmp")
    current = optional_state(path)
    if current is not None and current[1].payload == desired:
        if temporary.exists() or temporary.is_symlink():
            raise PrerequisiteError("terminal receipt has unexpected transaction residue")
        return
    current_payload = None if current is None else current[1].payload
    if current_payload != expected:
        raise PrerequisiteError("terminal receipt state changed concurrently")
    try:
        temporary_state = optional_state(temporary)
    except PrerequisiteError as exc:
        raise PrerequisiteError("terminal receipt temporary is unsafe") from exc
    if temporary_state is None:
        write_new_private(temporary, desired)
    elif temporary_state[1].payload != desired:
        raise PrerequisiteError("terminal receipt temporary conflicts with the requested transition")
    current = optional_state(path)
    if (None if current is None else current[1].payload) != expected:
        raise PrerequisiteError("terminal receipt state changed before publication")
    os.replace(temporary, path)
    fsync_directory(path.parent)
    published = json_snapshot(path, label="Source-1290 terminal prerequisite state")[1]
    if published.payload != desired:
        raise PrerequisiteError("terminal receipt publication changed")


def validate_state(value: dict[str, object], binding: dict[str, object]) -> str:
    expected_intent = json.loads(prepared_payload(binding))["intent_id"]
    phase = value.get("phase")
    if phase == "prepared":
        if set(value) != {"binding", "intent_id", "phase", "schema"} or value.get("schema") != STATE_SCHEMA or value.get("binding") != binding:
            raise PrerequisiteError("prepared terminal intent does not match current custody")
        validate_bound_id(value, "intent_id", "prepared terminal intent")
        return str(value["intent_id"])
    if phase == "terminalized":
        if set(value) != {"binding", "intent_id", "phase", "receipt_id", "schema", "terminal"} or value.get("schema") != STATE_SCHEMA or value.get("binding") != binding:
            raise PrerequisiteError("terminalized receipt does not match current custody")
        validate_bound_id(value, "receipt_id", "terminalized receipt")
        terminal = value.get("terminal")
        if (
            value.get("intent_id") != expected_intent
            or not isinstance(terminal, dict)
            or set(terminal) != {"capture_sha256", "session_id", "status"}
            or HASH_RE.fullmatch(str(terminal.get("capture_sha256", ""))) is None
            or UUID_RE.fullmatch(str(terminal.get("session_id", ""))) is None
            or terminal.get("status") != "authenticated-exited-shell"
        ):
            raise PrerequisiteError("terminalized receipt is malformed")
        return str(value["intent_id"])
    raise PrerequisiteError("terminal prerequisite state has an unknown phase")


def require_pane_identity(args: Args) -> None:
    if exact_pane_id(TARGET) != args.pane_id or process_start_ticks(args.pane_pid) != args.pane_start_ticks:
        raise PrerequisiteError("Source-1290 carrier pane identity drifted")


# 🧑 Human Source `202608/manager_mail/85c5dff58359-1290.txt:3-4`: "Close the “memory” thing. It is so old."
def reconcile(args: Args) -> bytes:
    """Terminalize only the carrier pane while its blocked lifecycle stays unchanged."""

    task = args.root / CANONICAL_CARRIER
    todo = args.root / "TODO.md"
    private_inputs = (args.report_file, args.acceptance_file, args.completed_audit, args.ownership_manifest, args.terminal_receipt)
    if task.parent != args.root or args.terminal_receipt.is_relative_to(args.root) or args.ownership_manifest.is_relative_to(args.root) or len(set(private_inputs)) != len(private_inputs):
        raise PrerequisiteError("terminal prerequisite paths violate the work-log boundary")
    paths = report_paths(args)
    indexed_paths = manifest_paths(args)
    with task_file_lock(args.root / ".omo-task-membership.lock"), task_target_lock(args.root, TARGET):
        lock_paths = {
            *indexed_paths,
            task,
            todo,
            args.completed_audit,
            args.ownership_manifest,
            args.report_file,
            args.acceptance_file,
            paths.receipt,
            paths.publication,
            paths.commitment,
            args.terminal_receipt,
            *(args.root / relative for relative in POST_ARCHIVE_FILES),
            *(CANONICAL_SOURCE_ROOT / relative for relative in SOURCE_FILES),
            *paths.route_evidence,
        }
        with ExitStack() as locks:
            for path in sorted(lock_paths):
                locks.enter_context(task_file_lock(path))
            if manifest_paths(args) != indexed_paths:
                raise PrerequisiteError("ownership manifest changed before locked preparation")
            binding = build_binding(args, paths)
            require_pane_identity(args)
            current = optional_state(args.terminal_receipt)
            if current is None:
                if current_command(args.pane_id) in SHELL_COMMANDS:
                    raise PrerequisiteError("exited shell is missing its durable pre-terminalization intent")
                prepared = prepared_payload(binding)
                replace_state(args.terminal_receipt, None, prepared)
                current = optional_state(args.terminal_receipt)
                assert current is not None
            intent_id = validate_state(current[0], binding)
            report_binding = binding.get("report")
            if not isinstance(report_binding, dict) or not isinstance(report_binding.get("receipt_id"), str):
                raise PrerequisiteError("terminal binding lost its report receipt identity")
            terminal_evidence = str(report_binding["receipt_id"])
            if current[0].get("phase") == "terminalized":
                require_no_terminal_temporary(args.terminal_receipt)
                terminal = current[0]["terminal"]
                assert isinstance(terminal, dict)
                shell = ExitedCodexShell(str(terminal.get("session_id", "")), str(terminal.get("capture_sha256", "")))
                require_pane_identity(args)
                observed = validate_exited_codex_shell(TARGET, args.pane_id, shell.session_id, terminal_evidence, args.lines)
                require_pane_identity(args)
                if observed != shell.capture_sha256 or shell.session_id != args.session_id.lower() or build_binding(args, paths) != binding:
                    raise PrerequisiteError("terminalized receipt or human-pending custody drifted")
                return current[1].payload
            prepared = current[1].payload

            def require_current() -> None:
                try:
                    state = optional_state(args.terminal_receipt)
                    current_binding = build_binding(args, paths)
                except (OSError, PrerequisiteError, TaskFrontmatterError, subprocess.SubprocessError, ValueError) as exc:
                    raise PrerequisiteError("Source-1290 lifecycle evidence drifted before terminalization") from exc
                if state is None or state[1].payload != prepared or current_binding != binding:
                    raise PrerequisiteError("Source-1290 lifecycle evidence drifted before terminalization")

            if current_command(args.pane_id) in SHELL_COMMANDS:
                shell = ExitedCodexShell(
                    args.session_id.lower(),
                    validate_exited_codex_shell(TARGET, args.pane_id, args.session_id, terminal_evidence, args.lines),
                )
            else:
                shell = terminalize_bound_codex_to_shell(
                    TARGET,
                    args.pane_id,
                    args.pane_pid,
                    args.pane_start_ticks,
                    args.session_id,
                    terminal_evidence,
                    require_current,
                    wait_s=args.wait_s,
                    n_lines=args.lines,
                )
            require_pane_identity(args)
            require_current()
            observed = validate_exited_codex_shell(TARGET, args.pane_id, shell.session_id, terminal_evidence, args.lines)
            if observed != shell.capture_sha256 or shell.session_id != args.session_id.lower():
                raise PrerequisiteError("authenticated exited shell changed before receipt publication")
            require_pane_identity(args)
            require_current()
            terminalized = terminalized_payload(binding, intent_id, shell)
            replace_state(args.terminal_receipt, prepared, terminalized)
            return terminalized


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--archive-todo-sha256", required=True)
    _ = parser.add_argument("--pane-id", required=True)
    _ = parser.add_argument("--pane-pid", type=int, required=True)
    _ = parser.add_argument("--pane-start-ticks", type=int, required=True)
    _ = parser.add_argument("--session-id", required=True)
    _ = parser.add_argument("--report-file", type=Path, required=True)
    _ = parser.add_argument("--report-sha256", required=True)
    _ = parser.add_argument("--report-status", choices=("blocked", "in-progress", "done"), required=True)
    _ = parser.add_argument("--acceptance-file", type=Path, required=True)
    _ = parser.add_argument("--acceptance-sha256", required=True)
    _ = parser.add_argument("--completed-audit", type=Path, required=True)
    _ = parser.add_argument("--completed-audit-sha256", required=True)
    _ = parser.add_argument("--ownership-manifest", type=Path, required=True)
    _ = parser.add_argument("--ownership-manifest-sha256", required=True)
    _ = parser.add_argument(
        "--source-head",
        required=True,
        help="full lowercase SHA from: git -C /home/sichangheagent/.config rev-parse --verify HEAD",
    )
    _ = parser.add_argument("--terminal-receipt", type=Path, required=True)
    _ = parser.add_argument("--wait-s", type=float, default=10.0)
    _ = parser.add_argument("--lines", type=int, default=2000)
    parsed = parser.parse_args(argv)
    hashes = (
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.archive_todo_sha256,
        parsed.report_sha256,
        parsed.acceptance_sha256,
        parsed.completed_audit_sha256,
        parsed.ownership_manifest_sha256,
    )
    if any(HASH_RE.fullmatch(value) is None for value in hashes):
        parser.error("all artifact digests must be lowercase SHA-256 values")
    if COMMIT_RE.fullmatch(parsed.source_head) is None:
        parser.error("--source-head must be one full lowercase Git SHA")
    try:
        require_source_head(parsed.source_head)
    except PrerequisiteError as exc:
        parser.error(str(exc))
    if PANE_RE.fullmatch(parsed.pane_id) is None or parsed.pane_pid <= 1 or parsed.pane_start_ticks <= 0 or UUID_RE.fullmatch(parsed.session_id) is None:
        parser.error("pane, process, and Codex session identities must be exact")
    if parsed.completed_audit_sha256 != SOURCE1290_AUDIT_SHA256:
        parser.error("--completed-audit-sha256 must identify the immutable Source-1290 audit")
    if parsed.wait_s < 0 or parsed.lines <= 0:
        parser.error("terminalization bounds are invalid")
    paths = tuple(path.expanduser().resolve(strict=False) for path in (parsed.report_file, parsed.acceptance_file, parsed.completed_audit, parsed.ownership_manifest, parsed.terminal_receipt))
    if any(not path.is_absolute() for path in paths):
        parser.error("artifact paths must be absolute")
    return Args(
        parsed.root.expanduser().resolve(),
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.archive_todo_sha256,
        parsed.pane_id,
        parsed.pane_pid,
        parsed.pane_start_ticks,
        parsed.session_id.lower(),
        paths[0],
        parsed.report_sha256,
        parsed.report_status,
        paths[1],
        parsed.acceptance_sha256,
        paths[2],
        parsed.completed_audit_sha256,
        paths[3],
        parsed.ownership_manifest_sha256,
        parsed.source_head,
        paths[4],
        parsed.wait_s,
        parsed.lines,
    )


def parse_ownership_preflight_args(argv: list[str]) -> OwnershipPreflightArgs:
    parser = argparse.ArgumentParser(description="Emit a bounded Source-1290 ownership manifest to stdout.")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    parsed = parser.parse_args(argv)
    if HASH_RE.fullmatch(parsed.todo_sha256) is None:
        parser.error("--todo-sha256 must be one lowercase SHA-256 value")
    return OwnershipPreflightArgs(parsed.root.expanduser().resolve(), parsed.todo_sha256)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["ownership-preflight"]:
            preflight = parse_ownership_preflight_args(arguments[1:])
            output = ownership_preflight(preflight.root, preflight.todo_sha256)
        else:
            output = reconcile(parse_args(arguments))
    except (OSError, RuntimeError, TaskFrontmatterError, subprocess.SubprocessError, ValueError) as exc:
        print(f"omo_source1290_prerequisite.py: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
