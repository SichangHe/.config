#!/usr/bin/env python3
"""Create fresh recovery evidence without consuming an unauthorized adoption receipt."""
from __future__ import annotations

import argparse
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

from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_production_approval import read_approval, validate_approval

SHA256_LENGTH = 64
ADOPTION_SCHEMA = "omo-completion-mail-adoption/v1"
RECOVERY_SCHEMA = "omo-completion-incident-recovery/v1"
TASK_NAME = "transcription_sw.md"
TASK_TARGET = "wl:32"
TASK_MANAGER = "wl:1"
OTHER_OWNER_NAME = "memory_research_mgr.md"
TODO_NAME = "TODO.md"
MESSAGE_ID = "<178815460436.2815805.14149274743602497510@gmail.com>"
THREAD_ROOT_MESSAGE_ID = "<178815432253.2784108.9648480549229137852@gmail.com>"
OUTCOME = "task done"
ADOPTION_FIELDS = {
    "schema",
    "root",
    "task",
    "task_sha256",
    "owner",
    "outcome",
    "pending_task_items",
    "mail_policy",
    "message_id",
    "thread_root_message_id",
    "subject",
    "provider",
    "all_mail_uid",
    "all_mail_uidvalidity",
    "sent_mail_uid",
    "sent_mail_uidvalidity",
    "gmail_message_id",
    "gmail_thread_id",
    "internaldate_unix_ms",
    "raw_sha256",
    "body_sha256",
    "thread_message_ids",
}


@dataclass(frozen=True)
class Args:
    root: Path
    task_sha256: str
    todo_sha256: str
    other_sha256: str
    incident_path: Path
    incident_sha256: str
    incident_device: int
    incident_inode: int
    incident_size: int
    incident_mtime_ns: int
    incident_mode: int
    incident_uid: int
    incident_gid: int
    output: Path
    approved_packet_sha256: str
    approval_path: Path
    approval_sha256: str


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def root_membership_lock(root: Path):
    return task_file_lock(root / ".omo-task-membership.lock")


def read_private_file(path: Path, expected_sha256: str) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        raise OSError("incident receipt must be an absolute canonical path")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise OSError("incident receipt must be an owner-private regular file")
        payload = b""
        while chunk := os.read(descriptor, min(65_536, 65_537 - len(payload))):
            payload += chunk
            if len(payload) > 65_536:
                break
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    bound = path.lstat()
    if len(payload) > 65_536:
        raise OSError("incident receipt is oversized")
    if not same_file_state(before, after) or (bound.st_dev, bound.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("incident receipt changed while read")
    if sha256(payload) != expected_sha256:
        raise OSError("incident receipt bytes do not match the bound SHA-256")
    return payload


def parse_receipt(payload: bytes, root: Path, task_sha256: str) -> tuple[str, ...]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate incident receipt field: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OSError("incident receipt is not unambiguous UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != ADOPTION_FIELDS:
        raise OSError("incident receipt has an incomplete or unknown schema")
    items = value["pending_task_items"]
    if (
        value["schema"] != ADOPTION_SCHEMA
        or value["root"] != str(root)
        or value["task"] != TASK_NAME
        or value["task_sha256"] != task_sha256
        or value["owner"] != TASK_TARGET
        or value["outcome"] != OUTCOME
        or value["mail_policy"] != "already-delivered-no-resend"
        or value["message_id"] != MESSAGE_ID
        or value["thread_root_message_id"] != THREAD_ROOT_MESSAGE_ID
        or value["subject"] != "Re: [wl:32] transcription software"
        or value["provider"] != "gmail-agent-sent"
        or value["all_mail_uid"] != "6749"
        or value["all_mail_uidvalidity"] != "12"
        or value["sent_mail_uid"] != "5479"
        or value["sent_mail_uidvalidity"] != "5"
        or value["gmail_message_id"] != "1875016003461706226"
        or value["gmail_thread_id"] != "1875015707857181726"
        or value["internaldate_unix_ms"] != "1788154605000"
        or value["raw_sha256"] != "60851e043fee1905496fd48ddff6d6d10a2e175e4975ebcc3f236b05a64b3eac"
        or value["body_sha256"] != "6cb31298f73031c28da1c35613c4daf59aa6ce06c3803cdb9f4bb5c7763f911e"
        or value["thread_message_ids"] != [THREAD_ROOT_MESSAGE_ID, MESSAGE_ID]
        or not isinstance(items, list)
        or not items
        or any(not isinstance(item, str) or not item for item in items)
    ):
        raise OSError("incident receipt does not bind the exact no-resend delivery")
    return tuple(items)


def canonical_target(value: str) -> str:
    match = re.fullmatch(r"([^:\s]+):(\d+)(?:\.(\d+))?", value)
    if match is None:
        return value
    session, window, pane = match.groups()
    result = f"{session}:{int(window)}"
    return result if pane is None or int(pane) == 0 else f"{result}.{int(pane)}"


def possibly_claims_target(text: str, target: str) -> bool:
    session, _separator, window = target.partition(":")
    if not session or not window.isdecimal():
        return False
    return re.search(rf"(?mi)^[ \t]*runat[ \t]*:[ \t]*(?:![^ \t\r\n]+[ \t]+)?(?:['\"])?{re.escape(session)}:0*{int(window)}(?:\.0+)?(?:['\"])?[ \t]*(?:#.*)?$", text) is not None


def task_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for candidate in root.rglob("*.md"):
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(f"work-log membership contains an unsafe Markdown entry: {candidate}")
        resolved = candidate.resolve()
        resolved.relative_to(root)
        paths.append(resolved)
    if len(paths) != len(set(paths)):
        raise OSError("work-log Markdown membership contains aliases")
    return tuple(sorted(paths))


def active_target_owners(root: Path, target: str, paths: tuple[Path, ...]) -> tuple[Path, ...]:
    owners: list[Path] = []
    for path in paths:
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
            metadata = parse_task_metadata(text, root)
        except (OSError, UnicodeDecodeError, TaskFrontmatterError) as exc:
            if possibly_claims_target(text, target):
                raise OSError(f"cannot exclude malformed target owner {path.relative_to(root)}") from exc
            continue
        if metadata is not None and metadata.status != "done" and canonical_target(metadata.runat) == canonical_target(target):
            owners.append(path)
    return tuple(sorted(owners))


def has_pending_marker(text: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and stripped == "(pending)":
            return True
    return False


def other_owner_is_preserved(root: Path, text: str) -> bool:
    metadata = parse_task_metadata(text, root)
    return bool(metadata is not None and metadata.version == "v1.0.0" and metadata.status != "done" and metadata.is_manager and metadata.runat == TASK_TARGET)


def todo_has_exact_pending_row(text: str) -> bool:
    section = ""
    current_headers = 0
    previous_headers = 0
    matches: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1]
            current_headers += stripped == "current:"
            previous_headers += stripped == "previous:"
        elif stripped.split()[:1] == [TASK_NAME]:
            matches.append(f"{section}\0{stripped}")
    return current_headers == 1 and previous_headers == 1 and matches == [f"current\0{TASK_NAME} {TASK_TARGET}"]


def recovery_receipt(args: Args, incident_state: os.stat_result, items: tuple[str, ...]) -> bytes:
    value = {
        "schema": RECOVERY_SCHEMA,
        "root": str(args.root),
        "task": TASK_NAME,
        "task_sha256": args.task_sha256,
        "todo": TODO_NAME,
        "todo_sha256": args.todo_sha256,
        "protected_owner": OTHER_OWNER_NAME,
        "protected_owner_sha256": args.other_sha256,
        "owner": TASK_TARGET,
        "outcome": OUTCOME,
        "pending_task_items": list(items),
        "active_owner_names": sorted((TASK_NAME, OTHER_OWNER_NAME)),
        "incident_receipt_path": str(args.incident_path),
        "incident_receipt_sha256": args.incident_sha256,
        "incident_receipt_device": str(incident_state.st_dev),
        "incident_receipt_inode": str(incident_state.st_ino),
        "incident_receipt_size": str(incident_state.st_size),
        "incident_receipt_mtime_ns": str(incident_state.st_mtime_ns),
        "incident_receipt_mode": oct(stat.S_IMODE(incident_state.st_mode)),
        "incident_receipt_uid": str(incident_state.st_uid),
        "incident_receipt_gid": str(incident_state.st_gid),
        "incident_message_id": MESSAGE_ID,
        "recovery_receipt_path": str(args.output),
        "recovery_policy": "preserve-incident-receipt-no-reuse",
        "execution_authority": "not-contained-separate-explicit-production-approval-required",
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def validate_task(root: Path, payload: bytes, items: tuple[str, ...]) -> None:
    text = payload.decode()
    metadata = parse_task_metadata(text, root)
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != "owner-authenticated reconciliation of already delivered completion email; no resend"
        or metadata.runat != TASK_TARGET
        or metadata.managerat != TASK_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items != items
        or has_pending_marker(text)
    ):
        raise OSError("transcription task queue, target, type, status, or ownership drifted")


def write_private_exclusive(path: Path, payload: bytes) -> os.stat_result:
    if not path.is_absolute() or path.resolve() != path or path.name in {"", ".", ".."}:
        raise OSError("recovery receipt output must be an absolute canonical file")
    parent = path.parent
    state = parent.stat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise OSError("recovery receipt directory must be owner-private mode 0700")
    directory_descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_parent = os.fstat(directory_descriptor)
        if not same_file_state(state, opened_parent):
            raise OSError("recovery receipt directory changed before exclusive creation")
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_descriptor)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.fsync(directory_descriptor)
        descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_descriptor)
        try:
            created = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        bound = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (created.st_dev, created.st_ino):
            raise OSError("recovery receipt output changed inside its bound directory")
        current_parent = parent.stat()
        if (current_parent.st_dev, current_parent.st_ino) != (opened_parent.st_dev, opened_parent.st_ino):
            raise OSError("recovery receipt directory identity changed during exclusive creation")
    finally:
        os.close(directory_descriptor)
    if read_private_file(path, sha256(payload)) != payload:
        raise OSError("recovery receipt output identity changed after exclusive creation")
    return created


# 🧑 Human source `manager_mail/85c5dff58359-1270.txt:1-7`: "Find me a good transcription software ... supporting Mac OS and Linux."
def recover(args: Args) -> None:
    task = args.root / TASK_NAME
    other = args.root / OTHER_OWNER_NAME
    todo = args.root / TODO_NAME
    if args.output == args.incident_path or args.output.parent == args.incident_path.parent:
        raise OSError("recovery evidence must be mechanically separate from incident evidence")
    with root_membership_lock(args.root), task_target_lock(args.root, TASK_TARGET):
        paths = task_paths(args.root)
        if not {task, other, todo}.issubset(paths):
            raise OSError("required transcription lifecycle records are missing from work-log membership")
        with ExitStack() as locks:
            for path in paths:
                locks.enter_context(task_file_lock(path))
            incident_state = args.incident_path.stat()
            expected_incident_state = (
                args.incident_device,
                args.incident_inode,
                args.incident_mode,
                args.incident_uid,
                args.incident_gid,
                args.incident_size,
                args.incident_mtime_ns,
            )
            observed_incident_state = (
                incident_state.st_dev,
                incident_state.st_ino,
                stat.S_IMODE(incident_state.st_mode),
                incident_state.st_uid,
                incident_state.st_gid,
                incident_state.st_size,
                incident_state.st_mtime_ns,
            )
            if observed_incident_state != expected_incident_state:
                raise OSError("incident receipt identity drifted from the preparation packet")
            incident_payload = read_private_file(args.incident_path, args.incident_sha256)
            items = parse_receipt(incident_payload, args.root, args.task_sha256)
            task_payload = task.read_bytes()
            todo_payload = todo.read_bytes()
            other_payload = other.read_bytes()
            if (
                sha256(task_payload) != args.task_sha256
                or sha256(todo_payload) != args.todo_sha256
                or sha256(other_payload) != args.other_sha256
            ):
                raise OSError("task, TODO, or protected-owner bytes drifted from the recovery binding")
            validate_task(args.root, task_payload, items)
            if not todo_has_exact_pending_row(todo_payload.decode()):
                raise OSError("transcription TODO row did not bind a pending closure")
            if not other_owner_is_preserved(args.root, other_payload.decode()):
                raise OSError("protected shared-target manager ownership drifted")
            expected_owners = tuple(sorted((task.resolve(), other.resolve())))
            if active_target_owners(args.root, TASK_TARGET, paths) != expected_owners:
                raise OSError("shared target has missing, additional, or ambiguous active ownership")
            payload = recovery_receipt(args, incident_state, items)
            approval_state = args.approval_path.stat()
            approval_payload = read_approval(args.approval_path, args.approval_sha256)
            validate_approval(
                approval_payload,
                approved_packet_sha256=args.approved_packet_sha256,
                root=args.root,
                task_sha256=args.task_sha256,
                todo_sha256=args.todo_sha256,
                protected_owner_sha256=args.other_sha256,
                incident_receipt_path=args.incident_path,
                incident_receipt_sha256=args.incident_sha256,
                recovery_receipt_path=args.output,
                recovery_receipt_sha256=sha256(payload),
            )
            if (
                task_paths(args.root) != paths
                or task.read_bytes() != task_payload
                or todo.read_bytes() != todo_payload
                or other.read_bytes() != other_payload
                or read_private_file(args.incident_path, args.incident_sha256) != incident_payload
                or not same_file_state(incident_state, args.incident_path.stat())
                or read_approval(args.approval_path, args.approval_sha256) != approval_payload
                or not same_file_state(approval_state, args.approval_path.stat())
            ):
                raise OSError("incident evidence or production state changed during recovery preparation")
            output_state = write_private_exclusive(args.output, payload)
            if (
                task_paths(args.root) != paths
                or task.read_bytes() != task_payload
                or todo.read_bytes() != todo_payload
                or other.read_bytes() != other_payload
                or read_private_file(args.incident_path, args.incident_sha256) != incident_payload
                or not same_file_state(incident_state, args.incident_path.stat())
                or read_approval(args.approval_path, args.approval_sha256) != approval_payload
                or not same_file_state(approval_state, args.approval_path.stat())
                or read_private_file(args.output, sha256(payload)) != payload
                or not same_file_state(output_state, args.output.stat())
            ):
                raise OSError("incident evidence, production state, approval, or recovery output changed after preparation")


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--other-owner-sha256", required=True)
    _ = parser.add_argument("--incident-receipt", type=Path, required=True)
    _ = parser.add_argument("--incident-receipt-sha256", required=True)
    _ = parser.add_argument("--incident-device", type=int, required=True)
    _ = parser.add_argument("--incident-inode", type=int, required=True)
    _ = parser.add_argument("--incident-size", type=int, required=True)
    _ = parser.add_argument("--incident-mtime-ns", type=int, required=True)
    _ = parser.add_argument("--incident-mode", type=lambda value: int(value, 8), required=True)
    _ = parser.add_argument("--incident-uid", type=int, required=True)
    _ = parser.add_argument("--incident-gid", type=int, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    _ = parser.add_argument("--approved-packet-sha256", required=True)
    _ = parser.add_argument("--production-approval", type=Path, required=True)
    _ = parser.add_argument("--production-approval-sha256", required=True)
    parsed = parser.parse_args(argv)
    hashes = (
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.other_owner_sha256,
        parsed.incident_receipt_sha256,
        parsed.approved_packet_sha256,
        parsed.production_approval_sha256,
    )
    if any(len(value) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value) for value in hashes):
        parser.error("task, TODO, owner, and incident SHA-256 bindings must be lowercase hexadecimal")
    return Args(
        parsed.root.resolve(),
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.other_owner_sha256,
        parsed.incident_receipt,
        parsed.incident_receipt_sha256,
        parsed.incident_device,
        parsed.incident_inode,
        parsed.incident_size,
        parsed.incident_mtime_ns,
        parsed.incident_mode,
        parsed.incident_uid,
        parsed.incident_gid,
        parsed.output,
        parsed.approved_packet_sha256,
        parsed.production_approval,
        parsed.production_approval_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        recover(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, TaskFrontmatterError, UnicodeDecodeError, ValueError) as exc:
        print(f"omo_incident_receipt_recover.py: {exc}", file=sys.stderr)
        return 2
    print("Created separate recovery evidence; preserved unauthorized adoption receipt unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
