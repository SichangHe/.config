#!/usr/bin/env python3
"""Close one completed blocked task while preserving its distinct shared-target owner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata

SHA256_RE = re.compile(r"[0-9a-f]{64}")
# 🧑 Human source `manager_mail/85c5dff58359-1270.txt:1-7`: "Find me a good transcription software ... supporting Mac OS and Linux."
SCHEMA = "omo-completion-mail-adoption/v1"
TASK_NAME = "transcription_sw.md"
TASK_TARGET = "wl:32"
MESSAGE_ID = "<178815460436.2815805.14149274743602497510@gmail.com>"
OUTCOME = "task done"
OTHER_OWNER_NAME = "memory_research_mgr.md"
TASK_MANAGER = "wl:1"
INITIAL_BLOCKER = "owner-authenticated reconciliation of already delivered completion email; no resend"
TODO_NAME = "TODO.md"
THREAD_ROOT_MESSAGE_ID = "<178815432253.2784108.9648480549229137852@gmail.com>"
SUBJECT = "Re: [wl:32] transcription software"
ALL_MAIL_UID = "6749"
ALL_MAIL_UIDVALIDITY = "12"
SENT_MAIL_UID = "5479"
SENT_MAIL_UIDVALIDITY = "5"
GMAIL_MESSAGE_ID = "1875016003461706226"
GMAIL_THREAD_ID = "1875015707857181726"
INTERNALDATE_UNIX_MS = "1788154605000"
RAW_SHA256 = "60851e043fee1905496fd48ddff6d6d10a2e175e4975ebcc3f236b05a64b3eac"
BODY_SHA256 = "6cb31298f73031c28da1c35613c4daf59aa6ce06c3803cdb9f4bb5c7763f911e"
RECEIPT_FIELDS = {
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


def root_membership_lock(root: Path):
    return task_file_lock(root / ".omo-task-membership.lock")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_target(value: str) -> str:
    match = re.fullmatch(r"([^:\s]+):(\d+)(?:\.(\d+))?", value)
    if match is None:
        return value
    session, window, pane = match.groups()
    result = f"{session}:{int(window)}"
    return result if pane is None or int(pane) == 0 else f"{result}.{int(pane)}"


def same_target(left: str, right: str) -> bool:
    return canonical_target(left) == canonical_target(right)


def read_private_file(path: Path, expected_sha256: str) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        raise OSError("adoption receipt must be an absolute canonical path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise OSError("adoption receipt must be an owner-private regular file")
        payload = b""
        while chunk := os.read(fd, min(65_536, 65_537 - len(payload))):
            payload += chunk
            if len(payload) > 65_536:
                break
        after = os.fstat(fd)
    finally:
        os.close(fd)
    bound = path.lstat()
    if len(payload) > 65_536:
        raise OSError("adoption receipt is oversized")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        bound.st_dev,
        bound.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise OSError("adoption receipt changed while read")
    if sha256(payload) != expected_sha256:
        raise OSError("adoption receipt bytes do not match --adoption-receipt-sha256")
    return payload


def parse_receipt(payload: bytes, root: Path, task_sha256: str) -> tuple[str, ...]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate receipt field: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OSError("adoption receipt is not unambiguous UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        raise OSError("adoption receipt has an incomplete or unknown schema")
    items = value["pending_task_items"]
    thread_ids = value["thread_message_ids"]
    if (
        value["schema"] != SCHEMA
        or value["root"] != str(root)
        or value["task"] != TASK_NAME
        or value["task_sha256"] != task_sha256
        or value["owner"] != TASK_TARGET
        or value["outcome"] != OUTCOME
        or value["mail_policy"] != "already-delivered-no-resend"
        or value["message_id"] != MESSAGE_ID
        or value["thread_root_message_id"] != THREAD_ROOT_MESSAGE_ID
        or value["subject"] != SUBJECT
        or value["provider"] != "gmail-agent-sent"
        or value["all_mail_uid"] != ALL_MAIL_UID
        or value["all_mail_uidvalidity"] != ALL_MAIL_UIDVALIDITY
        or value["sent_mail_uid"] != SENT_MAIL_UID
        or value["sent_mail_uidvalidity"] != SENT_MAIL_UIDVALIDITY
        or value["gmail_message_id"] != GMAIL_MESSAGE_ID
        or value["gmail_thread_id"] != GMAIL_THREAD_ID
        or value["internaldate_unix_ms"] != INTERNALDATE_UNIX_MS
        or value["raw_sha256"] != RAW_SHA256
        or value["body_sha256"] != BODY_SHA256
        or not isinstance(items, list)
        or not items
        or any(not isinstance(item, str) or not item for item in items)
        or not isinstance(thread_ids, list)
        or thread_ids != [THREAD_ROOT_MESSAGE_ID, MESSAGE_ID]
    ):
        raise OSError("adoption receipt does not authorize this exact no-resend closure")
    scalar_fields = RECEIPT_FIELDS - {"pending_task_items", "thread_message_ids"}
    if any(not isinstance(value[field], str) or not value[field] for field in scalar_fields):
        raise OSError("adoption receipt scalar bindings are incomplete")
    return tuple(items)


def possibly_claims_target(text: str, target: str) -> bool:
    canonical = target.partition(".")[0]
    session, _separator, window = canonical.partition(":")
    if not session or not window.isdecimal():
        return False
    pattern = rf"(?mi)^[ \t]*runat[ \t]*:[ \t]*(?:![^ \t\r\n]+[ \t]+)?(?:['\"])?{re.escape(session)}:0*{int(window)}(?:\.0+)?(?:['\"])?[ \t]*(?:#.*)?$"
    return re.search(pattern, text) is not None


def has_pending_marker(text: str) -> bool:
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and stripped == "(pending)":
            return True
    return False


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
        if metadata is not None and metadata.status != "done" and same_target(metadata.runat, target):
            owners.append(path)
    return tuple(sorted(owners))


def task_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for candidate in root.rglob("*.md"):
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(f"work-log membership contains an unsafe Markdown entry: {candidate}")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise OSError(f"work-log membership escapes the root: {candidate}") from exc
        paths.append(resolved)
    if len(paths) != len(set(paths)):
        raise OSError("work-log Markdown membership contains aliases")
    return tuple(sorted(paths))


def task_replacement(root: Path, text: str, items: tuple[str, ...], receipt_sha256: str) -> str:
    metadata = parse_task_metadata(text, root)
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != INITIAL_BLOCKER
        or metadata.runat != TASK_TARGET
        or metadata.managerat != TASK_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items != items
        or has_pending_marker(text)
    ):
        raise OSError("transcription task queue, target, type, status, or ownership drifted")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise OSError("transcription task has no exact frontmatter boundary")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise OSError("transcription task has no closing frontmatter boundary") from exc
    updated: list[str] = [lines[0]]
    index = 1
    found = {"status": 0, "blocked_on": 0, "queue": 0}
    while index < end:
        line = lines[index]
        key, separator, _value = line.rstrip("\r\n").partition(":")
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        if separator and key == "status":
            found["status"] += 1
            updated.append(f"status: done{newline}")
        elif separator and key == "blocked_on":
            found["blocked_on"] += 1
        elif separator and key == "pending_task_items":
            found["queue"] += 1
            updated.append(f"pending_task_items: []{newline}")
            index += 1
            while index < end and lines[index].startswith((" ", "\t")):
                index += 1
            continue
        else:
            updated.append(line)
        index += 1
    if found != {"status": 1, "blocked_on": 1, "queue": 1}:
        raise OSError("transcription lifecycle fields are missing or ambiguous")
    updated.extend(lines[end:])
    cleared = "".join(updated)
    evidence = f"adopted authenticated Gmail Sent delivery {MESSAGE_ID}; receipt sha256 {receipt_sha256}; no resend"
    newline = "\r\n" if "\r\n" in cleared else "\n"
    separator = "" if cleared.endswith(("\n", "\r")) else newline
    replacement = f"{cleared}{separator}(verified removed pending items: {evidence}){newline}"
    updated_metadata = parse_task_metadata(replacement, root)
    if updated_metadata is None or updated_metadata.status != "done" or updated_metadata.blocked_on or updated_metadata.pending_task_items:
        raise OSError("transcription task replacement is not exact done state")
    return replacement


def todo_replacement(text: str) -> str:
    lines = text.splitlines(keepends=True)
    section = ""
    current_headers: list[int] = []
    previous_headers: list[int] = []
    rows: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1]
            if stripped == "current:":
                current_headers.append(index)
            elif stripped == "previous:":
                previous_headers.append(index)
        else:
            fields = stripped.split()
            if fields and fields[0].strip("`") == TASK_NAME:
                rows.append((index, section))
    if len(current_headers) != 1 or len(previous_headers) != 1 or len(rows) != 1 or rows[0][1] != "current":
        raise OSError("transcription TODO row or lifecycle sections are missing or ambiguous")
    source_index = rows[0][0]
    if lines[source_index].strip() != f"{TASK_NAME} {TASK_TARGET}":
        raise OSError("transcription TODO row does not bind the exact shared target")
    moved = lines.pop(source_index)
    destination = next(index for index, line in enumerate(lines) if line.strip() == "previous:") + 1
    while destination < len(lines) and not lines[destination].strip():
        destination += 1
    lines.insert(destination, moved)
    return "".join(lines)


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns)


def replace_if_unchanged(path: Path, payload: bytes, before: os.stat_result) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(before.st_mode & 0o7777)
        if not same_file_state(before, path.stat()):
            raise OSError(f"{path.name} changed before atomic replacement")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def finish_transaction(
    task: Path,
    updated_task: bytes,
    task_before: os.stat_result,
    todo: Path,
    todo_payload: bytes,
    updated_todo: bytes,
    todo_before: os.stat_result,
) -> None:
    replace_if_unchanged(todo, updated_todo, todo_before)
    moved_todo_before = todo.stat()
    try:
        replace_if_unchanged(task, updated_task, task_before)
    except Exception as exc:
        try:
            replace_if_unchanged(todo, todo_payload, moved_todo_before)
        except Exception as rollback_exc:
            raise OSError(f"task replacement failed and TODO rollback also failed: {rollback_exc}") from exc
        raise


def other_owner_is_preserved(root: Path, text: str) -> bool:
    metadata = parse_task_metadata(text, root)
    return bool(
        metadata is not None
        and metadata.version == "v1.0.0"
        and metadata.status != "done"
        and metadata.is_manager
        and metadata.runat == TASK_TARGET
    )


def reconcile(root: Path, task_sha256: str, todo_sha256: str, other_sha256: str, receipt_path: Path, receipt_sha256: str) -> None:
    task = root / TASK_NAME
    other = root / OTHER_OWNER_NAME
    todo = root / TODO_NAME
    with root_membership_lock(root), task_target_lock(root, TASK_TARGET):
        paths = task_paths(root)
        if not {task, other, todo}.issubset(paths):
            raise OSError("required transcription lifecycle records are missing from work-log membership")
        with ExitStack() as locks:
            for path in paths:
                locks.enter_context(task_file_lock(path))
            receipt_payload = read_private_file(receipt_path, receipt_sha256)
            items = parse_receipt(receipt_payload, root, task_sha256)
            task_before = task.stat()
            task_payload = task.read_bytes()
            other_payload = other.read_bytes()
            todo_before = todo.stat()
            todo_payload = todo.read_bytes()
            if sha256(task_payload) != task_sha256 or sha256(other_payload) != other_sha256 or sha256(todo_payload) != todo_sha256:
                raise OSError("task, TODO, or distinct-owner bytes drifted from the complete binding")
            if not other_owner_is_preserved(root, other_payload.decode()):
                raise OSError("distinct active shared-target manager ownership drifted")
            expected_owners = tuple(sorted((task.resolve(), other.resolve())))
            if active_target_owners(root, TASK_TARGET, paths) != expected_owners:
                raise OSError("shared target has missing, additional, or ambiguous active ownership")
            task_text = task_payload.decode()
            todo_text = todo_payload.decode()
            updated_task = task_replacement(root, task_text, items, receipt_sha256)
            updated_todo = todo_replacement(todo_text)
            if updated_todo == todo_text:
                raise OSError("transcription TODO row did not move from current to previous")
            if read_private_file(receipt_path, receipt_sha256) != receipt_payload:
                raise OSError("adoption receipt changed before lifecycle mutation")
            if task_paths(root) != paths or other.read_bytes() != other_payload or todo.read_bytes() != todo_payload or task.read_bytes() != task_payload:
                raise OSError("task, TODO, or distinct owner changed before lifecycle mutation")
            finish_transaction(task, updated_task.encode(), task_before, todo, todo_payload, updated_todo.encode(), todo_before)
            try:
                if task.read_text(encoding="utf-8") != updated_task or todo.read_text(encoding="utf-8") != updated_todo or other.read_bytes() != other_payload:
                    raise OSError("shared-target closure did not preserve its exact committed result")
                remaining = active_target_owners(root, TASK_TARGET, paths)
                if task_paths(root) != paths or remaining != (other.resolve(),):
                    raise OSError("shared-target closure changed or failed to preserve the distinct active owner")
            except Exception as exc:
                rollback_errors: list[str] = []
                if task.read_bytes() != updated_task.encode():
                    rollback_errors.append("task: committed bytes drifted")
                else:
                    try:
                        replace_if_unchanged(task, task_payload, task.stat())
                    except Exception as rollback_exc:
                        rollback_errors.append(f"task: {rollback_exc}")
                if todo.read_bytes() != updated_todo.encode():
                    rollback_errors.append("TODO: committed bytes drifted")
                else:
                    try:
                        replace_if_unchanged(todo, todo_payload, todo.stat())
                    except Exception as rollback_exc:
                        rollback_errors.append(f"TODO: {rollback_exc}")
                if rollback_errors:
                    raise OSError(f"post-commit verification failed and rollback failed: {'; '.join(rollback_errors)}") from exc
                raise


def parse_args(argv: list[str]) -> tuple[Path, str, str, str, Path, str]:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--other-owner-sha256", required=True)
    _ = parser.add_argument("--adoption-receipt", type=Path, required=True)
    _ = parser.add_argument("--adoption-receipt-sha256", required=True)
    parsed = parser.parse_args(argv)
    hashes = (parsed.task_sha256, parsed.todo_sha256, parsed.other_owner_sha256, parsed.adoption_receipt_sha256)
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        parser.error("task, TODO, owner, and adoption-receipt SHA-256 bindings must be lowercase hexadecimal")
    return parsed.root.resolve(), parsed.task_sha256, parsed.todo_sha256, parsed.other_owner_sha256, parsed.adoption_receipt, parsed.adoption_receipt_sha256


def main(argv: list[str] | None = None) -> int:
    try:
        reconcile(*parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, TaskFrontmatterError, UnicodeDecodeError, ValueError) as exc:
        print(f"omo_shared_task_done.py: {exc}", file=sys.stderr)
        return 2
    print("Closed transcription_sw.md without pane access; preserved memory_research_mgr.md as the sole active wl:32 owner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
