#!/usr/bin/env python3
"""Record pending task items and clear the consumed pending marker."""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import task_path

PENDING_MARKER = "(pending)"
EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"


@dataclass(frozen=True)
class Args:
    root: Path
    pending_file: Path
    line: int
    task_file: Path
    items: tuple[str, ...]
    ack_human: bool
    email_file: Path | None = None


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    pending_file: Path
    line: int
    task_file: Path | None = None
    item: list[str]
    ack_human: bool = False
    email_file: Path | None = None


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--pending-file", type=Path, required=True, help="Task file containing the consumed `(pending)` line.")
    _ = parser.add_argument("--line", type=int, required=True, help="One-based line number whose stripped content is `(pending)`.")
    _ = parser.add_argument("--task-file", type=Path, help="Task file that receives `pending_task_items`; defaults to --pending-file.")
    _ = parser.add_argument("--item", action="append", default=[], help="Pending task item to append. Pass once per item.")
    _ = parser.add_argument("--ack-human", action="store_true", help="Email the human after the pending marker and items are recorded.")
    _ = parser.add_argument("--email-file", type=Path, help="Stored `manager_mail/*.txt` file whose `Subject:` header should be used for the human acknowledgement.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    try:
        items = tuple(normalized_item(item) for item in parsed.item)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if not items:
        parser.error("at least one --item is required; use omo_task_edit.py pending-marker-clear for no-item acknowledgements or omo_task_edit.py pending-replace/pending-remove for existing-item edits.")
    return Args(parsed.root.resolve(), parsed.pending_file, parsed.line, parsed.task_file or parsed.pending_file, items, parsed.ack_human, parsed.email_file)


def normalized_item(item: str) -> str:
    value = item.strip()
    if not value:
        raise argparse.ArgumentTypeError("pending task item must not be empty.")
    if "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("pending task item must be one line.")
    return value


def remove_pending_line(text: str, line_number: int) -> str:
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        raise TaskFrontmatterError("pending line number is outside the file.")
    if lines[line_number - 1].strip() != PENDING_MARKER:
        raise TaskFrontmatterError("specified line does not contain `(pending)`.")
    del lines[line_number - 1]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def pending_marker_at_line(text: str, line_number: int) -> bool:
    lines = text.splitlines()
    return 1 <= line_number <= len(lines) and lines[line_number - 1].strip() == PENDING_MARKER


def items_digest(items: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(items).encode("utf-8")).hexdigest()[:16]


def recorded_line(line: int, items: tuple[str, ...]) -> str:
    return f"(pending items recorded line={line}: n={len(items)} sha256={items_digest(items)})"


def ack_sent_line(line: int, items: tuple[str, ...]) -> str:
    return f"(human ack sent for pending items line={line}: n={len(items)} sha256={items_digest(items)})"


def line_exists(text: str, line: str) -> bool:
    return any(value.strip() == line for value in text.splitlines())


def append_line_once(text: str, line: str) -> str:
    if line_exists(text, line):
        return text
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{separator}{line}\n"


def remove_line_once(text: str, line: str) -> str:
    lines = text.splitlines(keepends=True)
    for idx, value in enumerate(lines):
        if value.strip() == line:
            del lines[idx]
            return "".join(lines)
    return text


def item_lines(items: tuple[str, ...]) -> list[str]:
    return [f"  - {item}" for item in items]


def all_items_recorded(text: str, items: tuple[str, ...]) -> bool:
    metadata = parse_task_metadata(text)
    return metadata is not None and all(item in metadata.pending_task_items for item in items)


def add_pending_items(text: str, items: tuple[str, ...]) -> str:
    metadata = parse_task_metadata(text)
    if metadata is None:
        raise TaskFrontmatterError("target task file has no frontmatter.")
    if metadata.status == "done":
        raise TaskFrontmatterError("target task file is already done; record pending items on a running or blocked task.")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise TaskFrontmatterError("target task file has no frontmatter.")
    frontmatter_end = 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            frontmatter_end = idx
            break
    if frontmatter_end == 0:
        raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
    existing = set(metadata.pending_task_items)
    missing_items = tuple(item for item in items if item not in existing)
    if not missing_items:
        return text
    for idx in range(1, frontmatter_end):
        line = lines[idx]
        key, sep, value = line.partition(":")
        if sep and key == "pending_task_items":
            if value.strip() == "[]":
                lines[idx : idx + 1] = ["pending_task_items:", *item_lines(missing_items)]
                return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            insert_at = idx + 1
            while insert_at < frontmatter_end and lines[insert_at].startswith("  - "):
                insert_at += 1
            lines[insert_at:insert_at] = item_lines(missing_items)
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise TaskFrontmatterError("target task file has no `pending_task_items` frontmatter field.")


def update_texts(pending_text: str, target_text: str, same_file: bool, line: int, items: tuple[str, ...]) -> tuple[str, str]:
    if same_file:
        updated = append_line_once(add_pending_items(remove_pending_line(pending_text, line), items), recorded_line(line, items))
        return updated, updated
    return append_line_once(remove_pending_line(pending_text, line), recorded_line(line, items)), add_pending_items(target_text, items)


def retry_already_recorded(pending_text: str, target_text: str, line: int, items: tuple[str, ...]) -> bool:
    return not pending_marker_at_line(pending_text, line) and line_exists(pending_text, recorded_line(line, items)) and all_items_recorded(target_text, items)


def reject_retry_over_new_marker(pending_text: str, line: int, items: tuple[str, ...]) -> None:
    if pending_marker_at_line(pending_text, line) and line_exists(pending_text, recorded_line(line, items)):
        raise TaskFrontmatterError("cannot retry pending record while a new live `(pending)` marker is at the original line.")


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


def ack_subject(email_path: Path | None) -> str:
    if email_path is not None:
        return subject_from_email_file(email_path)
    return "Request recorded"


def ack_body(items: tuple[str, ...]) -> str:
    item_text = "\n".join(f"- {item}" for item in items)
    return f"Added pending items:\n{item_text}\n"


def send_human_ack(items: tuple[str, ...], email_path: Path | None) -> None:
    with tempfile.TemporaryDirectory(prefix="omo-record-pending-") as tmp:
        subject_path = Path(tmp) / "subject.txt"
        body_path = Path(tmp) / "body.md"
        subject_path.write_text(ack_subject(email_path) + "\n", encoding="utf-8")
        body_path.write_text(ack_body(items), encoding="utf-8")
        subprocess.run([str(EMAIL_HELPER), "--manager-human", "--subject-file", str(subject_path), "--message-file", str(body_path)], check=True)


def send_human_ack_once(pending_path: Path, args: Args, email_path: Path | None) -> None:
    before = pending_path.stat()
    text = pending_path.read_text(encoding="utf-8")
    marker = ack_sent_line(args.line, args.items)
    if line_exists(text, marker):
        return
    if pending_marker_at_line(text, args.line):
        raise TaskFrontmatterError("cannot retry human acknowledgement while a new live `(pending)` marker is at the original line.")
    updated = append_line_once(text, marker)
    replace_if_unchanged(pending_path, updated, before)
    try:
        send_human_ack(args.items, email_path)
    except (OSError, subprocess.CalledProcessError):
        rollback_before = pending_path.stat()
        rollback_text = pending_path.read_text(encoding="utf-8")
        replace_if_unchanged(pending_path, remove_line_once(rollback_text, marker), rollback_before)
        raise


def run(args: Args) -> int:
    try:
        pending_path = task_path(args.root, args.pending_file)
        target_path = task_path(args.root, args.task_file)
        email_path = task_path(args.root, args.email_file) if args.email_file is not None else None
        pending_before = pending_path.stat()
        target_before = target_path.stat()
        same_file = pending_path == target_path
        pending_text = pending_path.read_text(encoding="utf-8")
        target_text = pending_text if same_file else target_path.read_text(encoding="utf-8")
        reject_retry_over_new_marker(pending_text, args.line, args.items)
        if retry_already_recorded(pending_text, target_text, args.line, args.items):
            if args.ack_human:
                send_human_ack_once(pending_path, args, email_path)
            print(f"recorded {len(args.items)} pending item(s) in {target_path.name}; `(pending)` was already removed from {pending_path.name}:{args.line}")
            return 0
        updated_pending, updated_target = update_texts(pending_text, target_text, same_file, args.line, args.items)
        if same_file:
            replace_if_unchanged(pending_path, updated_pending, pending_before)
        else:
            replace_if_unchanged(target_path, updated_target, target_before)
            replace_if_unchanged(pending_path, updated_pending, pending_before)
        if args.ack_human:
            send_human_ack_once(pending_path, args, email_path)
    except (OSError, TaskFrontmatterError, subprocess.CalledProcessError, argparse.ArgumentTypeError) as exc:
        print(f"omo_record_pending.py: {exc}", file=sys.stderr)
        return 2
    print(f"recorded {len(args.items)} pending item(s) in {target_path.name}; removed `(pending)` from {pending_path.name}:{args.line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
