#!/usr/bin/env python3
"""Safely edit task-file pending items and append task comments."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
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
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_blocking_actor import request as blocking_request
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import task_path
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1
from omo_manager.omo_task_metadata import frontmatter_parts

PENDING_MARKER = "(pending)"
REMOVE_REMINDER = "Verify the removed pending item was actually done or cancelled; consider evaluator agents for uncertain verification."
EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
CLEAR_KINDS = {"cancelled", "duplicate", "existing-owner-item", "report-only", "superseded"}
EMAIL_SOURCE_PREFIXES = ("(record and delegate ", "(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
MANAGER_SOURCE_PREFIXES = ("(from manager ",)

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


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("summary", help="Print task frontmatter summary and pending_task_items.")
    summary_parser.set_defaults(command="summary")
    _ = summary_parser.add_argument("task_file", type=Path)

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

    move_parser = subparsers.add_parser("pending-move", help="Move one pending_task_item from one task file to another.")
    move_parser.set_defaults(command="pending-move")
    _ = move_parser.add_argument("--from", dest="from_file", type=Path, required=True, help="Source task file containing the pending item.")
    _ = move_parser.add_argument("--to", dest="to_file", type=Path, required=True, help="Destination task file that should receive the pending item.")
    _ = move_parser.add_argument("--item", required=True, help="Pending task item to move.")

    marker_clear_parser = subparsers.add_parser("pending-marker-clear", help="Remove one pending marker without adding pending_task_items.")
    marker_clear_parser.set_defaults(command="pending-marker-clear")
    _ = marker_clear_parser.add_argument("task_file", type=Path)
    _ = marker_clear_parser.add_argument("--line", type=int, required=True, help="One-based line number whose stripped content is `(pending)`.")
    _ = marker_clear_parser.add_argument("--comment", required=True, help="One-line parenthesized comment evidence explaining why no item was added.")
    _ = marker_clear_parser.add_argument("--ack-human", action="store_true", help="Email the human that no pending item was added.")
    _ = marker_clear_parser.add_argument("--email-file", type=Path, help="Stored `manager_mail/*.txt` file whose `Subject:` header should be used for the human acknowledgement.")
    _ = marker_clear_parser.add_argument("--clear-kind", choices=sorted(CLEAR_KINDS), help="Semantic reason required for human-origin markers.")
    _ = marker_clear_parser.add_argument("--owner-task-file", type=Path, help="Active owner task file containing --owner-item; required for --clear-kind existing-owner-item.")
    _ = marker_clear_parser.add_argument("--owner-item", help="Exact existing pending item already tracking this request.")

    comment_parser = subparsers.add_parser("comment-add", aliases=["comment"], help="Append a parenthesized comment line to a task file.")
    comment_parser.set_defaults(command="comment-add")
    _ = comment_parser.add_argument("task_file", type=Path)
    _ = comment_parser.add_argument("legacy_message", nargs="?", help="Compatibility positional comment text.")
    _ = comment_parser.add_argument("--message", help="One-line comment text to append.")

    delegate_parser = subparsers.add_parser("delegate-message", help="Append a pending message block to a worker task file.")
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

    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    try:
        root = parsed.root.resolve()
        command = canonical_command(parsed.command)
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


def summary_text(text: str, work_log_root: Path | None = None) -> str:
    metadata = require_metadata(text, work_log_root)
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
        if command == "summary":
            print(summary_text(text, args.root), end="")
            return 0
        if command == "pending-list":
            for item in require_metadata(text, args.root).pending_task_items:
                print(item)
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
            write_if_changed(path, text, updated, before)
            print(f"removed {count} pending item(s) from {path.name}; {REMOVE_REMINDER}")
            return 0
        if command == "pending-marker-clear":
            email_path = task_path(args.root, args.email_file) if args.email_file is not None else None
            validate_clear_kind_value(args.clear_kind)
            if marker_clear_recorded(text, args.line, args.comment, args.clear_kind):
                if args.ack_human and not marker_clear_ack_sent(text, args.line, args.comment, args.clear_kind) and line_is_pending_marker(text, args.line):
                    raise TaskFrontmatterError("cannot retry human acknowledgement while a new live `(pending)` marker is at the original line.")
                if args.ack_human:
                    send_marker_clear_ack_once(path, args, email_path)
                print(f"already removed `(pending)` from {path.name}:{args.line}; no pending item added")
                return 0
            validate_marker_clear_semantics(args, text)
            updated, changed = clear_pending_marker(text, args.line, args.comment, args.clear_kind)
            write_if_changed(path, text, updated, before)
            if args.ack_human:
                send_marker_clear_ack_once(path, args, email_path)
            action = "removed" if changed else "already removed"
            print(f"{action} `(pending)` from {path.name}:{args.line}; no pending item added")
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
    except (OSError, TaskFrontmatterError, subprocess.CalledProcessError, argparse.ArgumentTypeError) as exc:
        print(f"omo_task_edit.py: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
