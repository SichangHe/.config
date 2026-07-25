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


@dataclass(frozen=True)
class Args:
    command: str
    items: tuple[str, ...] = ()
    old_item: str = ""
    new_item: str = ""
    evidence: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Print open items, one per line.")
    add = sub.add_parser("add", help="Add open work.")
    add.add_argument("--item", action="append", required=True)
    replace = sub.add_parser("replace", help="Replace one exact open item.")
    replace.add_argument("--old-item", required=True)
    replace.add_argument("--new-item", required=True)
    remove = sub.add_parser("remove", help="Remove verified completed or cancelled work.")
    remove.add_argument("--item", action="append", required=True)
    remove.add_argument("--evidence", required=True)
    parsed = parser.parse_args(argv)
    if parsed.command == "add":
        return Args("add", normalized_items(tuple(parsed.item)))
    if parsed.command == "replace":
        return Args("replace", old_item=normalized_items((parsed.old_item,))[0], new_item=normalized_items((parsed.new_item,))[0])
    if parsed.command == "remove":
        return Args("remove", normalized_items(tuple(parsed.item)), evidence=normalized_comment_message(parsed.evidence))
    return Args("list")


def run(args: Args, root: Path = DEFAULT_ROOT) -> int:
    path = current_active_task(root)
    metadata = read_task_metadata(path)
    if metadata is None:
        raise TaskFrontmatterError("current work queue metadata is invalid")
    with task_target_lock(root, metadata.runat):
        if current_active_task(root) != path:
            raise TaskFrontmatterError("current work queue ownership changed; retry")
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        current = read_task_metadata(path)
        if current is None:
            raise TaskFrontmatterError("current work queue metadata is invalid")
        if args.command == "list":
            for item in current.pending_task_items:
                print(item)
            return 0
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
        replace_if_unchanged(path, updated, before)
        print(f"removed {count} pending item(s); verify each item was actually done or cancelled")
        return 0


def main(argv: list[str]) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_pending.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
