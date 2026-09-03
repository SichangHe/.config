#!/usr/bin/env python3
"""Safely edit task-file metadata, pending items, and comments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import TaskMetadata
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import load_task
from omo_manager.omo_blocking import task_paths
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_blocking_actor import request as blocking_request
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_status import parse_manager_child_metadata
from omo_manager.omo_task_status import relative_task_ref
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import root_membership_lock
from omo_manager.omo_task_status import same_file_state
from omo_manager.omo_task_status import task_path
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1
from omo_manager.omo_task_metadata import frontmatter_parts

PENDING_MARKER = "(pending)"
REMOVE_REMINDER = "Verify the removed pending item was actually done or cancelled; consider evaluator agents for uncertain verification."
EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
CLEAR_KINDS = {"cancelled", "duplicate", "existing-owner-item", "report-only", "superseded"}
EMAIL_SOURCE_PREFIXES = ("(record and delegate ", "(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
MANAGER_SOURCE_PREFIXES = ("(from manager ",)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AMH_SHUTDOWN_AUTHORITY = "tell those agents to document anything worth keeping long-term, move out their pending task items, then close them all"
AMH_SHUTDOWN_AUTHORITY_PATH_RE = re.compile(r"^(?:[0-9]{6}/)?manager_mail/[^/\r\n]+\.txt$")

COMMAND_ALIASES = {
    "list": "pending-list",
    "add": "pending-add",
    "replace": "pending-replace",
    "update": "pending-replace",
    "remove": "pending-remove",
    "comment": "comment-add",
}


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path | None
    command: str
    items: tuple[str, ...] = ()
    old_item: str = ""
    new_item: str = ""
    comment: str = ""
    evidence: str = ""
    line: int = 0
    ack_human: bool = False
    email_file: Path | None = None
    source_file: Path | None = None
    target_file: Path | None = None
    message_file: Path | None = None
    clear_kind: str = ""
    owner_task_file: Path | None = None
    owner_item: str = ""
    item_id: str = ""
    on_task: Path | None = None
    on_item_id: str = ""
    task_files: tuple[Path, ...] = ()
    source_ref: str = ""
    preserve_live_source: bool = False
    source_sha256: str = ""
    destination_sha256: str = ""
    authority_file: Path | None = None
    authority_sha256: str = ""


@dataclass(frozen=True)
class PendingListBounds:
    field_idx: int
    list_end: int


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    command: str
    task_file: Path | None = None
    item: list[str] | str | None = None
    old_item: str = ""
    new_item: str = ""
    comment: str = ""
    evidence: str = ""
    message: str | None = None
    legacy_message: str | None = None
    line: int = 0
    ack_human: bool = False
    email_file: Path | None = None
    from_file: Path | None = None
    to_file: Path | None = None
    message_file: Path | None = None
    clear_kind: str = ""
    owner_task_file: Path | None = None
    owner_item: str = ""
    item_id: str = ""
    on_task: Path | None = None
    on_item_id: str = ""
    source_ref: str = ""
    preserve_live_source: bool = False
    source_sha256: str = ""
    destination_sha256: str = ""
    authority_file: Path | None = None
    authority_sha256: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("summary", help="Print task path, frontmatter summary, and pending_task_items.")
    summary_parser.set_defaults(command="summary")
    _ = summary_parser.add_argument("task_file", type=Path, nargs="+")

    list_parser = subparsers.add_parser("pending-list", aliases=["list"], help="Print pending_task_items, one item per line.")
    list_parser.set_defaults(command="pending-list")
    _ = list_parser.add_argument("task_file", type=Path)

    add_parser = subparsers.add_parser("pending-add", aliases=["add"], help="Append one or more pending_task_items.")
    add_parser.set_defaults(command="pending-add")
    _ = add_parser.add_argument("task_file", type=Path)
    _ = add_parser.add_argument("--item", action="append", required=True, help="Pending task item to add. Pass once per item.")

    replace_parser = subparsers.add_parser("pending-replace", aliases=["replace", "update"], help="Replace one exact pending_task_item.")
    replace_parser.set_defaults(command="pending-replace")
    _ = replace_parser.add_argument("task_file", type=Path)
    _ = replace_parser.add_argument("--old-item", required=True, help="Existing pending task item text.")
    _ = replace_parser.add_argument("--new-item", required=True, help="Replacement pending task item text.")

    remove_parser = subparsers.add_parser("pending-remove", aliases=["remove"], help="Remove one or more exact pending_task_items.")
    remove_parser.set_defaults(command="pending-remove")
    _ = remove_parser.add_argument("task_file", type=Path)
    _ = remove_parser.add_argument("--item", action="append", required=True, help="Pending task item to remove. Pass once per item.")
    _ = remove_parser.add_argument("--evidence", required=True, help="One-line evidence that the removed item is complete or cancelled.")

    move_parser = subparsers.add_parser(
        "pending-move",
        help="Move one pending_task_item from one task file to another.",
        description="Atomically transfer one still-open item to its initial owner. Use only for initial routing.",
    )
    move_parser.set_defaults(command="pending-move")
    _ = move_parser.add_argument("--from", dest="from_file", type=Path, required=True, help="Source task file containing the pending item.")
    _ = move_parser.add_argument("--to", dest="to_file", type=Path, required=True, help="Destination task file that should receive the pending item.")
    _ = move_parser.add_argument("--item", required=True, help="Pending task item to move.")

    closure_transfer_parser = subparsers.add_parser(
        "pending-closure-transfer",
        help="Transfer an AMH task's complete pending queue to a surviving non-AMH manager before closure.",
        description="Closure-only atomic transfer of one AMH task's complete ordered pending queue and custody.",
    )
    closure_transfer_parser.set_defaults(command="pending-closure-transfer")
    _ = closure_transfer_parser.add_argument("--from", dest="from_file", type=Path, required=True, help="Active AMH source task file to drain before closure.")
    _ = closure_transfer_parser.add_argument("--to", dest="to_file", type=Path, required=True, help="Active surviving non-AMH manager task file that assumes custody.")
    _ = closure_transfer_parser.add_argument("--source-sha256", required=True, help="Lowercase SHA-256 of the exact source task bytes.")
    _ = closure_transfer_parser.add_argument("--destination-sha256", required=True, help="Lowercase SHA-256 of the exact destination task bytes.")
    _ = closure_transfer_parser.add_argument("--authority-file", type=Path, required=True, help="Owner-private manager_mail source containing the exact authoritative Human shutdown instruction.")
    _ = closure_transfer_parser.add_argument("--authority-sha256", required=True, help="Lowercase SHA-256 of the exact Human-authority file bytes.")

    marker_clear_parser = subparsers.add_parser(
        "pending-marker-clear",
        help="Remove one pending marker without adding pending_task_items.",
        description="Clear a consumed `(pending)` marker when no new pending task item should be added.",
    )
    marker_clear_parser.set_defaults(command="pending-marker-clear")
    _ = marker_clear_parser.add_argument("task_file", type=Path)
    _ = marker_clear_parser.add_argument("--line", type=int, required=True, help="One-based line number whose stripped content is `(pending)`.")
    _ = marker_clear_parser.add_argument("--comment", required=True, help="One-line parenthesized comment evidence explaining why no item was added.")
    _ = marker_clear_parser.add_argument("--ack-human", action="store_true", help="Email the human that no pending item was added.")
    _ = marker_clear_parser.add_argument("--email-file", type=Path, help="Stored `manager_mail/*.txt` file whose `Subject:` header should be used for the human acknowledgement.")
    _ = marker_clear_parser.add_argument("--clear-kind", choices=sorted(CLEAR_KINDS), help="Semantic reason required for human-origin markers.")
    _ = marker_clear_parser.add_argument("--owner-task-file", type=Path, help="Active owner task file containing --owner-item; required for --clear-kind existing-owner-item.")
    _ = marker_clear_parser.add_argument("--owner-item", help="Exact existing pending item already tracking this request.")

    source_dedupe_parser = subparsers.add_parser(
        "source-pointer-dedupe",
        help="Remove repeated bare human-source pointers after their request is already closed.",
        description="Removes only exact bare `(record and delegate manager_mail/*.txt)` lines; live `(pending)` blocks are refused.",
    )
    source_dedupe_parser.set_defaults(command="source-pointer-dedupe")
    _ = source_dedupe_parser.add_argument("task_file", type=Path)
    _ = source_dedupe_parser.add_argument("--source-ref", required=True, help="Exact manager_mail/*.txt reference to remove.")
    _ = source_dedupe_parser.add_argument("--evidence", required=True, help="One-line evidence that the referenced request is already complete or cancelled.")
    _ = source_dedupe_parser.add_argument("--preserve-live-source", action="store_true", help="Leave an exact source pointer in a live `(pending)` block intact; requires an active matching queue item.")

    comment_parser = subparsers.add_parser("comment-add", aliases=["comment"], help="Append a parenthesized comment line to a task file.")
    comment_parser.set_defaults(command="comment-add")
    _ = comment_parser.add_argument("task_file", type=Path)
    _ = comment_parser.add_argument("legacy_message", nargs="?", help="Compatibility positional comment text.")
    _ = comment_parser.add_argument("--message", help="One-line comment text to append.")

    delegate_parser = subparsers.add_parser(
        "delegate-message",
        help="Append a pending message block to a worker task file.",
        description="Append a manager-owned worker message for delivery by omo_pending_watch.py.",
    )
    delegate_parser.set_defaults(command="delegate-message")
    _ = delegate_parser.add_argument("task_file", type=Path)
    _ = delegate_parser.add_argument("--message-file", type=Path, required=True, help="File containing the worker message body.")

    dependency_add = subparsers.add_parser("dependency-add", help="Add an item dependency owned by a direct child task.")
    dependency_add.set_defaults(command="dependency-add")
    _ = dependency_add.add_argument("--task", dest="task_file", type=Path, required=True)
    _ = dependency_add.add_argument("--item-id", required=True)
    _ = dependency_add.add_argument("--on-task", type=Path, required=True)
    _ = dependency_add.add_argument("--on-item-id", required=True)

    dependency_remove = subparsers.add_parser("dependency-remove", help="Remove an item dependency owned by a direct child task.")
    dependency_remove.set_defaults(command="dependency-remove")
    _ = dependency_remove.add_argument("--task", dest="task_file", type=Path, required=True)
    _ = dependency_remove.add_argument("--item-id", required=True)
    _ = dependency_remove.add_argument("--on-task", type=Path, required=True)
    _ = dependency_remove.add_argument("--on-item-id", required=True)
    _ = dependency_remove.add_argument("--evidence", required=True)

    normalize_parser = subparsers.add_parser(
        "frontmatter-normalize",
        help="Replace one empty later duplicate frontmatter block with a Markdown separator.",
    )
    normalize_parser.set_defaults(command="frontmatter-normalize")
    _ = normalize_parser.add_argument("task_file", type=Path)
    _ = normalize_parser.add_argument("--line", type=int, required=True, help="One-based line number of the duplicate opening marker.")

    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    try:
        root = parsed.root.resolve()
        command = canonical_command(parsed.command)
        if command == "frontmatter-normalize":
            if parsed.line < 2:
                parser.error("--line must identify a later frontmatter block.")
            return Args(root, parsed.task_file, command, line=parsed.line)
        if command == "summary":
            task_files = tuple(parsed.task_file)
            return Args(root, task_files[0], command, task_files=task_files)
        if command in {"dependency-add", "dependency-remove"}:
            return Args(
                root,
                parsed.task_file,
                command,
                evidence=normalized_comment_message(parsed.evidence) if command == "dependency-remove" else "",
                item_id=parsed.item_id,
                on_task=parsed.on_task,
                on_item_id=parsed.on_item_id,
            )
        if command == "pending-add":
            items = normalized_items(tuple(parsed.item or ()))
            return Args(root, parsed.task_file, command, items=items)
        if command == "pending-replace":
            return Args(root, parsed.task_file, command, old_item=normalized_item(parsed.old_item), new_item=normalized_item(parsed.new_item))
        if command == "pending-remove":
            items = normalized_items(tuple(parsed.item or ()))
            return Args(root, parsed.task_file, command, items=items, evidence=normalized_comment_message(parsed.evidence))
        if command == "pending-move":
            if parsed.from_file is None or parsed.to_file is None:
                parser.error("pending-move requires --from and --to.")
            if not isinstance(parsed.item, str):
                parser.error("pending-move requires --item.")
            return Args(root, None, command, items=(normalized_item(parsed.item),), source_file=parsed.from_file, target_file=parsed.to_file)
        if command == "pending-closure-transfer":
            if parsed.from_file is None or parsed.to_file is None:
                parser.error("pending-closure-transfer requires --from and --to.")
            if (
                SHA256_RE.fullmatch(parsed.source_sha256) is None
                or SHA256_RE.fullmatch(parsed.destination_sha256) is None
                or parsed.authority_file is None
                or SHA256_RE.fullmatch(parsed.authority_sha256) is None
            ):
                parser.error("pending-closure-transfer requires lowercase SHA-256 source, destination, and Human-authority digests.")
            return Args(
                root,
                None,
                command,
                source_file=parsed.from_file,
                target_file=parsed.to_file,
                source_sha256=parsed.source_sha256,
                destination_sha256=parsed.destination_sha256,
                authority_file=parsed.authority_file,
                authority_sha256=parsed.authority_sha256,
            )
        if command == "pending-marker-clear":
            if parsed.line < 1:
                parser.error("--line must be positive.")
            if parsed.ack_human and not parsed.clear_kind:
                parser.error("--clear-kind is required with --ack-human.")
            if parsed.clear_kind == "existing-owner-item":
                if parsed.owner_task_file is None or not parsed.owner_item:
                    parser.error("--clear-kind existing-owner-item requires --owner-task-file and --owner-item.")
                return Args(
                    root,
                    parsed.task_file,
                    command,
                    comment=normalized_comment_message(parsed.comment),
                    line=parsed.line,
                    ack_human=parsed.ack_human,
                    email_file=parsed.email_file,
                    clear_kind=parsed.clear_kind,
                    owner_task_file=parsed.owner_task_file,
                    owner_item=normalized_item(parsed.owner_item),
                )
            if parsed.owner_task_file is not None or parsed.owner_item:
                parser.error("--owner-task-file and --owner-item are only valid with --clear-kind existing-owner-item.")
            return Args(root, parsed.task_file, command, comment=normalized_comment_message(parsed.comment), line=parsed.line, ack_human=parsed.ack_human, email_file=parsed.email_file, clear_kind=parsed.clear_kind)
        if command == "source-pointer-dedupe":
            return Args(
                root,
                parsed.task_file,
                command,
                evidence=normalized_comment_message(parsed.evidence),
                source_ref=normalized_source_ref(parsed.source_ref),
                preserve_live_source=parsed.preserve_live_source,
            )
        if command == "comment-add":
            message = parsed.message if parsed.message is not None else parsed.legacy_message
            if message is None:
                parser.error("comment-add requires --message.")
            return Args(root, parsed.task_file, command, comment=normalized_comment_message(message))
        if command == "delegate-message":
            return Args(root, parsed.task_file, command, message_file=parsed.message_file)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return Args(root, parsed.task_file, command)


def canonical_command(command: str) -> str:
    return COMMAND_ALIASES.get(command, command)


def normalized_source_ref(value: str) -> str:
    ref = value.strip()
    if not ref.startswith("manager_mail/") or not ref.endswith(".txt") or any(character.isspace() for character in ref):
        raise argparse.ArgumentTypeError("--source-ref must be an exact manager_mail/*.txt reference.")
    return ref


def normalized_item(item: str) -> str:
    if "\n" in item or "\r" in item:
        raise argparse.ArgumentTypeError("pending task item must be one line.")
    value = item.strip()
    if not value:
        raise argparse.ArgumentTypeError("pending task item must not be empty.")
    return value


def normalized_items(items: tuple[str, ...]) -> tuple[str, ...]:
    if not items:
        raise argparse.ArgumentTypeError("at least one pending task item is required.")
    return tuple(normalized_item(item) for item in items)


def normalized_comment_message(comment: str) -> str:
    if "\n" in comment or "\r" in comment:
        raise argparse.ArgumentTypeError("comment must be one line.")
    value = comment.strip()
    if not value:
        raise argparse.ArgumentTypeError("comment must not be empty.")
    if value.startswith("(") and value.endswith(")"):
        if not value[1:-1].strip():
            raise argparse.ArgumentTypeError("comment must not be empty.")
    return value


def normalized_comment(comment: str) -> str:
    value = normalized_comment_message(comment)
    if value.casefold() == "pending":
        raise argparse.ArgumentTypeError("comment must not create a live `(pending)` marker.")
    if value.startswith("(") and value.endswith(")"):
        return f"(manager note: {value})"
    return f"({value})"


def normalized_message_file(path: Path) -> Path:
    value = path.expanduser()
    if not value.is_absolute():
        value = Path.cwd() / value
    return value.resolve(strict=False)


def message_file_text(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    if not value.strip():
        raise TaskFrontmatterError("message file must not be empty.")
    return value


def subject_from_email_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep and key.casefold() == "subject":
            subject = value.strip()
            if subject:
                return subject
            break
    raise TaskFrontmatterError("email file has no nonempty `Subject:` header.")


def marker_clear_ack_subject(email_path: Path | None) -> str:
    if email_path is not None:
        return subject_from_email_file(email_path)
    return "Request acknowledged"


def marker_clear_ack_body(comment: str, clear_kind: str = "") -> str:
    classification = f"Classification: {clear_kind}\n" if clear_kind else ""
    return f"No pending item was added.\n{classification}Reason: {comment}\n"


def send_marker_clear_ack(comment: str, email_path: Path | None, clear_kind: str = "") -> None:
    with tempfile.TemporaryDirectory(prefix="omo-task-edit-") as tmp:
        subject_path = Path(tmp) / "subject.txt"
        body_path = Path(tmp) / "body.md"
        subject_path.write_text(marker_clear_ack_subject(email_path) + "\n", encoding="utf-8")
        body_path.write_text(marker_clear_ack_body(comment, clear_kind), encoding="utf-8")
        subprocess.run([str(EMAIL_HELPER), "--manager-human", "--subject-file", str(subject_path), "--message-file", str(body_path)], check=True)


def require_metadata(text: str, work_log_root: Path | None = None) -> TaskMetadata:
    metadata = parse_task_metadata(text, work_log_root)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    return metadata


def require_v1_metadata(text: str) -> TaskMetadata:
    metadata = require_metadata(text)
    if metadata.version != TASK_FRONTMATTER_V1:
        raise TaskFrontmatterError("v2 task mutation is disabled until migration validation and watcher enablement are complete.")
    return metadata


def require_task_file(task_file: Path | None) -> Path:
    if task_file is None:
        raise TaskFrontmatterError("task file is required.")
    return task_file


def frontmatter_closing_idx(lines: list[str]) -> int:
    parts = frontmatter_parts("".join(lines))
    if parts is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    frontmatter, _body = parts
    return len(frontmatter) + 1


def pending_list_bounds(lines: list[str]) -> PendingListBounds:
    closing_idx = frontmatter_closing_idx(lines)
    for idx in range(1, closing_idx):
        key, sep, _value = lines[idx].rstrip("\r\n").partition(":")
        if sep and key == "pending_task_items":
            list_end = idx + 1
            while list_end < closing_idx and lines[list_end].startswith("  - "):
                list_end += 1
            return PendingListBounds(idx, list_end)
    raise TaskFrontmatterError("task file has no `pending_task_items` frontmatter field.")


def line_newline(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def preferred_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def v1_record_identity(metadata: TaskMetadata) -> tuple[str, str, str, str, str, bool, str]:
    return (
        metadata.version,
        metadata.status,
        metadata.runat,
        metadata.tool,
        metadata.managerat,
        metadata.is_manager,
        metadata.blocked_on,
    )


def normalize_duplicate_frontmatter(text: str, line_number: int) -> str:
    authoritative = require_v1_metadata(text)
    lines = text.splitlines(keepends=True)
    start_idx = line_number - 1
    authoritative_end_idx = frontmatter_closing_idx(lines)
    if start_idx <= authoritative_end_idx or start_idx >= len(lines) or lines[start_idx].strip() != "---":
        raise TaskFrontmatterError("--line must identify a later frontmatter opening marker.")
    end_idx = next((idx for idx in range(start_idx + 1, len(lines)) if lines[idx].strip() == "---"), None)
    if end_idx is None:
        raise TaskFrontmatterError("later frontmatter opening marker has no closing marker.")
    duplicate_text = "".join(lines[start_idx : end_idx + 1])
    duplicate = require_v1_metadata(duplicate_text)
    if duplicate.pending_task_items:
        raise TaskFrontmatterError("later frontmatter has pending items; refusing to discard them.")
    if v1_record_identity(duplicate) != v1_record_identity(authoritative):
        raise TaskFrontmatterError("later frontmatter does not match the authoritative record.")
    lines[start_idx : end_idx + 1] = [lines[start_idx]]
    updated = "".join(lines)
    if require_v1_metadata(updated) != authoritative:
        raise TaskFrontmatterError("normalization changed the authoritative record.")
    return updated


def render_pending_items(text: str, items: tuple[str, ...]) -> str:
    _ = require_v1_metadata(text)
    lines = text.splitlines(keepends=True)
    bounds = pending_list_bounds(lines)
    newline = line_newline(lines[bounds.field_idx])
    if items:
        replacement = [f"pending_task_items:{newline}", *(f"  - {item}{newline}" for item in items)]
    else:
        replacement = [f"pending_task_items: []{newline}"]
    lines[bounds.field_idx : bounds.list_end] = replacement
    updated = "".join(lines)
    _ = require_metadata(updated)
    return updated


def add_pending_items(text: str, items: tuple[str, ...]) -> tuple[str, int]:
    requested = normalized_items(items)
    metadata = require_v1_metadata(text)
    if metadata.status == "done":
        raise TaskFrontmatterError("task is already done; do not add pending task items to done tasks.")
    existing = list(metadata.pending_task_items)
    seen = set(existing)
    missing: list[str] = []
    for item in requested:
        if item in seen:
            continue
        missing.append(item)
        seen.add(item)
    if not missing:
        return text, 0
    return render_pending_items(text, (*existing, *missing)), len(missing)


def replace_pending_item(text: str, old_item: str, new_item: str) -> tuple[str, bool]:
    metadata = require_v1_metadata(text)
    if metadata.status == "done":
        raise TaskFrontmatterError("task is already done; do not replace pending task items on done tasks.")
    old_value = normalized_item(old_item)
    new_value = normalized_item(new_item)
    items = list(metadata.pending_task_items)
    matches = [idx for idx, item in enumerate(items) if item == old_value]
    if not matches:
        raise TaskFrontmatterError("pending task item not found.")
    if len(matches) > 1:
        raise TaskFrontmatterError("pending task item appears multiple times; remove duplicates before replacing it.")
    if old_value == new_value:
        return text, False
    if new_value in items:
        raise TaskFrontmatterError("replacement pending task item already exists.")
    items[matches[0]] = new_value
    return render_pending_items(text, tuple(items)), True


def remove_pending_items(text: str, items: tuple[str, ...]) -> tuple[str, int]:
    metadata = require_v1_metadata(text)
    requested = normalized_items(items)
    current = list(metadata.pending_task_items)
    missing = [item for item in requested if item not in current]
    if missing:
        raise TaskFrontmatterError(f"pending task item not found: {missing[0]}")
    remove_set = set(requested)
    remaining = tuple(item for item in current if item not in remove_set)
    removed_count = len(current) - len(remaining)
    return render_pending_items(text, remaining), removed_count


def pending_remove_evidence_comment(n_items: int, evidence: str) -> str:
    noun = "item" if n_items == 1 else "items"
    return f"verified removed pending {noun}: {evidence}"


def append_comment(text: str, comment: str) -> str:
    _ = require_v1_metadata(text)
    value = normalized_comment(comment)
    return append_comment_line(text, value)


def append_comment_line(text: str, comment_line: str) -> str:
    newline = preferred_newline(text)
    separator = "" if not text or text.endswith("\n") else newline
    return f"{text}{separator}{comment_line}{newline}"


def metadata_summary_text(metadata: TaskMetadata) -> str:
    lines = [
        f"status: {metadata.status}",
        f"runat: {metadata.runat}",
        f"managerat: {metadata.managerat}",
        f"is_manager: {str(metadata.is_manager).lower()}",
    ]
    if metadata.pending_task_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in metadata.pending_task_items)
    else:
        lines.append("pending_task_items: []")
    return "\n".join(lines) + "\n"


def require_summary_metadata(text: str, work_log_root: Path | None = None) -> TaskMetadata:
    metadata = parse_manager_child_metadata(text, work_log_root)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    if metadata.status == "done" and metadata.runat == "retired" and metadata.pending_task_items:
        raise TaskFrontmatterError("historical done/retired summary requires an empty pending queue.")
    return metadata


def summary_text(text: str, work_log_root: Path | None = None) -> str:
    return metadata_summary_text(require_summary_metadata(text, work_log_root))


def summary_output(root: Path, task_files: tuple[Path, ...]) -> str:
    """Return validated task summaries sorted by manager then task-file label."""
    summaries: list[tuple[str, str, str]] = []
    for task_file in task_files:
        path = task_path(root, task_file)
        text = path.read_text(encoding="utf-8")
        metadata = require_summary_metadata(text, root)
        label = path.relative_to(root).as_posix()
        summaries.append((metadata.managerat, label, metadata_summary_text(metadata)))
    return "".join(f"task_file: {label}\n{text}" for _, label, text in sorted(summaries))


def line_is_pending_marker(text: str, line_number: int) -> bool:
    lines = text.splitlines()
    return 1 <= line_number <= len(lines) and lines[line_number - 1].strip() == PENDING_MARKER


def comment_line_exists(text: str, comment_line: str) -> bool:
    return any(line.strip() == comment_line for line in text.splitlines())


def remove_pending_marker_line(text: str, line_number: int) -> str:
    lines = text.splitlines(keepends=True)
    if line_number < 1 or line_number > len(lines):
        raise TaskFrontmatterError("pending line number is outside the file.")
    if lines[line_number - 1].strip() != PENDING_MARKER:
        raise TaskFrontmatterError("specified line does not contain `(pending)`.")
    del lines[line_number - 1]
    return "".join(lines)


def source_pointer_dedupe_record(source_ref: str, count: int, evidence: str) -> str:
    return normalized_comment(f"deduped {count} bare source pointer(s) for {source_ref}: {evidence}")


def dedupe_bare_source_pointers(text: str, source_ref: str, evidence: str, *, preserve_live_source: bool = False) -> tuple[str, int]:
    """Remove exact duplicate source pointers while refusing a live pending block."""
    pointer = f"(record and delegate {source_ref})"
    lines = text.splitlines(keepends=True)
    remove_indices: list[int] = []
    in_fence = False
    in_pending_block = False
    preserved_live_pointer = False
    for index, line in enumerate(lines):
        physical_line = line.rstrip("\r\n")
        stripped_line = physical_line.strip()
        if stripped_line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped_line == PENDING_MARKER:
            in_pending_block = True
            continue
        if physical_line != pointer:
            continue
        if in_pending_block:
            if preserve_live_source:
                preserved_live_pointer = True
                continue
            raise TaskFrontmatterError("refusing to remove a source pointer inside a live `(pending)` block.")
        remove_indices.append(index)
    if preserve_live_source and not preserved_live_pointer:
        raise TaskFrontmatterError("--preserve-live-source requires an exact source pointer inside a live `(pending)` block.")
    if len(remove_indices) == 1 and not preserve_live_source:
        raise TaskFrontmatterError("refusing to remove a single bare source pointer; use this command only for duplicated intake.")
    if not remove_indices:
        return text, 0
    removal_set = set(remove_indices)
    updated = "".join(line for index, line in enumerate(lines) if index not in removal_set)
    record = source_pointer_dedupe_record(source_ref, len(remove_indices), evidence)
    if comment_line_exists(updated, record):
        return updated, len(remove_indices)
    return append_comment_line(updated, record), len(remove_indices)


def pending_block_lines(text: str, line_number: int) -> list[str]:
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        raise TaskFrontmatterError("pending line number is outside the file.")
    if lines[line_number - 1].strip() != PENDING_MARKER:
        raise TaskFrontmatterError("specified line does not contain `(pending)`.")
    end_idx = len(lines)
    for idx in range(line_number, len(lines)):
        if lines[idx].strip() == PENDING_MARKER:
            end_idx = idx
            break
    return lines[line_number - 1 : end_idx]


def pending_block_is_human_origin(block_lines: list[str]) -> bool:
    stripped_lines = [line.strip() for line in block_lines]
    if any(line.startswith(MANAGER_SOURCE_PREFIXES) for line in stripped_lines):
        return False
    if any(line.startswith(AGENT_SOURCE_PREFIXES) for line in stripped_lines):
        return False
    if any(line.startswith(EMAIL_SOURCE_PREFIXES) for line in stripped_lines):
        return True
    return True


def clear_comment(comment: str, clear_kind: str = "") -> str:
    if clear_kind:
        return f"{clear_kind}: {comment}"
    return comment


def clear_record_line(line_number: int, comment: str, clear_kind: str = "") -> str:
    return normalized_comment(f"pending marker cleared line={line_number}: {clear_comment(comment, clear_kind)}")


def clear_ack_sent_line(line_number: int, comment: str, clear_kind: str = "") -> str:
    return normalized_comment(f"human ack sent for pending marker clear line={line_number}: {clear_comment(comment, clear_kind)}")


def clear_pending_marker(text: str, line_number: int, comment: str, clear_kind: str = "") -> tuple[str, bool]:
    comment_line = clear_record_line(line_number, comment, clear_kind)
    if line_is_pending_marker(text, line_number):
        updated = remove_pending_marker_line(text, line_number)
        if comment_line_exists(updated, comment_line):
            return updated, True
        return append_comment_line(updated, comment_line), True
    if comment_line_exists(text, comment_line):
        return text, False
    raise TaskFrontmatterError("specified line does not contain `(pending)`.")


def marker_clear_recorded(text: str, line_number: int, comment: str, clear_kind: str) -> bool:
    return comment_line_exists(text, clear_record_line(line_number, comment, clear_kind))


def marker_clear_ack_sent(text: str, line_number: int, comment: str, clear_kind: str) -> bool:
    return comment_line_exists(text, clear_ack_sent_line(line_number, comment, clear_kind))


def append_marker_clear_ack_sent(text: str, line_number: int, comment: str, clear_kind: str) -> str:
    comment_line = clear_ack_sent_line(line_number, comment, clear_kind)
    if comment_line_exists(text, comment_line):
        return text
    return append_comment_line(text, comment_line)


def marker_clear_should_ack_human(clear_kind: str) -> bool:
    """Return false for clears that only state an already-known duplicate."""
    return clear_kind not in {"duplicate", "existing-owner-item"}


def remove_marker_clear_ack_sent(text: str, line_number: int, comment: str, clear_kind: str) -> str:
    comment_line = clear_ack_sent_line(line_number, comment, clear_kind)
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip() == comment_line:
            del lines[idx]
            return "".join(lines)
    return text


def validate_clear_kind_value(clear_kind: str) -> None:
    if clear_kind and clear_kind not in CLEAR_KINDS:
        raise TaskFrontmatterError("human-origin marker clear has invalid `--clear-kind`.")


def validate_marker_clear_semantics(args: Args, text: str) -> None:
    validate_clear_kind_value(args.clear_kind)
    human_origin = args.ack_human or pending_block_is_human_origin(pending_block_lines(text, args.line))
    if not human_origin:
        return
    if not args.clear_kind:
        raise TaskFrontmatterError("human-origin marker clear requires `--clear-kind` so the cleared request has semantic evidence.")
    if args.clear_kind != "existing-owner-item":
        return
    if args.owner_task_file is None or not args.owner_item:
        raise TaskFrontmatterError("`--clear-kind existing-owner-item` requires `--owner-task-file` and `--owner-item`.")
    owner_path = task_path(args.root, args.owner_task_file)
    metadata = require_metadata(owner_path.read_text(encoding="utf-8"), args.root)
    if metadata.status == "done":
        raise TaskFrontmatterError("owner task is already done; cite an active owner task item before clearing the human-origin marker.")
    if args.owner_item not in metadata.pending_task_items:
        raise TaskFrontmatterError("owner task does not contain the cited pending item.")


def send_marker_clear_ack_once(path: Path, args: Args, email_path: Path | None) -> None:
    current_before = path.stat()
    current_text = path.read_text(encoding="utf-8")
    if marker_clear_ack_sent(current_text, args.line, args.comment, args.clear_kind):
        return
    updated = append_marker_clear_ack_sent(current_text, args.line, args.comment, args.clear_kind)
    write_if_changed(path, current_text, updated, current_before)
    try:
        send_marker_clear_ack(args.comment, email_path, args.clear_kind)
    except (OSError, subprocess.CalledProcessError):
        rollback_before = path.stat()
        rollback_text = path.read_text(encoding="utf-8")
        rolled_back = remove_marker_clear_ack_sent(rollback_text, args.line, args.comment, args.clear_kind)
        write_if_changed(path, rollback_text, rolled_back, rollback_before)
        raise


def move_pending_item(source_text: str, target_text: str, item: str) -> tuple[str, str, int, int]:
    value = normalized_item(item)
    _ = require_metadata(source_text)
    target_metadata = require_metadata(target_text)
    if target_metadata.status == "done":
        raise TaskFrontmatterError("destination task is already done; do not move pending task items to done tasks.")
    updated_source, removed_count = remove_pending_items(source_text, (value,))
    updated_target, added_count = add_pending_items(target_text, (value,))
    return updated_source, updated_target, removed_count, added_count


def task_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    try:
        _ = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError(f"task file is not UTF-8: {path.name}") from exc
    return payload


def task_snapshot(path: Path) -> tuple[os.stat_result, bytes]:
    before = path.stat()
    payload = task_bytes(path)
    if not same_file_state(before, path.stat()):
        raise TaskFrontmatterError(f"task file changed while being read: {path.name}")
    return before, payload


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def amh_target(target: str) -> bool:
    return target.partition(":")[0].casefold().startswith("amh")


def human_target(target: str) -> bool:
    return target.partition(":")[0].casefold().startswith("h")


def closure_transfer_record(
    direction: str,
    root: Path,
    source_path: Path,
    target_path: Path,
    source_sha256: str,
    destination_sha256: str,
    source_metadata: TaskMetadata,
    authority_ref: str,
    authority_sha256: str,
) -> str:
    record = {
        "direction": direction,
        "source": relative_task_ref(root, source_path),
        "destination": relative_task_ref(root, target_path),
        "source_sha256": source_sha256,
        "destination_before_sha256": destination_sha256,
        "source_status": source_metadata.status,
        "source_blocked_on": source_metadata.blocked_on,
        "authority": authority_ref,
        "authority_sha256": authority_sha256,
    }
    return f"pending closure transfer {json.dumps(record, ensure_ascii=True, separators=(',', ':'), sort_keys=True)}"


def restore_own_write(path: Path, expected: bytes, original: str) -> None:
    current = task_bytes(path)
    if current != expected:
        raise TaskFrontmatterError(f"{path.name} changed after transfer write; refusing unsafe rollback.")
    replace_if_unchanged_locked(path, original, path.stat())


def closure_transfer_transaction_path(root: Path) -> Path:
    return root / ".omo-pending-closure-transfer.json"


def canonical_json(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode()


def read_closure_transfer_transaction(path: Path) -> tuple[dict[str, object], bytes] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskFrontmatterError("pending-closure-transfer recovery record is unsafe.")
    payload = path.read_bytes()
    if not same_file_state(info, path.lstat()):
        raise TaskFrontmatterError("pending-closure-transfer recovery record changed while being read.")
    if len(payload) > 8 * 1024 * 1024:
        raise TaskFrontmatterError("pending-closure-transfer recovery record is oversized.")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("pending-closure-transfer recovery record is malformed.") from exc
    keys = {
        "schema",
        "source",
        "destination",
        "source_sha256",
        "destination_sha256",
        "authority",
        "authority_sha256",
        "source_before",
        "destination_before",
        "source_after",
        "destination_after",
    }
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(value[key], str) for key in keys):
        raise TaskFrontmatterError("pending-closure-transfer recovery record has invalid fields.")
    if value["schema"] != "omo-pending-closure-transfer/v1" or payload != canonical_json(value):
        raise TaskFrontmatterError("pending-closure-transfer recovery record is not canonical.")
    return value, payload


def publish_closure_transfer_transaction(path: Path, record: dict[str, object]) -> bytes:
    payload = canonical_json(record)
    if len(payload) > 8 * 1024 * 1024:
        raise TaskFrontmatterError("pending-closure-transfer recovery record would be oversized.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count == 0:
                raise OSError("pending-closure-transfer recovery record write made no progress")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return payload


def remove_closure_transfer_transaction(path: Path, expected: bytes) -> None:
    if path.read_bytes() != expected:
        raise TaskFrontmatterError("pending-closure-transfer recovery record changed before cleanup.")
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_closure_authority(root: Path, path: Path, expected_sha256: str) -> tuple[bytes, str]:
    authority_path = relative_task_ref(root, path)
    if AMH_SHUTDOWN_AUTHORITY_PATH_RE.fullmatch(authority_path) is None:
        raise TaskFrontmatterError("Human shutdown authority must be one permitted manager_mail source.")
    before = path.lstat()
    if before.st_size > 1_000_000:
        raise TaskFrontmatterError("Human shutdown authority is oversized.")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        not same_file_state(before, after)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or sha256(payload) != expected_sha256
    ):
        raise TaskFrontmatterError("Human shutdown authority is unsafe or does not match its digest.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("Human shutdown authority is not UTF-8.") from exc
    matching_lines = [line_number for line_number, line in enumerate(text.replace("\r\n", "\n").splitlines(), start=1) if line == AMH_SHUTDOWN_AUTHORITY]
    if len(matching_lines) != 1:
        raise TaskFrontmatterError("authority file must contain exactly the Source-1376 Human shutdown instruction.")
    return payload, f"{authority_path}:{matching_lines[0]}-{matching_lines[0]}"


def markdown_paths(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for candidate in root.rglob("*.md"):
        resolved = candidate.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise TaskFrontmatterError(f"Markdown path escapes the task root: {candidate.relative_to(root)}")
        paths.add(resolved)
    return tuple(sorted(paths))


def fsync_task_directories(*paths: Path) -> None:
    for directory in sorted({path.parent for path in paths}):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def raw_target_claims(text: str, target: str) -> bool:
    return any(
        key.strip() == "runat" and separator and same_tmux_target(value.strip(), target)
        for key, separator, value in (line.partition(":") for line in text.splitlines())
    )


# 🧑 Human, Source-1376: “tell those agents to document anything worth keeping long-term, move out their pending task items, then close them all”.
def transfer_pending_items_for_closure(
    root: Path,
    source_path: Path,
    target_path: Path,
    expected_source_sha256: str,
    expected_destination_sha256: str,
    authority_path: Path,
    expected_authority_sha256: str,
) -> int:
    if source_path == target_path:
        raise TaskFrontmatterError("source and destination task files must be different.")
    if v2_enabled(root):
        raise TaskFrontmatterError("pending-closure-transfer supports established v1 queues only; v2 task writes are enabled.")
    transaction_path = closure_transfer_transaction_path(root)
    with root_membership_lock(root):
        initial_markdown_paths = markdown_paths(root)
        locked_paths = tuple(sorted({*initial_markdown_paths, (root / "TODO.md").resolve(), transaction_path.resolve(), authority_path}))
        with ExitStack() as locks:
            for path in locked_paths:
                locks.enter_context(task_file_lock(path))
            todo_state = (root / "TODO.md").stat()
            todo_before = task_bytes(root / "TODO.md")
            linked_paths = task_paths(root)
            if source_path not in linked_paths or target_path not in linked_paths:
                raise TaskFrontmatterError("source and destination must each have active TODO custody.")
            if (
                not same_file_state(todo_state, (root / "TODO.md").stat())
                or task_bytes(root / "TODO.md") != todo_before
                or task_paths(root) != linked_paths
                or markdown_paths(root) != initial_markdown_paths
            ):
                raise TaskFrontmatterError("active task custody changed while transfer was being prepared; retry.")
            authority_payload, authority_ref = validate_closure_authority(root, authority_path, expected_authority_sha256)
            snapshots = {path: task_snapshot(path) for path in initial_markdown_paths}
            payloads = {path: snapshot[1] for path, snapshot in snapshots.items()}
            texts = {path: payload.decode("utf-8") for path, payload in payloads.items()}
            parsed: dict[Path, TaskMetadata] = {}
            malformed: dict[Path, str] = {}
            for path, text in texts.items():
                try:
                    value = parse_task_metadata(text, root)
                except TaskFrontmatterError:
                    malformed[path] = text
                    continue
                if value is not None and value.status != "done":
                    parsed[path] = value
            existing_transaction = read_closure_transfer_transaction(transaction_path)
            transaction_payload: bytes | None = None
            transaction: dict[str, object] | None = None
            if existing_transaction is not None:
                transaction, transaction_payload = existing_transaction
                expected_binding = {
                    "source": relative_task_ref(root, source_path),
                    "destination": relative_task_ref(root, target_path),
                    "source_sha256": expected_source_sha256,
                    "destination_sha256": expected_destination_sha256,
                    "authority": relative_task_ref(root, authority_path),
                    "authority_sha256": expected_authority_sha256,
                }
                if any(transaction[key] != value for key, value in expected_binding.items()):
                    raise TaskFrontmatterError("another pending-closure-transfer recovery record already exists.")
                source_original = str(transaction["source_before"])
                target_original = str(transaction["destination_before"])
            else:
                source_original = texts[source_path]
                target_original = texts[target_path]
            if sha256(source_original.encode()) != expected_source_sha256 or sha256(target_original.encode()) != expected_destination_sha256:
                raise TaskFrontmatterError("source or destination digest does not match the bound original task bytes.")
            source_metadata = require_v1_metadata(source_original)
            target_metadata = require_v1_metadata(target_original)
            if source_metadata.status != "blocked" or not source_metadata.blocked_on or target_metadata.status == "done":
                raise TaskFrontmatterError("pending-closure-transfer requires a Human-authorized blocked source and an active destination.")
            if not amh_target(source_metadata.runat):
                raise TaskFrontmatterError("pending-closure-transfer source must be an AMH task record.")
            if amh_target(target_metadata.runat) or human_target(target_metadata.runat):
                raise TaskFrontmatterError("pending-closure-transfer destination must be a non-AMH, non-human task record.")
            if not target_metadata.is_manager:
                raise TaskFrontmatterError("pending-closure-transfer destination must be a surviving manager task record.")
            for path, text in malformed.items():
                if raw_target_claims(text, source_metadata.runat) or raw_target_claims(text, target_metadata.runat):
                    raise TaskFrontmatterError(f"cannot verify ownership because {relative_task_ref(root, path)} has malformed claiming frontmatter.")
            source_owners = [path for path, value in parsed.items() if same_tmux_target(value.runat, source_metadata.runat)]
            target_owners = [path for path, value in parsed.items() if same_tmux_target(value.runat, target_metadata.runat)]
            if source_owners != [source_path] or target_owners != [target_path]:
                raise TaskFrontmatterError("source and destination must each be the sole active owner of their target.")
            moving = source_metadata.pending_task_items
            if not moving:
                raise TaskFrontmatterError("AMH source pending queue is empty; there is no custody to transfer.")
            for item in moving:
                owners = [path for path, value in parsed.items() for candidate in value.pending_task_items if candidate == item]
                valid_recovery_owners = transaction is not None and len(owners) == len(set(owners)) and set(owners).issubset({source_path, target_path})
                if owners != [source_path] and not valid_recovery_owners:
                    raise TaskFrontmatterError(f"pending item does not have exactly one source owner: {item}")
            updated_source = render_pending_items(source_original, ())
            updated_target = render_pending_items(target_original, (*target_metadata.pending_task_items, *moving))
            updated_source = append_comment(
                updated_source,
                closure_transfer_record(
                    "sent", root, source_path, target_path, expected_source_sha256, expected_destination_sha256, source_metadata, authority_ref, expected_authority_sha256
                ),
            )
            updated_target = append_comment(
                updated_target,
                closure_transfer_record(
                    "received", root, source_path, target_path, expected_source_sha256, expected_destination_sha256, source_metadata, authority_ref, expected_authority_sha256
                ),
            )
            updated_source_payload = updated_source.encode("utf-8")
            updated_target_payload = updated_target.encode("utf-8")
            if transaction is not None:
                if transaction["source_after"] != updated_source or transaction["destination_after"] != updated_target:
                    raise TaskFrontmatterError("pending-closure-transfer recovery record does not match the derived transfer.")
            else:
                transaction = {
                    "schema": "omo-pending-closure-transfer/v1",
                    "source": relative_task_ref(root, source_path),
                    "destination": relative_task_ref(root, target_path),
                    "source_sha256": expected_source_sha256,
                    "destination_sha256": expected_destination_sha256,
                    "authority": relative_task_ref(root, authority_path),
                    "authority_sha256": expected_authority_sha256,
                    "source_before": source_original,
                    "destination_before": target_original,
                    "source_after": updated_source,
                    "destination_after": updated_target,
                }
                transaction_payload = publish_closure_transfer_transaction(transaction_path, transaction)
            if transaction_payload is None:
                raise RuntimeError("pending-closure-transfer recovery state was not initialized")
            try:
                source_current_state, source_current = task_snapshot(source_path)
                target_current_state, target_current = task_snapshot(target_path)
                if source_current not in {source_original.encode(), updated_source_payload} or target_current not in {
                    target_original.encode(),
                    updated_target_payload,
                }:
                    raise TaskFrontmatterError("task bytes conflict with the pending-closure-transfer recovery record.")
                if target_current == target_original.encode():
                    replace_if_unchanged_locked(target_path, updated_target, target_current_state)
                if source_current == source_original.encode():
                    replace_if_unchanged_locked(source_path, updated_source, source_current_state)
            except (OSError, TaskFrontmatterError) as exc:
                rollback_errors: list[str] = []
                try:
                    if task_bytes(target_path) == updated_target_payload:
                        restore_own_write(target_path, updated_target_payload, target_original)
                except (OSError, TaskFrontmatterError) as rollback_exc:
                    rollback_errors.append(f"{target_path.name}: {rollback_exc}")
                try:
                    if task_bytes(source_path) == updated_source_payload:
                        restore_own_write(source_path, updated_source_payload, source_original)
                except (OSError, TaskFrontmatterError) as rollback_exc:
                    rollback_errors.append(f"{source_path.name}: {rollback_exc}")
                for path, original in ((target_path, target_original), (source_path, source_original)):
                    try:
                        if task_bytes(path) != original.encode():
                            rollback_errors.append(f"{path.name}: current bytes are not the recorded original")
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{path.name}: {rollback_exc}")
                if not rollback_errors:
                    fsync_task_directories(source_path, target_path)
                    remove_closure_transfer_transaction(transaction_path, transaction_payload)
                else:
                    raise TaskFrontmatterError(f"transfer failed and rollback was incomplete: {'; '.join(rollback_errors)}") from exc
                raise
            try:
                final_source = task_bytes(source_path)
                final_target = task_bytes(target_path)
                final_source_metadata = require_v1_metadata(final_source.decode("utf-8"))
                final_target_metadata = require_v1_metadata(final_target.decode("utf-8"))
                if (
                    final_source != updated_source_payload
                    or final_target != updated_target_payload
                    or final_source_metadata.pending_task_items
                    or final_target_metadata.pending_task_items != (*target_metadata.pending_task_items, *moving)
                    or task_bytes(root / "TODO.md") != todo_before
                    or task_paths(root) != linked_paths
                    or markdown_paths(root) != initial_markdown_paths
                    or validate_closure_authority(root, authority_path, expected_authority_sha256)[0] != authority_payload
                    or any(task_bytes(path) != payloads[path] for path in initial_markdown_paths if path not in {source_path, target_path})
                ):
                    raise TaskFrontmatterError("post-transfer ownership validation failed.")
            except (OSError, TaskFrontmatterError) as exc:
                rollback_errors: list[str] = []
                for path, expected, original in (
                    (target_path, updated_target_payload, target_original),
                    (source_path, updated_source_payload, source_original),
                ):
                    try:
                        restore_own_write(path, expected, original)
                    except (OSError, TaskFrontmatterError) as rollback_exc:
                        rollback_errors.append(f"{path.name}: {rollback_exc}")
                if rollback_errors:
                    raise TaskFrontmatterError(f"post-transfer validation failed and rollback was incomplete: {'; '.join(rollback_errors)}") from exc
                fsync_task_directories(source_path, target_path)
                remove_closure_transfer_transaction(transaction_path, transaction_payload)
                raise
            fsync_task_directories(source_path, target_path)
            remove_closure_transfer_transaction(transaction_path, transaction_payload)
            return len(moving)


def append_delegate_message(text: str, message: str) -> str:
    metadata = require_v1_metadata(text)
    if metadata.status == "done":
        raise TaskFrontmatterError("task is already done; do not delegate new messages to done tasks.")
    if metadata.is_manager:
        raise TaskFrontmatterError("delegate-message requires a worker task file, not a manager task file.")
    newline = preferred_newline(text)
    separator = "" if not text or text.endswith("\n") else newline
    message_text = message if message.endswith("\n") else f"{message}{newline}"
    return f"{text}{separator}{PENDING_MARKER}{newline}(from manager omo_task_edit delegate-message){newline}{message_text}"


def write_if_changed(path: Path, text: str, updated: str, before: os.stat_result) -> None:
    if updated != text:
        replace_if_unchanged(path, updated, before)


def run(args: Args) -> int:
    try:
        command = canonical_command(args.command)
        if command in {"dependency-add", "dependency-remove"}:
            owner_path = task_path(args.root, require_task_file(args.task_file))
            source_path = task_path(args.root, require_task_file(args.on_task))
            caller_path = current_active_task(args.root)
            caller = load_task(caller_path, root=args.root)
            owner = load_task(owner_path, root=args.root)
            if not caller.metadata["is_manager"]:
                raise BlockingError("dependency changes require an active manager task")
            if not same_tmux_target(owner.metadata["managerat"], caller.metadata["runat"]):
                raise BlockingError("the edited task is not directly owned by the current manager")
            payload: dict[str, object] = {
                "operation": command,
                "task": str(owner_path.relative_to(args.root)),
                "item_id": args.item_id,
                "on_task": str(source_path.relative_to(args.root)),
                "on_item_id": args.on_item_id,
            }
            if command == "dependency-remove":
                payload["evidence"] = args.evidence
            _ = blocking_request(args.root, payload)
            action = "added" if command == "dependency-add" else "removed"
            print(f"{action} dependency {'to' if command == 'dependency-add' else 'from'} item {args.item_id}")
            return 0
        if command == "pending-closure-transfer":
            if args.source_file is None or args.target_file is None:
                raise TaskFrontmatterError("pending-closure-transfer requires source and destination task files.")
            source_path = task_path(args.root, args.source_file)
            target_path = task_path(args.root, args.target_file)
            if (
                SHA256_RE.fullmatch(args.source_sha256) is None
                or SHA256_RE.fullmatch(args.destination_sha256) is None
                or args.authority_file is None
                or SHA256_RE.fullmatch(args.authority_sha256) is None
            ):
                raise TaskFrontmatterError("pending-closure-transfer requires lowercase SHA-256 source, destination, and Human-authority digests.")
            authority_path = task_path(args.root, args.authority_file)
            count = transfer_pending_items_for_closure(
                args.root,
                source_path,
                target_path,
                args.source_sha256,
                args.destination_sha256,
                authority_path,
                args.authority_sha256,
            )
            print(f"transferred {count} pending item(s) from {source_path.name} to {target_path.name} for source closure")
            return 0
        if command == "pending-move":
            if args.source_file is None or args.target_file is None:
                raise TaskFrontmatterError("pending-move requires source and destination task files.")
            source_path = task_path(args.root, args.source_file)
            target_path = task_path(args.root, args.target_file)
            if source_path == target_path:
                raise TaskFrontmatterError("source and destination task files must be different.")
            source_before = source_path.stat()
            target_before = target_path.stat()
            source_text = source_path.read_text(encoding="utf-8")
            target_text = target_path.read_text(encoding="utf-8")
            source_metadata = parse_task_metadata(source_text, args.root)
            target_metadata = parse_task_metadata(target_text, args.root)
            if v2_enabled(args.root) and any(
                metadata is not None and metadata.version == TASK_FRONTMATTER_V1
                for metadata in (source_metadata, target_metadata)
            ):
                raise TaskFrontmatterError("v1 task writes are disabled after v2 enablement")
            if len(args.items) != 1:
                raise TaskFrontmatterError("pending-move requires exactly one pending item.")
            updated_source, updated_target, removed_count, added_count = move_pending_item(source_text, target_text, args.items[0])
            write_if_changed(target_path, target_text, updated_target, target_before)
            write_if_changed(source_path, source_text, updated_source, source_before)
            added_note = "already present in destination" if added_count == 0 else "added to destination"
            print(f"moved {removed_count} pending item(s) from {source_path.name} to {target_path.name}; {added_note}")
            return 0

        if command == "summary":
            task_files = args.task_files or (require_task_file(args.task_file),)
            print(summary_output(args.root, task_files), end="")
            return 0

        path = task_path(args.root, require_task_file(args.task_file))
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        initial_metadata = parse_task_metadata(text, args.root)
        if initial_metadata is not None and initial_metadata.version == TASK_FRONTMATTER_V1 and v2_enabled(args.root):
            raise TaskFrontmatterError("v1 task writes are disabled after v2 enablement")
        if command not in {"summary", "pending-list"}:
            metadata = parse_task_metadata(text, args.root)
            if metadata is not None and metadata.version != TASK_FRONTMATTER_V1:
                raise TaskFrontmatterError("v2 task mutation is disabled until migration validation and watcher enablement are complete.")
        if command == "pending-list":
            for item in require_metadata(text, args.root).pending_task_items:
                print(item)
            return 0
        if command == "frontmatter-normalize":
            updated = normalize_duplicate_frontmatter(text, args.line)
            write_if_changed(path, text, updated, before)
            print(f"normalized later frontmatter in {path.name}:{args.line}")
            return 0
        if command == "pending-add":
            updated, count = add_pending_items(text, args.items)
            write_if_changed(path, text, updated, before)
            print(f"added {count} pending item(s) to {path.name}")
            return 0
        if command == "pending-replace":
            updated, changed = replace_pending_item(text, args.old_item, args.new_item)
            write_if_changed(path, text, updated, before)
            action = "replaced" if changed else "left unchanged"
            print(f"{action} pending item in {path.name}")
            return 0
        if command == "pending-remove":
            evidence = normalized_comment_message(args.evidence)
            updated, count = remove_pending_items(text, args.items)
            updated = append_comment(updated, pending_remove_evidence_comment(count, evidence))
            if not require_owner_completion(
                args.root, path, text, "pending item removed after verification", items=args.items, evidence=evidence
            ):
                raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
            email = plan_completion_email(args.root, path, text, "pending item removed after verification", items=args.items, evidence=evidence)
            write_if_changed(path, text, updated, before)
            sent = send_completion_email(email)
            print(f"removed {count} pending item(s) from {path.name}; {REMOVE_REMINDER}")
            if sent:
                print("Emailed the human with the exact removed work and evidence.")
            return 0
        if command == "pending-marker-clear":
            email_path = task_path(args.root, args.email_file) if args.email_file is not None else None
            validate_clear_kind_value(args.clear_kind)
            should_ack_human = args.ack_human and marker_clear_should_ack_human(args.clear_kind)
            if marker_clear_recorded(text, args.line, args.comment, args.clear_kind):
                if should_ack_human and not marker_clear_ack_sent(text, args.line, args.comment, args.clear_kind) and line_is_pending_marker(text, args.line):
                    raise TaskFrontmatterError("cannot retry human acknowledgement while a new live `(pending)` marker is at the original line.")
                if should_ack_human:
                    send_marker_clear_ack_once(path, args, email_path)
                print(f"already removed `(pending)` from {path.name}:{args.line}; no pending item added")
                return 0
            validate_marker_clear_semantics(args, text)
            updated, changed = clear_pending_marker(text, args.line, args.comment, args.clear_kind)
            write_if_changed(path, text, updated, before)
            if should_ack_human:
                send_marker_clear_ack_once(path, args, email_path)
            action = "removed" if changed else "already removed"
            print(f"{action} `(pending)` from {path.name}:{args.line}; no pending item added")
            return 0
        if command == "source-pointer-dedupe":
            if args.preserve_live_source:
                metadata = require_metadata(text, args.root)
                if metadata.status == "done" or not any(args.source_ref in item for item in metadata.pending_task_items):
                    raise TaskFrontmatterError("--preserve-live-source requires an active pending item that cites --source-ref.")
            updated, count = dedupe_bare_source_pointers(
                text,
                args.source_ref,
                args.evidence,
                preserve_live_source=args.preserve_live_source,
            )
            if count == 0:
                print(f"no bare source pointers found for {args.source_ref} in {path.name}")
                return 0
            write_if_changed(path, text, updated, before)
            print(f"removed {count} bare source pointer(s) for {args.source_ref} from {path.name}")
            return 0
        if command == "comment-add":
            updated = append_comment(text, args.comment)
            write_if_changed(path, text, updated, before)
            print(f"appended comment to {path.name}")
            return 0
        if command == "delegate-message":
            if args.message_file is None:
                raise TaskFrontmatterError("delegate-message requires --message-file.")
            message = message_file_text(normalized_message_file(args.message_file))
            updated = append_delegate_message(text, message)
            write_if_changed(path, text, updated, before)
            print(f"appended pending message to {path.name}")
            return 0
        raise TaskFrontmatterError(f"unknown command: {command}")
    except (OSError, TaskFrontmatterError, BlockingError, subprocess.CalledProcessError, argparse.ArgumentTypeError) as exc:
        print(f"omo_task_edit.py: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
