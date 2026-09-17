#!/usr/bin/env python3
"""Record pending task items and clear the consumed pending marker."""
from __future__ import annotations

import argparse
import hashlib
import os
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
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_blocking import generated_id
from omo_manager.omo_blocking import load_yaml_mapping
from omo_manager.omo_blocking import render_task
from omo_manager.omo_blocking import split_task_text
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_completion_email import NO_CONTACT_RE
from omo_manager.omo_completion_email import completion_email_state_dir
from omo_manager.omo_completion_email import exclusive_record
from omo_manager.omo_completion_email import fsync_directory
from omo_manager.omo_completion_email import owned_private_file
from omo_manager.omo_completion_email import pending_item_notice_body
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import task_path
from omo_manager.omo_task_metadata import PENDING_ITEM_PROVENANCE_HELP
from omo_manager.omo_task_metadata import human_authored_pending_items
from omo_manager.omo_task_metadata import pending_items_with_origin
from omo_manager.omo_task_metadata import render_v1_pending_scalar

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
    item_origin: str


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog=f"""Use this helper for pending blocks that create new task items. It
validates that the `(pending)` marker is still at --line before atomically
recording the items and removing the marker.

{PENDING_ITEM_PROVENANCE_HELP}
Quote human-origin requests as closely as possible in --item. Human-authored
items require --ack-human and --email-file so creation is acknowledged on the
agent's latest verified Human thread.
--task-file is only for atomic initial assignment to a new owner; keep task-file
paths out of worker prompts.""",
    )
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--pending-file", type=Path, required=True, help="Task file containing the consumed `(pending)` line.")
    _ = parser.add_argument("--line", type=int, required=True, help="One-based line number whose stripped content is `(pending)`.")
    _ = parser.add_argument("--task-file", type=Path, help="Initial owner task file that receives `pending_task_items`; defaults to --pending-file.")
    _ = parser.add_argument("--item", action="append", default=[], help="Pending task item to append. Pass once per item.")
    origin = parser.add_mutually_exclusive_group(required=True)
    _ = origin.add_argument("--human-authored", action="store_const", const="human", dest="item_origin", help="Mark added items as Human-authored requests.")
    _ = origin.add_argument("--agent-authored", action="store_const", const="agent", dest="item_origin", help="Mark added items as agent-authored work.")
    _ = parser.add_argument("--ack-human", action="store_true", help="Email the human before the pending marker is consumed and items are recorded.")
    _ = parser.add_argument("--email-file", type=Path, help="Stored `manager_mail/*.txt` file that proves the pending request came from the Human.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    try:
        items = pending_items_with_origin(tuple(normalized_item(item) for item in parsed.item), parsed.item_origin)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if not items:
        parser.error("at least one --item is required; use omo_task_edit.py pending-marker-clear for no-item acknowledgements or omo_task_edit.py pending-replace/pending-remove for existing-item edits.")
    if parsed.ack_human and parsed.item_origin != "human":
        parser.error("--ack-human requires --human-authored.")
    if parsed.item_origin == "human" and not parsed.ack_human:
        parser.error("--human-authored requires --ack-human and --email-file.")
    if parsed.ack_human and parsed.email_file is None:
        parser.error("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
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
    return [f"  - {render_v1_pending_scalar(item)}" for item in items]


def all_items_recorded(text: str, items: tuple[str, ...], work_log_root: Path | None = None) -> bool:
    metadata = parse_task_metadata(text, work_log_root)
    return metadata is not None and all(item in metadata.pending_task_items for item in items)


def add_pending_items(text: str, items: tuple[str, ...], work_log_root: Path | None = None) -> str:
    metadata = parse_task_metadata(text, work_log_root)
    if metadata is None:
        raise TaskFrontmatterError("target task file has no frontmatter.")
    if metadata.status == "done":
        raise TaskFrontmatterError("target task file is already done; record pending items on an active task.")
    if work_log_root is not None and metadata.version == "v1.0.0" and v2_enabled(work_log_root):
        raise TaskFrontmatterError("v1 pending writes are disabled after v2 enablement.")
    if metadata.version == "v2.0.0":
        if work_log_root is None or not v2_enabled(work_log_root):
            raise TaskFrontmatterError("v2 pending writes are disabled until reviewed migration enablement.")
        frontmatter, body = split_task_text(text)
        values = load_yaml_mapping(frontmatter)
        existing = {item["text"] for item in values["pending_task_items"]}
        for item in items:
            if item in existing:
                continue
            values["pending_task_items"].append(
                {
                    "id": generated_id("pi"),
                    "text": item,
                    "blocked_on": [],
                    "notices": [],
                }
            )
            existing.add(item)
        return render_task(values, body, work_log_root)
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


def update_texts(
    pending_text: str,
    target_text: str,
    same_file: bool,
    line: int,
    items: tuple[str, ...],
    work_log_root: Path | None = None,
) -> tuple[str, str]:
    if same_file:
        updated = append_line_once(
            add_pending_items(remove_pending_line(pending_text, line), items, work_log_root),
            recorded_line(line, items),
        )
        return updated, updated
    return (
        append_line_once(remove_pending_line(pending_text, line), recorded_line(line, items)),
        add_pending_items(target_text, items, work_log_root),
    )


def retry_already_recorded(
    pending_text: str,
    target_text: str,
    line: int,
    items: tuple[str, ...],
    work_log_root: Path | None = None,
) -> bool:
    return (
        not pending_marker_at_line(pending_text, line)
        and line_exists(pending_text, recorded_line(line, items))
        and all_items_recorded(target_text, items, work_log_root)
    )


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


def pending_notice_key(args: Args) -> str:
    """Bind one manager-ingress creation notice across safe retries."""
    identity = "\0".join(
        (
            str(args.root.resolve()),
            str(task_path(args.root, args.pending_file)),
            str(task_path(args.root, args.task_file)),
            str(args.line),
            *args.items,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def reserve_pending_notice(pending_path: Path, target_path: Path, args: Args, pending_text: str, target_text: str) -> None:
    """Bind a creation notice to the exact ingress snapshot before delivery."""
    base_pending_text = remove_line_once(pending_text, ack_sent_line(args.line, args.items))
    base_target_text = base_pending_text if pending_path == target_path else target_text
    _updated_pending, updated_target_text = update_texts(
        base_pending_text,
        base_target_text,
        pending_path == target_path,
        args.line,
        args.items,
        args.root,
    )
    pending_sha256 = hashlib.sha256(base_pending_text.encode()).hexdigest()
    target_sha256 = hashlib.sha256(base_target_text.encode()).hexdigest()
    updated_target_sha256 = hashlib.sha256(updated_target_text.encode()).hexdigest()
    payload = (
        "schema=omo-pending-creation-reservation/v1\n"
        f"pending_path={pending_path.resolve()}\n"
        f"target_path={target_path.resolve()}\n"
        f"pending_sha256={pending_sha256}\n"
        f"target_sha256={target_sha256}\n"
        f"updated_target_sha256={updated_target_sha256}\n"
    )
    directory = completion_email_state_dir() / "pending-creation-reservations"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    reservation = directory / pending_notice_key(args)
    try:
        exclusive_record(reservation, payload)
    except FileExistsError:
        try:
            recorded = dict(line.split("=", 1) for line in owned_private_file(reservation, "pending creation reservation", 4096).decode().splitlines())
        except ValueError as exc:
            raise TaskFrontmatterError("pending creation reservation is malformed") from exc
        if (
            recorded.get("schema") != "omo-pending-creation-reservation/v1"
            or recorded.get("pending_path") != str(pending_path.resolve())
            or recorded.get("target_path") != str(target_path.resolve())
            or recorded.get("pending_sha256") != pending_sha256
            or target_sha256 not in {recorded.get("target_sha256"), recorded.get("updated_target_sha256")}
        ):
            raise TaskFrontmatterError("pending source changed after its creation notice was reserved")
    fsync_directory(directory)


def send_human_ack(args: Args) -> None:
    with tempfile.TemporaryDirectory(prefix="omo-record-pending-") as tmp:
        body_path = Path(tmp) / "body.md"
        body_path.write_text(pending_item_notice_body("pending item created", args.items), encoding="utf-8")
        subprocess.run(
            [
                str(EMAIL_HELPER),
                "--manager-human",
                "--non-completion",
                "--pending-notice-key",
                pending_notice_key(args),
                "--message-file",
                str(body_path),
            ],
            check=True,
        )


def send_human_ack_once(
    pending_path: Path,
    target_path: Path,
    args: Args,
    email_path: Path | None,
    expected_pending_sha256: str,
    expected_target_sha256: str,
) -> None:
    if email_path is None:
        raise TaskFrontmatterError("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
    _ = subject_from_email_file(email_path)
    before = pending_path.stat()
    text = pending_path.read_text(encoding="utf-8")
    base_text = remove_line_once(text, ack_sent_line(args.line, args.items))
    target_text = base_text if pending_path == target_path else target_path.read_text(encoding="utf-8")
    if (
        hashlib.sha256(base_text.encode()).hexdigest() != expected_pending_sha256
        or hashlib.sha256(target_text.encode()).hexdigest() != expected_target_sha256
    ):
        raise TaskFrontmatterError("pending source or target changed before its creation notice delivery")
    marker = ack_sent_line(args.line, args.items)
    if line_exists(text, marker):
        return
    send_human_ack(args)
    replace_if_unchanged_locked(pending_path, append_line_once(text, marker), before)


def contact_forbidden(*texts: str) -> bool:
    """Preserve an explicit no-contact rule on either ingress or owner task."""
    return any(NO_CONTACT_RE.search(text) is not None for text in texts)


def record_locked(args: Args, pending_path: Path, target_path: Path, email_path: Path | None) -> str:
    email_text = email_path.read_text(encoding="utf-8") if email_path is not None else ""
    pending_before = pending_path.stat()
    target_before = target_path.stat()
    same_file = pending_path == target_path
    pending_text = pending_path.read_text(encoding="utf-8")
    target_text = pending_text if same_file else target_path.read_text(encoding="utf-8")
    reject_retry_over_new_marker(pending_text, args.line, args.items)
    if retry_already_recorded(pending_text, target_text, args.line, args.items, args.root):
        if args.ack_human and not contact_forbidden(pending_text, target_text, email_text):
            base_pending_text = remove_line_once(pending_text, ack_sent_line(args.line, args.items))
            base_target_text = base_pending_text if same_file else target_text
            send_human_ack_once(
                pending_path,
                target_path,
                args,
                email_path,
                hashlib.sha256(base_pending_text.encode()).hexdigest(),
                hashlib.sha256(base_target_text.encode()).hexdigest(),
            )
        return f"recorded {len(args.items)} pending item(s) in {target_path.name}; `(pending)` was already removed from {pending_path.name}:{args.line}"
    _ = update_texts(pending_text, target_text, same_file, args.line, args.items, args.root)
    if args.ack_human and not contact_forbidden(pending_text, target_text, email_text):
        reserve_pending_notice(pending_path, target_path, args, pending_text, target_text)
        expected_pending = remove_line_once(pending_text, ack_sent_line(args.line, args.items))
        expected_target = expected_pending if same_file else target_text
        send_human_ack_once(
            pending_path,
            target_path,
            args,
            email_path,
            hashlib.sha256(expected_pending.encode()).hexdigest(),
            hashlib.sha256(expected_target.encode()).hexdigest(),
        )
        pending_before = pending_path.stat()
        target_before = target_path.stat()
        pending_text = pending_path.read_text(encoding="utf-8")
        target_text = pending_text if same_file else target_path.read_text(encoding="utf-8")
        reserve_pending_notice(pending_path, target_path, args, pending_text, target_text)
    updated_pending, updated_target = update_texts(pending_text, target_text, same_file, args.line, args.items, args.root)
    if same_file:
        replace_if_unchanged_locked(pending_path, updated_pending, pending_before)
    else:
        replace_if_unchanged_locked(target_path, updated_target, target_before)
        replace_if_unchanged_locked(pending_path, updated_pending, pending_before)
    return f"recorded {len(args.items)} pending item(s) in {target_path.name}; removed `(pending)` from {pending_path.name}:{args.line}"


def run(args: Args) -> int:
    try:
        human_items = human_authored_pending_items(args.items)
        if human_items and human_items != args.items:
            raise TaskFrontmatterError("one pending record cannot mix Human- and agent-authored items.")
        if human_items and not args.ack_human:
            raise TaskFrontmatterError("Human-authored pending items require a Human creation acknowledgement.")
        if args.ack_human and human_items != args.items:
            raise TaskFrontmatterError("Human creation acknowledgement requires explicitly Human-authored pending items.")
        if args.ack_human and args.email_file is None:
            raise TaskFrontmatterError("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
        pending_path = task_path(args.root, args.pending_file)
        target_path = task_path(args.root, args.task_file)
        email_path = task_path(args.root, args.email_file) if args.email_file is not None else None
        with ExitStack() as locks:
            for path in sorted({pending_path, target_path}):
                locks.enter_context(task_file_lock(path))
            result = record_locked(args, pending_path, target_path, email_path)
    except (OSError, TaskFrontmatterError, subprocess.CalledProcessError, argparse.ArgumentTypeError) as exc:
        print(f"omo_record_pending.py: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
