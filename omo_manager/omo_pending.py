#!/usr/bin/env python3
"""Read or update the current agent's pending work queue."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import acknowledge
from omo_manager.omo_blocking import add_items
from omo_manager.omo_blocking import load_task
from omo_manager.omo_blocking import replace_item
from omo_manager.omo_blocking import resolve_item
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_edit import add_pending_items
from omo_manager.omo_task_edit import append_comment
from omo_manager.omo_task_edit import normalized_comment_message
from omo_manager.omo_task_edit import normalized_items
from omo_manager.omo_task_edit import pending_remove_evidence_comment
from omo_manager.omo_task_edit import remove_pending_items
from omo_manager.omo_task_edit import replace_pending_item
from omo_manager.omo_task_edit import replace_if_unchanged
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import PendingTaskItem
from omo_manager.omo_blocking_actor import request as blocking_request
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import require_owner_completion


@dataclass(frozen=True)
class Args:
    command: str
    items: tuple[str, ...] = ()
    old_item: str = ""
    new_item: str = ""
    evidence: str = ""
    item_id: str = ""
    outcome: str = ""
    notice_id: str = ""
    answer_subject_file: Path | None = None
    answer_message_file: Path | None = None


def pending_item_state(item: PendingTaskItem) -> str:
    if any(dependency.state == "cancelled" for dependency in item.blocked_on):
        return "cancelled"
    return "waiting" if item.blocked_on else "ready"


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Print open items, one per line.")
    add = sub.add_parser("add", help="Add open work.")
    add.add_argument("--item", action="append", required=True)
    replace = sub.add_parser("replace", help="Replace one exact open item.")
    replace.add_argument("--old-item")
    replace.add_argument("--item-id")
    replace.add_argument("--new-item", required=True)
    remove = sub.add_parser(
        "remove",
        help="Remove verified completed or cancelled work.",
        description=(
            "Remove verified completed or cancelled work. To answer a human question and remove "
            "its pending item with one email, pass both --answer-subject-file and "
            "--answer-message-file; do not send the answer separately with email_me.py. The answer "
            "should keep only information the human still needs now."
        ),
    )
    remove.add_argument("--item", action="append")
    remove.add_argument("--item-id")
    remove.add_argument("--outcome", choices=("completed", "cancelled"))
    remove.add_argument("--evidence", required=True)
    remove.add_argument("--answer-subject-file", type=Path, help="One-line email subject for the combined human answer.")
    remove.add_argument("--answer-message-file", type=Path, help="Email body for the combined human answer.")
    wake_ack = sub.add_parser("wake-ack", help="Acknowledge one durable ready-item notice.")
    wake_ack.add_argument("--notice-id", required=True)
    parsed = parser.parse_args(argv)
    if parsed.command == "add":
        return Args("add", normalized_items(tuple(parsed.item)))
    if parsed.command == "replace":
        if bool(parsed.old_item) == bool(parsed.item_id):
            parser.error("replace requires exactly one of --old-item or --item-id.")
        old_item = normalized_items((parsed.old_item,))[0] if parsed.old_item else ""
        return Args("replace", old_item=old_item, new_item=normalized_items((parsed.new_item,))[0], item_id=parsed.item_id or "")
    if parsed.command == "remove":
        if bool(parsed.item) == bool(parsed.item_id):
            parser.error("remove requires exactly one of --item or --item-id.")
        if parsed.item_id and not parsed.outcome:
            parser.error("remove with --item-id requires --outcome.")
        if parsed.item and parsed.outcome:
            parser.error("legacy --item removal does not accept --outcome.")
        if bool(parsed.answer_subject_file) != bool(parsed.answer_message_file):
            parser.error("remove requires both --answer-subject-file and --answer-message-file when either is used.")
        items = normalized_items(tuple(parsed.item or ()))
        return Args(
            "remove",
            items,
            evidence=normalized_comment_message(parsed.evidence),
            item_id=parsed.item_id or "",
            outcome=parsed.outcome or "",
            answer_subject_file=parsed.answer_subject_file,
            answer_message_file=parsed.answer_message_file,
        )
    if parsed.command == "wake-ack":
        return Args("wake-ack", notice_id=parsed.notice_id)
    return Args("list")


def human_answer(args: Args) -> tuple[str, str]:
    """Read the optional combined answer before changing the pending queue."""

    if args.answer_subject_file is None or args.answer_message_file is None:
        return "", ""
    subject = args.answer_subject_file.read_text(encoding="utf-8").rstrip("\n")
    body = args.answer_message_file.read_text(encoding="utf-8")
    if not subject or not body.strip():
        raise ValueError("combined human answer requires a non-empty subject and message")
    return subject, body


def run(args: Args, root: Path = DEFAULT_ROOT) -> int:
    path = current_active_task(root)
    metadata = read_task_metadata(path, root)
    if metadata is None:
        raise TaskFrontmatterError("current work queue metadata is invalid")
    with task_target_lock(root, metadata.runat):
        if current_active_task(root) != path:
            raise TaskFrontmatterError("current work queue ownership changed; retry")
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        current = read_task_metadata(path, root)
        if current is None:
            raise TaskFrontmatterError("current work queue metadata is invalid")
        if args.command == "list":
            if current.pending_items:
                for item in current.pending_items:
                    print(f"{item.id}\t{item.text}\t{pending_item_state(item)}")
            else:
                for item in current.pending_task_items:
                    print(item)
            return 0
        answer_subject, answer_body = human_answer(args)
        if current.version == "v1.0.0" and v2_enabled(root):
            raise BlockingError("v1 pending writes are disabled after v2 enablement")
        if current.version == "v2.0.0":
            if not v2_enabled(root):
                raise BlockingError("v2 pending writes are disabled until reviewed migration enablement")
            document = load_task(path, root=root)
            if args.command == "add":
                item_ids = add_items(document, args.items)
                for item_id in item_ids:
                    print(item_id)
                return 0
            if args.command == "replace":
                if not args.item_id:
                    raise BlockingError("v2 replacement requires --item-id")
                replace_item(document, args.item_id, args.new_item)
                print(f"replaced pending item {args.item_id}")
                return 0
            if args.command == "remove":
                if not args.item_id or not args.outcome:
                    raise BlockingError("v2 removal requires --item-id and --outcome")
                resolved = [item for item in document.metadata["resolved_task_items"] if item["id"] == args.item_id]
                if resolved:
                    if resolved[0]["outcome"] != args.outcome or resolved[0]["evidence"] != args.evidence:
                        raise BlockingError("pending item was already resolved with different outcome or evidence")
                    _ = blocking_request(root, {"operation": "reconcile"})
                    print(f"pending item {args.item_id} was already resolved as {args.outcome}")
                    return 0
                matching = [item for item in current.pending_items if item.id == args.item_id]
                if len(matching) != 1:
                    raise BlockingError("pending item was not found exactly once")
                email = plan_completion_email(
                    root,
                    path,
                    text,
                    f"pending item {args.outcome}",
                    items=(matching[0].text,),
                    evidence=args.evidence,
                    human_subject=answer_subject,
                    human_body=answer_body,
                )
                if answer_subject and email is None:
                    raise BlockingError("combined human answer is not allowed by this task's reporting policy")
                if not require_owner_completion(
                    root,
                    path,
                    text,
                    f"pending item {args.outcome}",
                    items=(matching[0].text,),
                    evidence=args.evidence,
                    human_subject=answer_subject,
                    human_body=answer_body,
                    owner_may_mutate_after_delivery=True,
                ):
                    raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
                resolve_item(document, args.item_id, args.outcome, args.evidence)
                _ = blocking_request(root, {"operation": "reconcile"})
                print(f"resolved pending item {args.item_id} as {args.outcome}")
                if email is not None:
                    print("Emailed the human with the exact resolved work and evidence.")
                return 0
            if args.command == "wake-ack":
                item_id, item_text = acknowledge(document, args.notice_id)
                print(f"{item_id}\t{item_text}")
                return 0
            raise BlockingError("unsupported v2 pending command")
        if args.command == "wake-ack":
            raise BlockingError("wake acknowledgment requires a v2 task")
        if args.command == "add":
            updated, count = add_pending_items(text, args.items)
            replace_if_unchanged(path, updated, before)
            print(f"added {count} pending item(s)")
            return 0
        if args.command == "replace":
            updated, changed = replace_pending_item(text, args.old_item, args.new_item)
            replace_if_unchanged(path, updated, before)
            print("replaced pending item" if changed else "pending item unchanged")
            return 0
        updated, count = remove_pending_items(text, args.items)
        updated = append_comment(updated, pending_remove_evidence_comment(count, args.evidence))
        email = plan_completion_email(
            root,
            path,
            text,
            "pending item removed after verification",
            items=args.items,
            evidence=args.evidence,
            human_subject=answer_subject,
            human_body=answer_body,
        )
        if answer_subject and email is None:
            raise BlockingError("combined human answer is not allowed by this task's reporting policy")
        if not require_owner_completion(
            root,
            path,
            text,
            "pending item removed after verification",
            items=args.items,
            evidence=args.evidence,
            human_subject=answer_subject,
            human_body=answer_body,
            owner_may_mutate_after_delivery=True,
        ):
            raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
        replace_if_unchanged(path, updated, before)
        print(f"removed {count} pending item(s); verify each item was actually done or cancelled")
        if email is not None:
            print("Emailed the human with the exact removed work and evidence.")
        return 0


def main(argv: list[str]) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_pending.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
