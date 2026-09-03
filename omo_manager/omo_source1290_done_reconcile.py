#!/usr/bin/env python3
"""Finish one interrupted Source-1290 authority carrier without sending mail."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import close_exited_codex_shell, close_note, validate_exited_codex_shell
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, UniqueKeyLoader, parse_task_metadata
from omo_manager.omo_task_status import BOOKKEEPING_FAILED_PREFIX, CLOSE_FAILED_PREFIX, DONE_CLOSE_IN_PROGRESS
from omo_manager.omo_task_status import authoritative_active_target_task_paths, finish_done_transaction, has_pending_marker
from omo_manager.omo_task_status import read_private_audit, reconcile_todo_text, relative_task_ref, replace_if_unchanged_locked
from omo_manager.omo_task_status import reserve_private_audit, root_membership_lock
from omo_manager.omo_task_status import same_file_state, task_path, update_frontmatter_status

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PANE_ID_RE = re.compile(r"%[0-9]+\Z")
SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
AUTHORITY_RE = re.compile(
    r'<human_instruction[ \t]+authoritative="true"[ \t]+source="([^"\r\n]+)">\r?\n(.*?)</human_instruction>',
    re.DOTALL,
)
# 🧑 Human Source `manager_mail/85c5dff58359-1290.txt:3-4`: “Close the ‘memory’ thing. It is so old.”
SOURCE1290_AUTHORITY = "manager_mail/85c5dff58359-1290.txt:3-4"
SOURCE1290_EXCERPT = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
SOURCE1290_AUDIT_SHA256 = "eafa5c27d35ea2dacb4c94a0c53619f06acfb66bef703bf63dc569ac7af5fedf"
SOURCE1290_ENVELOPE_SHA256 = "067dc445bef60174e0490198aa3859557fd29fcb0a055ae18c940dc0a945daea"
ARCHIVED_SOURCE1290_AUTHORITY = f"202608/{SOURCE1290_AUTHORITY}"
CANONICAL_CARRIER = Path("mem1290_auth.md")
DUPLICATE_CARRIER = Path("memory_auth_1290.md")
ARCHIVE_TODO = Path("202608/old_todos.md")
ARCHIVED_MEMORY = Path("202608/memory_research_mgr.md")
ARCHIVED_TRANSCRIPTION = Path("202608/transcription_sw.md")
ARCHIVED_INTERRUPTED_EVAL = Path("202608/mem1290_eval.md")
ARCHIVED_INTERRUPTED_FIX = Path("202608/mem1290_fix.md")
POST_ARCHIVE_SHA256 = {
    CANONICAL_CARRIER: "86e0cbe819e7b1d0f2899d35b903744209222d9eaa46ca8e6929bb63af1ec30a",
    DUPLICATE_CARRIER: "3a0291e6ea4c6aa8ef59055d65e97c53a8468d1a29d6e41c9aad7e760f59c811",
    ARCHIVED_MEMORY: "d2ae03a9e19f981ec43c6b8527fca1475a31a7c0593611c8ac6f36dbb392e705",
    ARCHIVED_TRANSCRIPTION: "a01fec08cfdcab16755a5d44c5ae78fde5110b05967ed7b0c324bba55cc6bea1",
    ARCHIVED_INTERRUPTED_EVAL: "62d641ddcaede3417b5bb024c676d0c3322f8d3bdbac31aa03b9e269259a19cf",
    ARCHIVED_INTERRUPTED_FIX: "ee0429ecf458721f24d4965e285dd59ade51742359b717fd1d697067826d35d5",
}
DUPLICATE_CARRIER_BLOCKER = "duplicate authority carrier created during concurrent routing; canonical carrier is mem1290_auth.md; no production ownership"
INTERRUPTED_TARGETS = {ARCHIVED_INTERRUPTED_EVAL: "vldr:2", ARCHIVED_INTERRUPTED_FIX: "vldr:1"}
AUDIT_FIELDS = {
    "version",
    "operation",
    "state",
    "task",
    "task_sha256",
    "source_task_text",
    "cancelled_pending_items",
    "cancelled_pending_items_sha256",
    "prior_blocker",
    "shared_target",
    "protected_task",
    "protected_task_sha256",
    "todo_sha256",
    "source_todo_text",
    "authority",
    "authority_sha256",
    "authority_envelope",
    "authority_envelope_sha256",
    "committed_task_sha256",
    "committed_task_text",
    "committed_todo_sha256",
    "committed_todo_text",
    "final-result",
}
MAX_AUDIT_BYTES = 1_000_000
RECOVERY_OPERATION = "source1290-carrier-close-intent"
RECOVERY_AUDIT_FIELDS = {
    "version",
    "operation",
    "state",
    "task",
    "target",
    "pane_id",
    "session_id",
    "terminal_evidence_sha256",
    "terminal_capture_sha256",
    "in_progress_task_sha256",
    "failed_task_sha256",
    "todo_sha256",
    "archive_todo_sha256",
    "completed_audit",
    "completed_audit_sha256",
    "authority_sha256",
    "authority_envelope_sha256",
    "memory_sha256",
    "transcription_sha256",
    "duplicate_carrier_sha256",
    "interrupted_eval_sha256",
    "interrupted_fix_sha256",
}


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    task_sha256: str
    todo_sha256: str
    archive_todo_sha256: str
    pane_id: str
    session_id: str
    terminal_evidence: str
    completed_audit: Path
    completed_audit_sha256: str


@dataclass(frozen=True)
class CompletedAudit:
    authority_sha256: str
    authority_envelope_sha256: str
    memory_text: str
    memory_sha256: str
    transcription_sha256: str
    todo_text: str
    todo_sha256: str


@dataclass(frozen=True)
class RecoveryAudit:
    text: str
    terminal_capture_sha256: str


@dataclass(frozen=True)
class PostArchiveState:
    archive_todo: str
    memory: str
    transcription: str
    duplicate_carrier: str
    interrupted_eval: str
    interrupted_fix: str


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fsync_bound_directory(path: Path) -> None:
    """Durably flush one unchanged, path-bound directory."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        bound_before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or (before.st_dev, before.st_ino) != (bound_before.st_dev, bound_before.st_ino):
            raise OSError("carrier directory identity drifted before durability flush")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        bound_after = os.stat(path, follow_symlinks=False)
    finally:
        os.close(descriptor)
    if not same_file_state(before, after) or (after.st_dev, after.st_ino) != (bound_after.st_dev, bound_after.st_ino):
        raise OSError("carrier directory identity drifted during durability flush")


def private_audit(path: Path, expected_sha256: str) -> tuple[bytes, os.stat_result]:
    """Read one exact owner-private completed audit without following links."""

    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise OSError("completed audit must be an absolute canonical path")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(path.parent, directory_flags)
    descriptor = -1
    try:
        parent_before = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_before.st_mode) or parent_before.st_uid != os.getuid() or stat.S_IMODE(parent_before.st_mode) & 0o077:
            raise OSError("completed audit directory must be owner-private")
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = MAX_AUDIT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        bound = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        parent_after = os.fstat(parent_descriptor)
        parent_bound = path.parent.lstat()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    if (
        len(payload) > MAX_AUDIT_BYTES
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or not same_file_state(before, after)
        or (after.st_dev, after.st_ino) != (bound.st_dev, bound.st_ino)
        or bound.st_uid != before.st_uid
        or stat.S_IMODE(bound.st_mode) != 0o600
        or not same_file_state(parent_before, parent_after)
        or (parent_after.st_dev, parent_after.st_ino) != (parent_bound.st_dev, parent_bound.st_ino)
        or parent_after.st_uid != os.getuid()
        or stat.S_IMODE(parent_after.st_mode) & 0o077
        or sha256(payload) != expected_sha256
    ):
        raise OSError("completed audit identity, mode, size, or digest drifted")
    return payload, before


def authority_blocks(text: str) -> tuple[tuple[str, str], ...]:
    return tuple((locator, excerpt.replace("\r\n", "\n")) for locator, excerpt in AUTHORITY_RE.findall(text))


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


def validate_completed_audit(payload: bytes, carrier: str, root: Path) -> CompletedAudit:
    """Authenticate the immutable successful Source-1290 cancellation receipt."""

    if sha256(payload) != SOURCE1290_AUDIT_SHA256:
        raise OSError("completed Source-1290 audit is not the exact immutable cancellation receipt")
    try:
        record = yaml.load(payload, Loader=UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OSError("completed Source-1290 audit is not unambiguous UTF-8 YAML") from exc
    if not isinstance(record, dict) or set(record) != AUDIT_FIELDS:
        raise OSError("completed Source-1290 audit fields are incomplete or unexpected")
    source_task = record["source_task_text"]
    committed_task = record["committed_task_text"]
    source_todo = record["source_todo_text"]
    committed_todo = record["committed_todo_text"]
    queue = record["cancelled_pending_items"]
    scalar_hashes = (
        record["task_sha256"],
        record["cancelled_pending_items_sha256"],
        record["protected_task_sha256"],
        record["todo_sha256"],
        record["authority_sha256"],
        record["authority_envelope_sha256"],
        record["committed_task_sha256"],
        record["committed_todo_sha256"],
    )
    if (
        record["version"] != "v1.0.0"
        or record["operation"] != "cancel-shared-target"
        or record["state"] != "prepared"
        or record["task"] != "memory_research_mgr.md"
        or record["shared_target"] != "wl:32"
        or record["protected_task"] != "transcription_sw.md"
        or record["authority"] != SOURCE1290_AUTHORITY
        or record["authority_envelope"] != carrier
        or record["final-result"] != "success"
        or not isinstance(source_task, str)
        or not isinstance(committed_task, str)
        or not isinstance(source_todo, str)
        or not isinstance(committed_todo, str)
        or not isinstance(record["prior_blocker"], str)
        or not record["prior_blocker"]
        or not isinstance(queue, list)
        or not queue
        or any(not isinstance(item, str) or not item for item in queue)
        or any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in scalar_hashes)
    ):
        raise OSError("completed Source-1290 audit does not bind the exact cancellation and authority carrier")
    queue_digest = sha256(yaml.safe_dump(queue, sort_keys=False).encode())
    if (
        sha256(source_task.encode()) != record["task_sha256"]
        or queue_digest != record["cancelled_pending_items_sha256"]
        or sha256(source_todo.encode()) != record["todo_sha256"]
        or sha256(committed_task.encode()) != record["committed_task_sha256"]
        or sha256(committed_todo.encode()) != record["committed_todo_sha256"]
    ):
        raise OSError("completed Source-1290 audit recovery images or queue digest drifted")
    source_metadata = parse_task_metadata(source_task, root)
    committed_metadata = parse_task_metadata(committed_task, root)
    if (
        source_metadata is None
        or source_metadata.version != "v1.0.0"
        or source_metadata.status != "blocked"
        or not source_metadata.is_manager
        or source_metadata.runat != "wl:32"
        or list(source_metadata.pending_task_items) != queue
        or source_metadata.blocked_on != record["prior_blocker"]
        or committed_metadata is None
        or committed_metadata.status != "done"
        or committed_metadata.runat != "wl:32"
        or committed_metadata.pending_task_items
        or todo_rows(source_todo, "transcription_sw.md") != (("current", "transcription_sw.md wl:32"),)
        or todo_rows(source_todo, "memory_research_mgr.md") != (("current", "memory_research_mgr.md wl:32"),)
        or todo_rows(committed_todo, "transcription_sw.md") != (("current", "transcription_sw.md wl:32"),)
        or todo_rows(committed_todo, "memory_research_mgr.md") != (("previous", "memory_research_mgr.md wl:32"),)
    ):
        raise OSError("completed Source-1290 audit lost its task, queue, TODO, or transcription evidence")
    return CompletedAudit(
        str(record["authority_sha256"]),
        str(record["authority_envelope_sha256"]),
        committed_task,
        str(record["committed_task_sha256"]),
        str(record["protected_task_sha256"]),
        committed_todo,
        str(record["committed_todo_sha256"]),
    )


def source1290_authority(root: Path, expected_sha256: str) -> tuple[bytes, os.stat_result]:
    """Authenticate the exact archived Source-1290 mailbox source."""

    original = root / SOURCE1290_AUTHORITY.partition(":")[0]
    try:
        _ = original.lstat()
    except FileNotFoundError:
        pass
    else:
        raise OSError("pre-archive Source-1290 mailbox source unexpectedly exists")
    relative = Path(ARCHIVED_SOURCE1290_AUTHORITY.partition(":")[0])
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_descriptor = os.open(root, directory_flags)
    directory_descriptors = [root_descriptor]
    descriptor = -1
    try:
        directory_before = [os.fstat(root_descriptor)]
        for part in relative.parts[:-1]:
            directory_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptors[-1])
            directory_descriptors.append(directory_descriptor)
            directory_state = os.fstat(directory_descriptor)
            directory_before.append(directory_state)
            if not stat.S_ISDIR(directory_state.st_mode) or directory_state.st_uid != os.getuid() or stat.S_IMODE(directory_state.st_mode) & 0o022:
                raise OSError("Source-1290 archived mailbox directory is not owner-controlled")
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directory_descriptors[-1])
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = MAX_AUDIT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        bound = os.stat(relative.parts[-1], dir_fd=directory_descriptors[-1], follow_symlinks=False)
        directory_after = [os.fstat(directory_descriptor) for directory_descriptor in directory_descriptors]
        directory_bound = [root.lstat()]
        directory_bound.extend(
            os.stat(part, dir_fd=directory_descriptors[index], follow_symlinks=False)
            for index, part in enumerate(relative.parts[:-1])
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    if (
        len(payload) > MAX_AUDIT_BYTES
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or not same_file_state(before, after)
        or (after.st_dev, after.st_ino) != (bound.st_dev, bound.st_ino)
        or bound.st_uid != before.st_uid
        or stat.S_IMODE(bound.st_mode) != 0o600
        or any(not same_file_state(old, new) for old, new in zip(directory_before, directory_after, strict=True))
        or any((new.st_dev, new.st_ino) != (bound_directory.st_dev, bound_directory.st_ino) for new, bound_directory in zip(directory_after, directory_bound, strict=True))
        or sha256(payload) != expected_sha256
    ):
        raise OSError("Source-1290 mailbox identity, mode, size, or digest drifted")
    try:
        lines = payload.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise OSError("Source-1290 mailbox source is not UTF-8") from exc
    if len(lines) < 4 or "".join(lines[2:4]).replace("\r\n", "\n") != SOURCE1290_EXCERPT:
        raise OSError("Source-1290 mailbox lines 3-4 no longer contain the exact Human authority")
    return payload, before


def task_paths(root: Path) -> tuple[Path, ...]:
    """Return one strict immutable Markdown membership snapshot."""

    paths: list[Path] = []
    for candidate in root.rglob("*.md"):
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(f"work-log membership contains an unsafe Markdown entry: {candidate}")
        resolved = candidate.resolve(strict=True)
        _ = resolved.relative_to(root)
        paths.append(resolved)
    if len(paths) != len(set(paths)):
        raise OSError("work-log Markdown membership contains aliases")
    return tuple(sorted(paths))


def read_post_archive_state(root: Path, args: Args) -> PostArchiveState:
    """Authenticate the exact Source-1290 records after the August archive."""

    texts = {
        relative: (root / relative).read_text(encoding="utf-8")
        for relative in (DUPLICATE_CARRIER, ARCHIVED_MEMORY, ARCHIVED_TRANSCRIPTION, *INTERRUPTED_TARGETS)
    }
    archive_todo = (root / ARCHIVE_TODO).read_text(encoding="utf-8")
    if sha256(archive_todo.encode()) != args.archive_todo_sha256:
        raise OSError("archived TODO bytes do not match --archive-todo-sha256")
    if any(sha256(text.encode()) != POST_ARCHIVE_SHA256[relative] for relative, text in texts.items()):
        raise OSError("Source-1290 post-archive record bytes drifted")
    for original in (Path("memory_research_mgr.md"), Path("transcription_sw.md"), Path(SOURCE1290_AUTHORITY.partition(":")[0])):
        try:
            _ = (root / original).lstat()
        except FileNotFoundError:
            continue
        raise OSError("Source-1290 pre-archive record unexpectedly exists")
    memory_metadata = parse_task_metadata(texts[ARCHIVED_MEMORY], root)
    transcription_metadata = parse_task_metadata(texts[ARCHIVED_TRANSCRIPTION], root)
    duplicate_metadata = parse_task_metadata(texts[DUPLICATE_CARRIER], root)
    if (
        memory_metadata is None
        or memory_metadata.status != "done"
        or not memory_metadata.is_manager
        or memory_metadata.runat != "wl:32"
        or memory_metadata.pending_task_items
        or has_pending_marker(texts[ARCHIVED_MEMORY])
        or transcription_metadata is None
        or transcription_metadata.status != "done"
        or transcription_metadata.is_manager
        or transcription_metadata.runat != "wl:32"
        or transcription_metadata.pending_task_items
        or has_pending_marker(texts[ARCHIVED_TRANSCRIPTION])
        or duplicate_metadata is None
        or duplicate_metadata.status != "blocked"
        or duplicate_metadata.blocked_on != DUPLICATE_CARRIER_BLOCKER
        or duplicate_metadata.is_manager
        or duplicate_metadata.runat != "agent_managers:78"
        or duplicate_metadata.pending_task_items
        or has_pending_marker(texts[DUPLICATE_CARRIER])
        or authority_blocks(texts[DUPLICATE_CARRIER]) != ((ARCHIVED_SOURCE1290_AUTHORITY, SOURCE1290_EXCERPT),)
    ):
        raise OSError("Source-1290 post-archive memory, transcription, or duplicate-carrier custody drifted")
    for relative, target in INTERRUPTED_TARGETS.items():
        metadata = parse_task_metadata(texts[relative], root)
        if (
            metadata is None
            or metadata.status != "done"
            or metadata.blocked_on
            or metadata.is_manager
            or metadata.runat != target
            or metadata.pending_task_items
            or has_pending_marker(texts[relative])
        ):
            raise OSError("Source-1290 archived interrupted-close evidence drifted")
    archive_section = "archived from todo.md previous on 2026-09-01"
    if (
        todo_rows(archive_todo, "memory_research_mgr.md") != ((archive_section, "memory_research_mgr.md wl:32"),)
        or todo_rows(archive_todo, "transcription_sw.md") != ((archive_section, "transcription_sw.md wl:32"),)
    ):
        raise OSError("Source-1290 archived TODO custody drifted")
    return PostArchiveState(
        archive_todo,
        texts[ARCHIVED_MEMORY],
        texts[ARCHIVED_TRANSCRIPTION],
        texts[DUPLICATE_CARRIER],
        texts[ARCHIVED_INTERRUPTED_EVAL],
        texts[ARCHIVED_INTERRUPTED_FIX],
    )


def validate_post_archive_todo(text: str, carrier: str, target: str) -> None:
    """Require the exact live and archived Source-1290 custody rows."""

    if (
        todo_rows(text, carrier) != (("current", f"{carrier} {target}"),)
        or todo_rows(text, DUPLICATE_CARRIER.as_posix()) != (("human pending", "memory_auth_1290.md agent_managers:78"),)
        or todo_rows(text, ARCHIVED_INTERRUPTED_EVAL.as_posix()) != (("previous", "202608/mem1290_eval.md vldr:2"),)
        or todo_rows(text, ARCHIVED_INTERRUPTED_FIX.as_posix()) != (("previous", "202608/mem1290_fix.md vldr:1"),)
        or todo_rows(text, "memory_research_mgr.md")
        or todo_rows(text, ARCHIVED_MEMORY.as_posix())
        or todo_rows(text, "transcription_sw.md")
        or todo_rows(text, ARCHIVED_TRANSCRIPTION.as_posix())
    ):
        raise OSError("current TODO does not preserve the exact Source-1290 post-archive custody")


def archived_helper_targets_are_unowned(root: Path) -> bool:
    return all(not authoritative_active_target_task_paths(root, target) for target in INTERRUPTED_TARGETS.values())


def post_archive_state_is_current(root: Path, args: Args, expected: PostArchiveState) -> bool:
    try:
        return read_post_archive_state(root, args) == expected
    except (OSError, TaskFrontmatterError, UnicodeDecodeError):
        return False


def validate_carrier(root: Path, path: Path, text: str, expected_sha256: str, pane_id: str) -> tuple[str, bool]:
    """Validate the exact interrupted empty-queue authority carrier."""

    if sha256(text.encode()) != expected_sha256:
        raise OSError("authority-carrier task bytes do not match --task-sha256")
    metadata = parse_task_metadata(text, root)
    expected_authority = (ARCHIVED_SOURCE1290_AUTHORITY, SOURCE1290_EXCERPT)
    failed_reason = f"{CLOSE_FAILED_PREFIX}: target is not a supported live Codex pane: {pane_id} status=not_codex"
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on not in {DONE_CLOSE_IN_PROGRESS, failed_reason}
        or metadata.is_manager
        or metadata.pending_task_items
        or has_pending_marker(text)
        or authority_blocks(text) != (expected_authority,)
        or metadata.runat.partition(":")[0].startswith("h")
        or relative_task_ref(root, path) != CANONICAL_CARRIER.as_posix()
    ):
        raise OSError("task is not the exact interrupted non-human Source-1290 authority carrier with an empty queue")
    in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS, root)
    if sha256(in_progress.encode()) != POST_ARCHIVE_SHA256[CANONICAL_CARRIER]:
        raise OSError("authority-carrier bytes do not derive from the exact post-archive state")
    return metadata.runat, metadata.blocked_on == failed_reason


def validate_audit_bound_carrier(root: Path, text: str, audit: CompletedAudit) -> None:
    """Bind the immutable audit envelope to its exact post-archive carrier state."""

    in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS, root)
    if audit.authority_envelope_sha256 != SOURCE1290_ENVELOPE_SHA256 or sha256(in_progress.encode()) != POST_ARCHIVE_SHA256[CANONICAL_CARRIER]:
        raise OSError("completed Source-1290 audit does not bind the exact post-archive carrier")


def recovery_path(completed_audit: Path) -> Path:
    return completed_audit.with_name(f"{completed_audit.name}.source1290-carrier-close-intent")


def recovery_audit_text(
    args: Args,
    carrier: str,
    target: str,
    in_progress_text: str,
    failed_text: str,
    audit: CompletedAudit,
    state: PostArchiveState,
    terminal_capture_sha256: str,
) -> str:
    record = {
        "version": "v1.0.0",
        "operation": RECOVERY_OPERATION,
        "state": "prepared",
        "task": carrier,
        "target": target,
        "pane_id": args.pane_id,
        "session_id": args.session_id,
        "terminal_evidence_sha256": sha256(args.terminal_evidence.encode()),
        "terminal_capture_sha256": terminal_capture_sha256,
        "in_progress_task_sha256": sha256(in_progress_text.encode()),
        "failed_task_sha256": sha256(failed_text.encode()),
        "todo_sha256": args.todo_sha256,
        "archive_todo_sha256": args.archive_todo_sha256,
        "completed_audit": str(args.completed_audit),
        "completed_audit_sha256": args.completed_audit_sha256,
        "authority_sha256": audit.authority_sha256,
        "authority_envelope_sha256": audit.authority_envelope_sha256,
        "memory_sha256": sha256(state.memory.encode()),
        "transcription_sha256": sha256(state.transcription.encode()),
        "duplicate_carrier_sha256": sha256(state.duplicate_carrier.encode()),
        "interrupted_eval_sha256": sha256(state.interrupted_eval.encode()),
        "interrupted_fix_sha256": sha256(state.interrupted_fix.encode()),
    }
    return yaml.safe_dump(record, sort_keys=False)


def validate_recovery_audit(
    text: str,
    args: Args,
    carrier: str,
    target: str,
    in_progress_text: str,
    failed_text: str,
    audit: CompletedAudit,
    state: PostArchiveState,
) -> RecoveryAudit:
    try:
        record = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise OSError("Source-1290 close recovery audit is ambiguous or malformed") from exc
    if not isinstance(record, dict) or set(record) != RECOVERY_AUDIT_FIELDS:
        raise OSError("Source-1290 close recovery audit fields are incomplete or unexpected")
    terminal_capture_sha256 = record["terminal_capture_sha256"]
    expected = yaml.safe_load(
        recovery_audit_text(
            args,
            carrier,
            target,
            in_progress_text,
            failed_text,
            audit,
            state,
            str(terminal_capture_sha256),
        )
    )
    if not isinstance(terminal_capture_sha256, str) or SHA256_RE.fullmatch(terminal_capture_sha256) is None or record != expected:
        raise OSError("Source-1290 close recovery audit lost its exact evidence binding")
    return RecoveryAudit(text, terminal_capture_sha256)


def bookkeeping_failed(text: str, root: Path, reason: str) -> str:
    normalized = " ".join(reason.split()) or "unknown bookkeeping failure"
    return update_frontmatter_status(text, "blocked", f"{BOOKKEEPING_FAILED_PREFIX}: {normalized}", root)


def reconcile(args: Args) -> None:
    """Handoff to failed-close recovery, then close and finish only the carrier."""

    root = args.root.resolve(strict=True)
    path = task_path(root, args.task_file)
    todo = root / "TODO.md"
    if path == todo or path.is_symlink() or not path.is_file() or not todo.is_file():
        raise OSError("carrier and TODO must be distinct regular files")
    carrier = relative_task_ref(root, path)
    initial_text = path.read_text(encoding="utf-8")
    target, initial_failed = validate_carrier(root, path, initial_text, args.task_sha256, args.pane_id)
    initial_before = path.stat()
    todo_text = todo.read_text(encoding="utf-8")
    todo_before = todo.stat()
    if sha256(todo_text.encode()) != args.todo_sha256:
        raise OSError("TODO bytes do not match --todo-sha256")
    audit_payload, audit_before = private_audit(args.completed_audit, args.completed_audit_sha256)
    audit = validate_completed_audit(audit_payload, carrier, root)
    validate_audit_bound_carrier(root, initial_text, audit)
    authority_path = root / ARCHIVED_SOURCE1290_AUTHORITY.partition(":")[0]
    authority_payload, authority_before = source1290_authority(root, audit.authority_sha256)
    expected_authority = (ARCHIVED_SOURCE1290_AUTHORITY, SOURCE1290_EXCERPT)
    failed_reason = f"{CLOSE_FAILED_PREFIX}: target is not a supported live Codex pane: {args.pane_id} status=not_codex"
    close_intent_path = recovery_path(args.completed_audit)
    with root_membership_lock(root), task_target_lock(root, target):
        paths = task_paths(root)
        required_paths = {path, todo, *(root / relative for relative in (ARCHIVE_TODO, DUPLICATE_CARRIER, ARCHIVED_MEMORY, ARCHIVED_TRANSCRIPTION, *INTERRUPTED_TARGETS))}
        if not required_paths.issubset(paths):
            raise OSError("Source-1290 carrier or post-archive custody record disappeared from work-log membership")
        with ExitStack() as locks:
            for locked_path in sorted({*paths, args.completed_audit, authority_path, close_intent_path}, key=str):
                locks.enter_context(task_file_lock(locked_path))
            if task_paths(root) != paths:
                raise OSError("work-log membership changed while Source-1290 recovery acquired locks")
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            current_todo_before = todo.stat()
            current_todo = todo.read_text(encoding="utf-8")
            current_audit, current_audit_before = private_audit(args.completed_audit, args.completed_audit_sha256)
            current_authority, current_authority_before = source1290_authority(root, audit.authority_sha256)
            if (
                not same_file_state(initial_before, current_before)
                or current_text != initial_text
                or not same_file_state(todo_before, current_todo_before)
                or current_todo != todo_text
                or not same_file_state(audit_before, current_audit_before)
                or current_audit != audit_payload
                or not same_file_state(authority_before, current_authority_before)
                or current_authority != authority_payload
            ):
                raise OSError("carrier, TODO, or completed audit changed before locked reconciliation")
            target, current_failed = validate_carrier(root, path, current_text, args.task_sha256, args.pane_id)
            if current_failed != initial_failed:
                raise OSError("carrier close phase changed before locked reconciliation")
            if validate_completed_audit(current_audit, carrier, root) != audit:
                raise OSError("completed Source-1290 audit binding changed")
            validate_audit_bound_carrier(root, current_text, audit)
            state = read_post_archive_state(root, args)
            validate_post_archive_todo(current_todo, carrier, target)
            if not archived_helper_targets_are_unowned(root):
                raise OSError("Source-1290 archived helper target ownership drifted")
            owners = authoritative_active_target_task_paths(root, target)
            if owners != (path,):
                names = ", ".join(relative_task_ref(root, owner) for owner in owners) or "none"
                raise OSError(f"authority carrier is not the sole active owner of {target}: {names}")
            updated_todo = reconcile_todo_text(root, path, current_todo, target, "previous", ("current",))
            in_progress_text = update_frontmatter_status(current_text, "blocked", DONE_CLOSE_IN_PROGRESS, root)
            failed_text = update_frontmatter_status(in_progress_text, "blocked", failed_reason, root)
            if current_text not in {in_progress_text, failed_text}:
                raise OSError("carrier contains changes beyond the permitted done-close lifecycle transition")
            recovery_text = read_private_audit(close_intent_path)
            recovery: RecoveryAudit | None = None
            if recovery_text is not None:
                recovery = validate_recovery_audit(
                    recovery_text,
                    args,
                    carrier,
                    target,
                    in_progress_text,
                    failed_text,
                    audit,
                    state,
                )
                if current_text != failed_text:
                    raise OSError("durable Source-1290 close intent requires the exact done_close_failed carrier state")
            resolved_pane = exact_pane_id(target)
            if resolved_pane not in {"", args.pane_id}:
                raise OSError(f"authority-carrier pane rebound to {resolved_pane}")
            if not resolved_pane and recovery is None:
                raise OSError("authority-carrier pane is absent without an evidence-bound close intent")
            if resolved_pane:
                capture_sha256 = validate_exited_codex_shell(
                    target,
                    args.pane_id,
                    args.session_id,
                    args.terminal_evidence,
                )
                if recovery is not None and capture_sha256 != recovery.terminal_capture_sha256:
                    raise OSError("terminal shell capture drifted from its durable close intent")
            else:
                assert recovery is not None
                capture_sha256 = recovery.terminal_capture_sha256
            if (
                path.read_text(encoding="utf-8") != current_text
                or todo.read_text(encoding="utf-8") != current_todo
                or private_audit(args.completed_audit, args.completed_audit_sha256)[0] != current_audit
                or source1290_authority(root, audit.authority_sha256)[0] != current_authority
                or not post_archive_state_is_current(root, args, state)
                or task_paths(root) != paths
                or authoritative_active_target_task_paths(root, target) != (path,)
                or not archived_helper_targets_are_unowned(root)
                or exact_pane_id(target) != resolved_pane
                or read_private_audit(close_intent_path) != recovery_text
            ):
                raise OSError("task, audit, membership, ownership, or pane identity drifted after shell authentication")
            if authority_blocks(failed_text) != (expected_authority,):
                raise OSError("failed-close handoff would lose Source-1290 authority custody")
            if recovery is None:
                if current_text != failed_text:
                    replace_if_unchanged_locked(path, failed_text, current_before)
                failed_before = path.stat()
            else:
                failed_before = current_before
            fsync_bound_directory(path.parent)
            if recovery is None:
                prepared_recovery = recovery_audit_text(
                    args,
                    carrier,
                    target,
                    in_progress_text,
                    failed_text,
                    audit,
                    state,
                    capture_sha256,
                )
                reserve_private_audit(close_intent_path, prepared_recovery)
                recovery_text = prepared_recovery
                recovery = validate_recovery_audit(
                    recovery_text,
                    args,
                    carrier,
                    target,
                    in_progress_text,
                    failed_text,
                    audit,
                    state,
                )

            def evidence_is_current(expected_pane_id: str) -> bool:
                try:
                    return (
                        exact_pane_id(target) == expected_pane_id
                        and path.read_text(encoding="utf-8") == failed_text
                        and todo.read_text(encoding="utf-8") == current_todo
                        and read_private_audit(close_intent_path) == recovery_text
                        and private_audit(args.completed_audit, args.completed_audit_sha256)[0] == current_audit
                        and source1290_authority(root, audit.authority_sha256)[0] == current_authority
                        and post_archive_state_is_current(root, args, state)
                        and task_paths(root) == paths
                        and authoritative_active_target_task_paths(root, target) == (path,)
                        and archived_helper_targets_are_unowned(root)
                    )
                except (OSError, TaskFrontmatterError):
                    return False

            if resolved_pane:
                try:
                    close_exited_codex_shell(
                        target,
                        args.pane_id,
                        args.session_id,
                        args.terminal_evidence,
                        expected_capture_sha256=capture_sha256,
                        evidence_is_current=lambda: evidence_is_current(args.pane_id),
                    )
                except Exception as exc:
                    raise TaskFrontmatterError("ordinary-shell close did not complete; carrier is now in the existing done_close_failed recovery state") from exc
                if exact_pane_id(target):
                    raise TaskFrontmatterError("ordinary-shell close returned while the authority-carrier target remained live")
            elif exact_pane_id(target):
                raise OSError("authority-carrier target reappeared during evidence-bound absent-pane finish")
            elif not evidence_is_current(""):
                raise OSError("bound lifecycle evidence changed before evidence-bound absent-pane finish")
            noted_text = failed_text.rstrip("\n") + close_note(target, args.session_id)
            if authority_blocks(noted_text) != (expected_authority,):
                raise OSError("close bookkeeping would lose Source-1290 authority custody")
            try:
                replace_if_unchanged_locked(path, noted_text, failed_before)
                noted_before = path.stat()
                done_text = update_frontmatter_status(noted_text, "done", "", root)
                finish_done_transaction(
                    root,
                    path,
                    done_text,
                    noted_before,
                    locked=True,
                    todo_text=current_todo,
                    prepared_todo=updated_todo,
                    todo_before=current_todo_before,
                )
            except Exception as exc:
                retry_before = path.stat()
                retry_text = path.read_text(encoding="utf-8")
                if retry_text in {failed_text, noted_text}:
                    replace_if_unchanged_locked(path, bookkeeping_failed(retry_text, root, str(exc)), retry_before)
                raise TaskFrontmatterError("carrier shell closed but done bookkeeping did not complete") from exc
            if (
                authority_blocks(path.read_text(encoding="utf-8")) != (expected_authority,)
                or private_audit(args.completed_audit, args.completed_audit_sha256)[0] != current_audit
                or source1290_authority(root, audit.authority_sha256)[0] != current_authority
                or not post_archive_state_is_current(root, args, state)
                or todo.read_text(encoding="utf-8") != updated_todo
                or task_paths(root) != paths
                or not archived_helper_targets_are_unowned(root)
                or recovery is None
                or read_private_audit(close_intent_path) != recovery.text
            ):
                raise OSError("committed carrier-only closure lost authority, audit, or membership evidence")


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-file", type=Path, required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--archive-todo-sha256", required=True)
    _ = parser.add_argument("--pane-id", required=True)
    _ = parser.add_argument("--session-id", required=True)
    _ = parser.add_argument("--terminal-evidence", required=True)
    _ = parser.add_argument("--completed-audit", type=Path, required=True)
    _ = parser.add_argument("--completed-audit-sha256", required=True)
    parsed = parser.parse_args(argv)
    hashes = (
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.archive_todo_sha256,
        parsed.completed_audit_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        parser.error("all task, custody, TODO, and audit digests must be lowercase SHA-256 values")
    if parsed.completed_audit_sha256 != SOURCE1290_AUDIT_SHA256:
        parser.error("completed-audit digest must identify the exact immutable Source-1290 receipt")
    if PANE_ID_RE.fullmatch(parsed.pane_id) is None or SESSION_ID_RE.fullmatch(parsed.session_id) is None:
        parser.error("pane id and Codex session id must be exact")
    if len(parsed.terminal_evidence.strip()) < 12:
        parser.error("terminal evidence must be a specific nonempty report token")
    audit = parsed.completed_audit.expanduser()
    if not audit.is_absolute():
        parser.error("completed audit must be an absolute path")
    return Args(
        parsed.root.expanduser().resolve(),
        parsed.task_file,
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.archive_todo_sha256,
        parsed.pane_id,
        parsed.session_id,
        parsed.terminal_evidence.strip(),
        audit.resolve(strict=False),
        parsed.completed_audit_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        reconcile(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, TaskFrontmatterError, UnicodeDecodeError, ValueError) as exc:
        print(f"omo_source1290_done_reconcile.py: {exc}", file=sys.stderr)
        return 2
    print("Closed only the Source-1290 authority carrier; preserved its evidence and completed audit without mail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
