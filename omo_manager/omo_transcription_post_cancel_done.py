#!/usr/bin/env python3
"""Close the approved transcription task after its former shared owner is done."""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager import omo_shared_task_done as shared
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata

SHA256_RE = re.compile(r"[0-9a-f]{64}")
TASK_NAME = "transcription_sw.md"
MEMORY_NAME = "memory_research_mgr.md"
TODO_NAME = "TODO.md"
TARGET = "wl:32"
TASK_MANAGER = "wl:1"
MEMORY_MANAGER = "wl:30"
AUTHORITY_NAME = "manager_mail/85c5dff58359-1297.txt"
AUTHORITY_SHA256 = "e15382602b89ed477859459d1a315246d3a64a8f1e44bfe06228cd4c2b4bbce2"
ADOPTED_TASK_SHA256 = "f7714be6cc2cde7e9338251407e799967a0413b17b6a87f899bf1d40970cdd6b"
INITIAL_BLOCKER = "human: authorize supported separation of shared wl:32 ownership from memory_research_mgr.md; completion email already delivered and must not be resent"
DELIVERED_ITEMS = (
    "Find me a good transcription software by searching online using the multiple tools we have.",
    "The transcription software should give timeline, distinguish multiple speakers and be very accurate.",
    "It can be a command line tool, must be open source, and it could be a user graphics interface tool supporting Mac OS and Linux.",
)
CURRENT_ITEMS = (
    *DELIVERED_ITEMS,
    "Reconcile the already delivered completion email using owner-authenticated evidence. Do not resend any Human email. Verify delivery through the supported mail/delivery helpers, remove the completed pending items with exact evidence, and report privately. If complete, the manager will close the task through the supported lifecycle helper.",
)


@dataclass(frozen=True)
class FileBinding:
    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class Args:
    root: Path
    helper_sha256: str
    membership_sha256: str
    task_sha256: str
    todo_sha256: str
    memory_sha256: str
    adoption_path: Path
    adoption_sha256: str
    adoption_binding: FileBinding
    authority_path: Path
    authority_sha256: str
    authority_binding: FileBinding


def membership_sha256(paths: tuple[Path, ...]) -> str:
    return shared.sha256(b"\0".join(str(path).encode() for path in paths))


def same_binding(binding: FileBinding, state: os.stat_result) -> bool:
    return binding == FileBinding(
        state.st_dev,
        state.st_ino,
        state.st_size,
        state.st_mtime_ns,
        stat.S_IMODE(state.st_mode),
        state.st_uid,
        state.st_gid,
    )


def validate_authority(payload: bytes) -> None:
    try:
        lines = payload.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise OSError("Source-1297 authority is not UTF-8") from exc
    if len(lines) < 3 or lines[:3] != ["Subject: Re: Approve transcription task closure", "", "approve"]:
        raise OSError("Source-1297 does not contain the exact scoped approval")
    quoted = "\n".join(lines[3:])
    required = (
        "Please approve marking only the task “Find me a good transcription software” complete.",
        "It will not change any other task, send email, or rerun research.",
    )
    if any(value not in quoted for value in required):
        raise OSError("Source-1297 approval context is incomplete")


def validate_task(root: Path, text: str) -> None:
    metadata = parse_task_metadata(text, root)
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != INITIAL_BLOCKER
        or metadata.runat != TARGET
        or metadata.managerat != TASK_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items != CURRENT_ITEMS
        or shared.has_pending_marker(text)
    ):
        raise OSError("transcription task queue, target, type, status, blocker, or ownership drifted")


def validate_memory(root: Path, text: str) -> None:
    metadata = parse_task_metadata(text, root)
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "done"
        or metadata.blocked_on
        or metadata.runat != TARGET
        or metadata.managerat != MEMORY_MANAGER
        or not metadata.is_manager
        or metadata.pending_task_items
        or shared.has_pending_marker(text)
    ):
        raise OSError("done memory history, queue, target, type, or ownership drifted")


def todo_section(text: str, task_name: str) -> str:
    section = ""
    headers = {"current": 0, "previous": 0}
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1]
            if section in headers:
                headers[section] += 1
            continue
        fields = stripped.split()
        if fields and fields[0].strip("`") == task_name:
            if stripped != f"{task_name} {TARGET}":
                raise OSError(f"{task_name} TODO row does not bind {TARGET}")
            found.append(section)
    if headers != {"current": 1, "previous": 1} or len(found) != 1:
        raise OSError(f"{task_name} TODO custody is missing or ambiguous")
    return found[0]


def task_replacement(root: Path, text: str, adoption_sha256: str) -> str:
    validate_task(root, text)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise OSError("transcription task has no exact frontmatter boundary")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise OSError("transcription task has no closing frontmatter boundary") from exc
    updated = [lines[0]]
    found = {"status": 0, "blocked_on": 0, "queue": 0}
    index = 1
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
    newline = "\r\n" if "\r\n" in cleared else "\n"
    separator = "" if cleared.endswith(("\n", "\r")) else newline
    evidence = (
        f"authenticated Human Source-1297 {AUTHORITY_SHA256}; adopted authenticated Gmail Sent delivery "
        f"{shared.MESSAGE_ID}; adoption receipt sha256 {adoption_sha256}; no resend"
    )
    replacement = f"{cleared}{separator}(verified removed pending items: {evidence}){newline}"
    metadata = parse_task_metadata(replacement, root)
    if metadata is None or metadata.status != "done" or metadata.blocked_on or metadata.pending_task_items:
        raise OSError("transcription replacement is not exact done state")
    return replacement


def rollback_own_writes(task: Path, task_payload: bytes, updated_task: bytes, todo: Path, todo_payload: bytes, updated_todo: bytes) -> None:
    errors: list[str] = []
    task_state = task.stat()
    if task.read_bytes() == updated_task and shared.same_file_state(task_state, task.stat()):
        try:
            shared.replace_if_unchanged(task, task_payload, task_state)
        except OSError as exc:
            errors.append(f"task: {exc}")
    else:
        errors.append("task committed bytes drifted")
    todo_state = todo.stat()
    if todo.read_bytes() == updated_todo and shared.same_file_state(todo_state, todo.stat()):
        try:
            shared.replace_if_unchanged(todo, todo_payload, todo_state)
        except OSError as exc:
            errors.append(f"TODO: {exc}")
    else:
        errors.append("TODO committed bytes drifted")
    if errors:
        raise OSError(f"post-commit verification failed and rollback was incomplete: {'; '.join(errors)}")


# 🧑 Human source `manager_mail/85c5dff58359-1297.txt:3`: "approve"
def reconcile(args: Args) -> None:
    root = args.root
    task = root / TASK_NAME
    memory = root / MEMORY_NAME
    todo = root / TODO_NAME
    expected_authority = root / AUTHORITY_NAME
    helper_path = Path(__file__).resolve()
    if shared.sha256(helper_path.read_bytes()) != args.helper_sha256:
        raise OSError("closure helper bytes drifted from the reviewed binding")
    if args.authority_path != expected_authority or args.authority_sha256 != AUTHORITY_SHA256:
        raise OSError("closure requires the exact reviewed Source-1297 authority binding")
    with shared.root_membership_lock(root), task_target_lock(root, TARGET):
        paths = shared.task_paths(root)
        if not {task, memory, todo}.issubset(paths):
            raise OSError("required transcription lifecycle records are missing")
        if membership_sha256(paths) != args.membership_sha256:
            raise OSError("work-log Markdown membership drifted from the reviewed binding")
        with ExitStack() as locks:
            for path in paths:
                locks.enter_context(task_file_lock(path))
            authority_state = args.authority_path.stat()
            if not same_binding(args.authority_binding, authority_state):
                raise OSError("Source-1297 authority identity drifted from the reviewed binding")
            authority_payload = shared.read_private_file(args.authority_path, args.authority_sha256)
            validate_authority(authority_payload)
            adoption_state = args.adoption_path.stat()
            if not same_binding(args.adoption_binding, adoption_state):
                raise OSError("adoption evidence identity drifted from the reviewed binding")
            adoption_payload = shared.read_private_file(args.adoption_path, args.adoption_sha256)
            delivered_items = shared.parse_receipt(adoption_payload, root, ADOPTED_TASK_SHA256)
            if delivered_items != DELIVERED_ITEMS:
                raise OSError("adoption receipt does not bind the three completed research items")
            protected_payloads = {path: path.read_bytes() for path in paths if path not in {task, todo}}
            task_before = task.stat()
            task_payload = task.read_bytes()
            memory_payload = protected_payloads[memory]
            todo_before = todo.stat()
            todo_payload = todo.read_bytes()
            if (
                shared.sha256(task_payload) != args.task_sha256
                or shared.sha256(memory_payload) != args.memory_sha256
                or shared.sha256(todo_payload) != args.todo_sha256
            ):
                raise OSError("task, TODO, or done memory bytes drifted from the closure binding")
            task_text = task_payload.decode()
            memory_text = memory_payload.decode()
            todo_text = todo_payload.decode()
            validate_task(root, task_text)
            validate_memory(root, memory_text)
            if todo_section(todo_text, TASK_NAME) != "current" or todo_section(todo_text, MEMORY_NAME) != "previous":
                raise OSError("transcription or done memory TODO custody drifted")
            if shared.active_target_owners(root, TARGET, paths) != (task.resolve(),):
                raise OSError("transcription is not the sole active wl:32 owner")
            updated_task = task_replacement(root, task_text, args.adoption_sha256).encode()
            updated_todo = shared.todo_replacement(todo_text).encode()
            if todo_section(updated_todo.decode(), TASK_NAME) != "previous" or todo_section(updated_todo.decode(), MEMORY_NAME) != "previous":
                raise OSError("post-closure TODO custody is not exact previous history")
            if (
                shared.sha256(helper_path.read_bytes()) != args.helper_sha256
                or membership_sha256(shared.task_paths(root)) != args.membership_sha256
                or shared.read_private_file(args.authority_path, args.authority_sha256) != authority_payload
                or not shared.same_file_state(authority_state, args.authority_path.stat())
                or shared.read_private_file(args.adoption_path, args.adoption_sha256) != adoption_payload
                or not shared.same_file_state(adoption_state, args.adoption_path.stat())
                or task.read_bytes() != task_payload
                or todo.read_bytes() != todo_payload
                or any(path.read_bytes() != payload for path, payload in protected_payloads.items())
                or shared.task_paths(root) != paths
                or shared.active_target_owners(root, TARGET, paths) != (task.resolve(),)
            ):
                raise OSError("authority, delivery, task, TODO, memory, or ownership changed before closure")
            shared.finish_transaction(task, updated_task, task_before, todo, todo_payload, updated_todo, todo_before)
            try:
                if (
                    task.read_bytes() != updated_task
                    or todo.read_bytes() != updated_todo
                    or any(path.read_bytes() != payload for path, payload in protected_payloads.items())
                    or shared.sha256(helper_path.read_bytes()) != args.helper_sha256
                    or membership_sha256(shared.task_paths(root)) != args.membership_sha256
                    or shared.read_private_file(args.authority_path, args.authority_sha256) != authority_payload
                    or not shared.same_file_state(authority_state, args.authority_path.stat())
                    or shared.read_private_file(args.adoption_path, args.adoption_sha256) != adoption_payload
                    or not shared.same_file_state(adoption_state, args.adoption_path.stat())
                    or shared.task_paths(root) != paths
                    or shared.active_target_owners(root, TARGET, paths)
                ):
                    raise OSError("post-closure verification failed")
            except OSError:
                rollback_own_writes(task, task_payload, updated_task, todo, todo_payload, updated_todo)
                raise


def file_binding(parsed: argparse.Namespace, prefix: str) -> FileBinding:
    return FileBinding(*(getattr(parsed, f"{prefix}_{field}") for field in ("device", "inode", "size", "mtime_ns", "mode", "uid", "gid")))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--helper-sha256", required=True)
    parser.add_argument("--membership-sha256", required=True)
    parser.add_argument("--task-sha256", required=True)
    parser.add_argument("--todo-sha256", required=True)
    parser.add_argument("--memory-sha256", required=True)
    parser.add_argument("--adoption-receipt", type=Path, required=True)
    parser.add_argument("--adoption-receipt-sha256", required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    parser.add_argument("--authority-sha256", required=True)
    for prefix in ("adoption", "authority"):
        for field in ("device", "inode", "size", "mtime-ns", "mode", "uid", "gid"):
            parser.add_argument(f"--{prefix}-{field}", type=int, required=True)
    parsed = parser.parse_args(argv)
    digests = (parsed.helper_sha256, parsed.membership_sha256, parsed.task_sha256, parsed.todo_sha256, parsed.memory_sha256, parsed.adoption_receipt_sha256, parsed.authority_sha256)
    if any(SHA256_RE.fullmatch(value) is None for value in digests):
        parser.error("all SHA-256 bindings must be lowercase hexadecimal")
    root = parsed.root.expanduser().resolve()
    adoption = parsed.adoption_receipt.expanduser().resolve()
    authority = parsed.authority_file.expanduser().resolve()
    return Args(
        root,
        parsed.helper_sha256,
        parsed.membership_sha256,
        parsed.task_sha256,
        parsed.todo_sha256,
        parsed.memory_sha256,
        adoption,
        parsed.adoption_receipt_sha256,
        file_binding(parsed, "adoption"),
        authority,
        parsed.authority_sha256,
        file_binding(parsed, "authority"),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        reconcile(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, UnicodeDecodeError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_transcription_post_cancel_done: {exc}", file=sys.stderr)
        return 2
    print("Closed only transcription_sw.md from authenticated Source-1297; no mail or tmux action occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
