#!/usr/bin/env python3
"""Safely edit task-file metadata, pending items, and comments."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

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
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_status import parse_manager_child_metadata
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1
from omo_manager.omo_task_metadata import runat_kind
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import same_file_state
from omo_manager.omo_task_status import task_path
from omo_manager.omo_task_metadata import PENDING_ITEM_PROVENANCE_HELP
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_task_metadata import human_authored_pending_items
from omo_manager.omo_task_metadata import load_v2_mapping
from omo_manager.omo_task_metadata import pending_items_with_origin
from omo_manager.omo_task_metadata import pending_replacement_with_origin
from omo_manager.omo_task_metadata import render_v1_pending_scalar

PENDING_MARKER = "(pending)"
REMOVE_REMINDER = "Verify the removed pending item was actually done or cancelled; consider evaluator agents for uncertain verification."
EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
CLEAR_KINDS = {"cancelled", "duplicate", "existing-owner-item", "report-only", "superseded"}
EMAIL_SOURCE_PREFIXES = ("(record and delegate ", "(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
MANAGER_SOURCE_PREFIXES = ("(from manager ",)
ROUTED_PENDING_PREFIXES = ("(manager handled:",)
SOURCE1503_REF = Path("manager_mail/85c5dff58359-1503.txt")
SOURCE1503_SHA256 = "0eb6cfde4d5ef1160806e36b7077ad49f06c5e17f17248fdfec012f89d1a13eb"
SOURCE1503_EXCERPT = "Subject: Re: Close obsolete DeepWiki planner?\n\nClose them all\n"
SOURCE1506_REF = Path("manager_mail/85c5dff58359-1506.txt")
SOURCE1506_SHA256 = "a9dc947ccaf3be2a05af6b09a6092000b4ae9d7046352f4aa39850a0d96f7bcb"
SOURCE1506_EXCERPT = "Subject: Re: Wix and B12 current counts and latest site\n\nwl:11\n"
SOURCE1528_REF = Path("manager_mail/85c5dff58359-1528.txt")
SOURCE1528_SHA256 = "80573dc300f1a9ae1b79c7161463131e3f3367201957a4118afc892d7e7f1db8"
SOURCE1528_TASK = Path("dw_ops_mgr.md")
SOURCE1528_DISPOSITION_RECORDS = (
    '(verified removed pending item: Human said, “For a manager, close this task is not needed.” Interpreted and acknowledged in the existing Calendar owner email thread as cancelling only Calendar A15 follow-up, with no manager-role closure, calendar editor creation, or event mutation.)',
    "(verified removed pending item: Acknowledged by email in the existing Calendar owner thread as cancelling only Calendar A15 follow-up; calendar queue item removed, persistent manager role preserved, and no calendar editor or event mutation occurred.)",
)
# 🧑 "Subject: Re: Mailbox limit blocked — mail_cleanup_t.md\n\nStale"
SOURCE1788_REF = Path("manager_mail/85c5dff58359-1788.txt")
SOURCE1788_SHA256 = "90ec35f951bdce2eabecf1837b4271c70c969910ff1b14f542eb6f3dffa6de89"
SOURCE1788_TASK = Path("pb_news_mgr.md")
SOURCE1788_DISPOSITION_TASK = Path("mail_cleanup_t.md")
SOURCE1788_POINTER_ONLY_REMOVAL_SHA256 = "f103ab21e5c2885a391bdbbed2a6baa9f6f67aa422aa24ce02385a79cbf6bb43"
SOURCE1788_DISPOSITION_RECORD = (
    "(verified removed pending item: Human Source-1788 says the obsolete mailbox-limit blocker is stale; task status is running and fresh cleanup resumed. "
    "The item is reconciled without changing the separate book task or cleanup threshold items.)"
)
# 🧑 Human: "Terminate this agent. This is mostly a repeated send, and the task has been dispatched to another agent"
SOURCE2050_ROOT = "/ssd1/sichangheagent/work_logs"
SOURCE2050_TASK = Path("watcher_repair.md")
SOURCE2050_TASK_SHA256 = "bd6201c3c6a89266a52fdc640d9f27887f2fd700c600bd7040bdb8cacb78754b"
SOURCE2050_QUEUE_SHA256 = "f01d92a00d1d4365ef974a08615ec52f90b25d3f2effc720d98c1274cbc938c5"
SOURCE2050_PANGRAM_TASK = Path("src1964_pangram.md")
SOURCE2050_PANGRAM_SHA256 = "5a85ca7a0ee27d866936cd50c4bd07b8227d1aed60adcf1a32b8f8951cf1966e"
SOURCE2050_TODO_SHA256 = "a20e84b2a6c03ea7fd93ef1504c392e3b4346efda4d81ae206c4f8f0a5b08432"
SOURCE2050_ITEMS = (
    "Provide and execute the supported no-email terminal closure for queue-empty src1964_pangram.md at live dw:15, preserving Pangram repository/artifact custody and sending no duplicate Human email.",
    "Provide and execute supported no-email, no-duplicate recording of the three exact Human-authored Source-2003 items in src1964_pangram.md using acknowledgement Message-ID <178987548847.1940621.13225615650843671377@gmail.com> and authenticated report replay 1bec2f5a79d7e6b70bdf2032dd24fc03d071a70f7ebbb80156744c5650bcadf8; preserve Human provenance, dw:15 ownership, and send no further email.",
)
SOURCE2050_BODY_EVIDENCE = (
    "(verified removed pending item: Reviewed commit 2887f74d57eb0b047638d3d9bbfef01ca804daae; authenticated dw:15 artifact executed once, recorded exactly three Source-2003 Human items with no recovery email; substantive reviewed answer then sent in existing thread as <178987810510.2416648.579597486821177650@gmail.com>.)",
    "(pending marker cleared line=356: superseded: Superseded by Human Source-2050 termination of dw:15 and supported closure of src1964_pangram.md; no recovery or email remains authorized.)",
)
SOURCE2050_EVIDENCE = "Source-2003 recording is complete, and Human Source-2050 superseded the live dw:15 closure recovery; src1964_pangram.md is done and queue-empty under TODO previous."
SOURCE2050_GUARD = "Source-2050 Pangram cleanup publication is incomplete; retain this item until exact evidence is revalidated."
SOURCE2050_ROLLBACK_ATTEMPTS = 8

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
    expected_task_sha256: str = ""
    authority_file: Path | None = None
    authority_lines: tuple[int, int] = (0, 0)
    authority_sha256: str = ""
    completion_key: str = ""
    expected_source_sha256: str = ""
    expected_disposition_task_sha256: str = ""
    exact_line: str = ""
    blocked_on: str = ""
    expected_todo_sha256: str = ""


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
    completion_key: str = ""
    expected_source_sha256: str = ""
    expected_disposition_task_sha256: str = ""
    exact_line: str = ""
    blocked_on: str = ""
    done: bool = False
    expected_todo_sha256: str = ""
    item_origin: str


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("summary", help="Print task path, frontmatter summary, and pending_task_items.")
    summary_parser.set_defaults(command="summary")
    _ = summary_parser.add_argument("task_file", type=Path, nargs="+")

    list_parser = subparsers.add_parser("pending-list", aliases=["list"], help="Print pending_task_items, one item per line.")
    list_parser.set_defaults(command="pending-list")
    _ = list_parser.add_argument("task_file", type=Path)

    add_parser = subparsers.add_parser(
        "pending-add",
        aliases=["add"],
        help="Append one or more pending_task_items.",
        description=PENDING_ITEM_PROVENANCE_HELP,
        allow_abbrev=False,
    )
    add_parser.set_defaults(command="pending-add")
    _ = add_parser.add_argument("task_file", type=Path)
    _ = add_parser.add_argument("--item", action="append", required=True, help="Pending task item to add. Pass once per item.")
    origin = add_parser.add_mutually_exclusive_group(required=True)
    _ = origin.add_argument("--human-authored", action="store_const", const="human", dest="item_origin", help="Mark added items as Human-authored requests.")
    _ = origin.add_argument("--agent-authored", action="store_const", const="agent", dest="item_origin", help="Mark added items as agent-authored work.")

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
    _ = remove_parser.add_argument("--completion-key", required=True, help="Exact shared lowercase SHA-256 identity required before completion email.")

    move_parser = subparsers.add_parser(
        "pending-move",
        help="Move one pending_task_item from one task file to another.",
        description="Atomically transfer one still-open item to its initial owner. Use only for initial routing.",
    )
    move_parser.set_defaults(command="pending-move")
    _ = move_parser.add_argument("--from", dest="from_file", type=Path, required=True, help="Source task file containing the pending item.")
    _ = move_parser.add_argument("--to", dest="to_file", type=Path, required=True, help="Destination task file that should receive the pending item.")
    _ = move_parser.add_argument("--item", required=True, help="Pending task item to move.")

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
    _ = source_dedupe_parser.add_argument(
        "--preserve-live-source", action="store_true", help="Leave an exact source pointer in a live `(pending)` block intact; requires an active matching queue item."
    )

    source_disposition_parser = subparsers.add_parser(
        "source-pointer-disposition-cleanup",
        help="Remove the one registered bare Human-source pointer after exact same-task disposition.",
        description="Source-bound cleanup for an already dispositioned request; never treats a single pointer as duplicate intake.",
    )
    source_disposition_parser.set_defaults(command="source-pointer-disposition-cleanup")
    _ = source_disposition_parser.add_argument("task_file", type=Path)
    _ = source_disposition_parser.add_argument("--source-ref", required=True)
    _ = source_disposition_parser.add_argument("--expected-task-sha256", required=True)
    _ = source_disposition_parser.add_argument("--expected-source-sha256", required=True)
    _ = source_disposition_parser.add_argument("--expected-disposition-task-sha256", default="")

    trailing_line_parser = subparsers.add_parser(
        "trailing-body-line-remove",
        help="Remove one exact final body line from a completed task under a task-byte CAS guard.",
        description="Removes only the named final physical body line from a done, queue-empty task while preserving every other byte.",
    )
    trailing_line_parser.set_defaults(command="trailing-body-line-remove")
    _ = trailing_line_parser.add_argument("task_file", type=Path)
    _ = trailing_line_parser.add_argument("--line", type=int, required=True, help="One-based line number of the final body line.")
    _ = trailing_line_parser.add_argument("--exact-line", required=True, help="Exact line text without its line ending; use an empty value for a blank line.")
    _ = trailing_line_parser.add_argument("--expected-task-sha256", required=True, help="SHA-256 of the complete task bytes before removal.")

    envelope_parser = subparsers.add_parser(
        "human-envelope-record",
        help="Append one supported digest-bound Human envelope to its exact assigned closure task.",
    )
    envelope_parser.set_defaults(command="human-envelope-record")
    _ = envelope_parser.add_argument("task_file", type=Path)
    _ = envelope_parser.add_argument("--expected-task-sha256", required=True)
    _ = envelope_parser.add_argument("--authority-file", type=Path, required=True)
    _ = envelope_parser.add_argument("--authority-lines", required=True)
    _ = envelope_parser.add_argument("--authority-sha256", required=True)

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

    closed_parser = subparsers.add_parser(
        "closed-status-normalize",
        help="Replace one invalid legacy `closed` status with a digest-bound blocked status without changing runtime state.",
    )
    closed_parser.set_defaults(command="closed-status-normalize")
    _ = closed_parser.add_argument("task_file", type=Path)
    _ = closed_parser.add_argument("--expected-task-sha256", required=True)
    closed_outcome = closed_parser.add_mutually_exclusive_group(required=True)
    _ = closed_outcome.add_argument("--blocked-on")
    _ = closed_outcome.add_argument("--done", action="store_true")

    report_parser = subparsers.add_parser(
        "report-todo-remove",
        help="Remove one digest-bound targetless TODO row for a preserved non-task report.",
    )
    report_parser.set_defaults(command="report-todo-remove")
    _ = report_parser.add_argument("task_file", type=Path)
    _ = report_parser.add_argument("--expected-task-sha256", required=True)
    _ = report_parser.add_argument("--expected-todo-sha256", required=True)

    session_parser = subparsers.add_parser(
        "non-codex-session-normalize",
        help="Move one invalid Codex UUID from non-Codex tmux frontmatter into body history.",
    )
    session_parser.set_defaults(command="non-codex-session-normalize")
    _ = session_parser.add_argument("task_file", type=Path)
    _ = session_parser.add_argument("--expected-task-sha256", required=True)

    source2050_parser = subparsers.add_parser(
        "recover-source2050-pangram-cleanup",
        help="Remove the two digest-bound obsolete Pangram recovery items without email or lifecycle changes.",
    )
    source2050_parser.set_defaults(command="recover-source2050-pangram-cleanup", task_file=SOURCE2050_TASK)

    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    try:
        root = parsed.root.resolve()
        command = canonical_command(parsed.command)
        if command == "frontmatter-normalize":
            if parsed.line < 2:
                parser.error("--line must identify a later frontmatter block.")
            return Args(root, parsed.task_file, command, line=parsed.line)
        if command == "closed-status-normalize":
            expected = parsed.expected_task_sha256.strip()
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                parser.error("--expected-task-sha256 must be a lowercase SHA-256 digest.")
            return Args(
                root,
                parsed.task_file,
                command,
                expected_task_sha256=expected,
                blocked_on="" if parsed.done else normalized_comment_message(parsed.blocked_on),
            )
        if command == "report-todo-remove":
            report_digest = parsed.expected_task_sha256.strip()
            todo_digest = parsed.expected_todo_sha256.strip()
            if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (report_digest, todo_digest)):
                parser.error("report and TODO digests must be lowercase SHA-256 values.")
            return Args(
                root,
                parsed.task_file,
                command,
                expected_task_sha256=report_digest,
                expected_todo_sha256=todo_digest,
            )
        if command == "non-codex-session-normalize":
            expected = parsed.expected_task_sha256.strip()
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                parser.error("--expected-task-sha256 must be a lowercase SHA-256 digest.")
            return Args(root, parsed.task_file, command, expected_task_sha256=expected)
        if command == "recover-source2050-pangram-cleanup":
            return Args(root, SOURCE2050_TASK, command, items=SOURCE2050_ITEMS, evidence=SOURCE2050_EVIDENCE)
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
            items = pending_items_with_origin(normalized_items(tuple(parsed.item or ())), parsed.item_origin)
            return Args(root, parsed.task_file, command, items=items)
        if command == "pending-replace":
            return Args(root, parsed.task_file, command, old_item=normalized_item(parsed.old_item), new_item=normalized_item(parsed.new_item))
        if command == "pending-remove":
            if re.fullmatch(r"[0-9a-f]{64}", parsed.completion_key) is None:
                parser.error("--completion-key must be a lowercase SHA-256 digest.")
            items = normalized_items(tuple(parsed.item or ()))
            return Args(
                root,
                parsed.task_file,
                command,
                items=items,
                evidence=normalized_comment_message(parsed.evidence),
                completion_key=parsed.completion_key,
            )
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
            if parsed.ack_human and parsed.clear_kind not in {"duplicate", "existing-owner-item"} and parsed.email_file is None:
                parser.error("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
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
            return Args(
                root,
                parsed.task_file,
                command,
                comment=normalized_comment_message(parsed.comment),
                line=parsed.line,
                ack_human=parsed.ack_human,
                email_file=parsed.email_file,
                clear_kind=parsed.clear_kind,
            )
        if command == "source-pointer-dedupe":
            return Args(
                root,
                parsed.task_file,
                command,
                evidence=normalized_comment_message(parsed.evidence),
                source_ref=normalized_source_ref(parsed.source_ref),
                preserve_live_source=parsed.preserve_live_source,
            )
        if command == "source-pointer-disposition-cleanup":
            if re.fullmatch(r"[0-9a-f]{64}", parsed.expected_task_sha256) is None or re.fullmatch(r"[0-9a-f]{64}", parsed.expected_source_sha256) is None:
                parser.error("source-pointer-disposition-cleanup requires lowercase task and source SHA-256 digests.")
            if parsed.expected_disposition_task_sha256 and re.fullmatch(r"[0-9a-f]{64}", parsed.expected_disposition_task_sha256) is None:
                parser.error("--expected-disposition-task-sha256 must be a lowercase SHA-256 digest.")
            return Args(
                root,
                parsed.task_file,
                command,
                source_ref=normalized_source_ref(parsed.source_ref),
                expected_task_sha256=parsed.expected_task_sha256,
                expected_source_sha256=parsed.expected_source_sha256,
                expected_disposition_task_sha256=parsed.expected_disposition_task_sha256,
            )
        if command == "trailing-body-line-remove":
            if parsed.line < 1:
                parser.error("--line must be positive.")
            if "\n" in parsed.exact_line or "\r" in parsed.exact_line:
                parser.error("--exact-line must be one physical line without a line ending.")
            if re.fullmatch(r"[0-9a-f]{64}", parsed.expected_task_sha256) is None:
                parser.error("--expected-task-sha256 must be a lowercase SHA-256 digest.")
            return Args(
                root,
                parsed.task_file,
                command,
                line=parsed.line,
                expected_task_sha256=parsed.expected_task_sha256,
                exact_line=parsed.exact_line,
            )
        if command == "human-envelope-record":
            if re.fullmatch(r"[0-9a-f]{64}", parsed.expected_task_sha256) is None or re.fullmatch(r"[0-9a-f]{64}", parsed.authority_sha256) is None:
                parser.error("human-envelope-record requires lowercase task and authority SHA-256 digests.")
            try:
                start_text, end_text = parsed.authority_lines.split("-", 1)
                lines = (int(start_text), int(end_text))
            except (AttributeError, ValueError) as exc:
                raise argparse.ArgumentTypeError("--authority-lines must be START-END") from exc
            if lines != (1, 3):
                parser.error("human-envelope-record supports only the registered lines 1-3 authority excerpts.")
            return Args(
                root,
                parsed.task_file,
                command,
                expected_task_sha256=parsed.expected_task_sha256,
                authority_file=parsed.authority_file,
                authority_lines=lines,
                authority_sha256=parsed.authority_sha256,
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


@dataclass(frozen=True)
class HumanEnvelopeSpec:
    source_ref: Path
    source_sha256: str
    excerpt: str
    task_ref: Path
    runat: str
    status: str
    is_manager: bool


HUMAN_ENVELOPE_SPECS = (
    HumanEnvelopeSpec(SOURCE1503_REF, SOURCE1503_SHA256, SOURCE1503_EXCERPT, Path("dw_rotate_exec.md"), "config:4", "active", False),
    HumanEnvelopeSpec(SOURCE1506_REF, SOURCE1506_SHA256, SOURCE1506_EXCERPT, Path("mail_stale_cleanup.md"), "wl:11", "blocked", False),
)


def human_envelope_spec(args: Args, task_path_value: Path) -> HumanEnvelopeSpec:
    matches = [
        spec
        for spec in HUMAN_ENVELOPE_SPECS
        if task_path_value == args.root / spec.task_ref
        and args.authority_file == spec.source_ref
        and args.authority_lines == (1, 3)
        and args.authority_sha256 == spec.source_sha256
    ]
    if len(matches) != 1:
        raise TaskFrontmatterError("human-envelope-record requires one exact registered task, source, line range, and source digest.")
    return matches[0]


def human_authority_envelope(args: Args, task_path_value: Path, task_text: str) -> tuple[str, HumanEnvelopeSpec]:
    spec = human_envelope_spec(args, task_path_value)
    locator = f"{spec.source_ref}:1-3"
    if (
        hashlib.sha256(task_text.encode()).hexdigest() != args.expected_task_sha256
        or re.search(rf'(?m)^<human_instruction[^\r\n]*\bsource="{re.escape(locator)}"[^\r\n]*>', task_text) is not None
    ):
        raise TaskFrontmatterError("human-envelope-record requires the expected task bytes and an unused registered source binding.")
    metadata = parse_task_metadata(task_text, args.root)
    expected_status = metadata is not None and (metadata.status != "done" if spec.status == "active" else metadata.status == spec.status)
    if (
        metadata is None
        or not expected_status
        or metadata.runat != spec.runat
        or metadata.is_manager != spec.is_manager
        or (
            spec.source_ref == SOURCE1506_REF
            and (
                metadata.blocked_on != "human"
                or metadata.managerat != "wl:12"
                or metadata.pending_task_items
                or any(line.strip() == PENDING_MARKER for line in task_text.splitlines())
            )
        )
    ):
        raise TaskFrontmatterError("human-envelope-record task lifecycle does not match the registered source binding.")
    source_path = args.root / spec.source_ref
    manager_mail_state = source_path.parent.lstat()
    if not stat.S_ISDIR(manager_mail_state.st_mode) or stat.S_ISLNK(manager_mail_state.st_mode) or manager_mail_state.st_uid != os.getuid() or stat.S_IMODE(manager_mail_state.st_mode) & 0o077:
        raise TaskFrontmatterError("registered Human authority directory is not owner-private.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source_path, flags)
    try:
        before = os.fstat(fd)
        data = os.read(fd, 1_000_001)
        after = os.fstat(fd)
        current = source_path.lstat()
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        or hashlib.sha256(data).hexdigest() != spec.source_sha256
    ):
        raise TaskFrontmatterError("registered Human authority source is unsafe or changed.")
    try:
        source_lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("registered Human authority source is not UTF-8.") from exc
    excerpt = "".join(source_lines[:3])
    if excerpt.replace("\r\n", "\n") != spec.excerpt:
        raise TaskFrontmatterError("registered Human authority excerpt changed.")
    block = f'<human_instruction authoritative="true" source="{locator}">\n{excerpt}</human_instruction>\n'
    return task_text.rstrip("\n") + "\n\n" + block, spec


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


def marker_clear_ack_subject(email_path: Path) -> str:
    subject = subject_from_email_file(email_path)
    return subject if subject.lstrip().casefold().startswith("re:") else f"Re: {subject}"


def marker_clear_ack_body() -> str:
    return "Acknowledged: I handled your request without adding a pending item.\n"


def send_marker_clear_ack(email_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="omo-task-edit-") as tmp:
        subject_path = Path(tmp) / "subject.txt"
        body_path = Path(tmp) / "body.md"
        subject_path.write_text(marker_clear_ack_subject(email_path) + "\n", encoding="utf-8")
        body_path.write_text(marker_clear_ack_body(), encoding="utf-8")
        subprocess.run(
            [str(EMAIL_HELPER), "--manager-human", "--non-completion", "--subject-file", str(subject_path), "--message-file", str(body_path)],
            check=True,
        )


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
        replacement = [f"pending_task_items:{newline}", *(f"  - {render_v1_pending_scalar(item)}{newline}" for item in items)]
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


def restore_source2050_items(path: Path, original_items: tuple[str, ...]) -> bool:
    """Restore the two cleanup items while preserving a concurrent watcher edit."""

    for _attempt in range(SOURCE2050_ROLLBACK_ATTEMPTS):
        before = path.stat()
        current = path.read_text(encoding="utf-8")
        metadata = require_metadata(current, path.parent)
        items = list(metadata.pending_task_items)
        if items.count(SOURCE2050_GUARD) > 1:
            raise TaskFrontmatterError("Source-2050 rollback found duplicate publication guards.")
        items = [item for item in items if item != SOURCE2050_GUARD]
        for item in SOURCE2050_ITEMS:
            if items.count(item) > 1:
                raise TaskFrontmatterError("Source-2050 rollback found duplicate Pangram recovery items.")
            if item in items:
                continue
            original_idx = original_items.index(item)
            successors = (candidate for candidate in original_items[original_idx + 1 :] if candidate in items)
            successor = next(successors, None)
            items.insert(items.index(successor) if successor is not None else len(items), item)
        restored = render_pending_items(current, tuple(items))
        try:
            replace_if_unchanged_locked(path, restored, before)
            return True
        except TaskFrontmatterError:
            continue
    return False


def recover_source2050_pangram_cleanup(args: Args, path: Path) -> int:
    """Remove only the two obsolete Pangram recovery items from exact reviewed state."""

    pangram = args.root / SOURCE2050_PANGRAM_TASK
    todo = args.root / "TODO.md"
    with ExitStack() as locks:
        for locked_path in sorted({path, pangram, todo}, key=str):
            locks.enter_context(task_file_lock(locked_path))
        task_before = path.stat()
        pangram_before = pangram.stat()
        todo_before = todo.stat()
        task_bytes = path.read_bytes()
        pangram_bytes = pangram.read_bytes()
        todo_bytes = todo.read_bytes()
        if (
            str(args.root.resolve()) != SOURCE2050_ROOT
            or path != args.root / SOURCE2050_TASK
            or hashlib.sha256(task_bytes).hexdigest() != SOURCE2050_TASK_SHA256
            or hashlib.sha256(pangram_bytes).hexdigest() != SOURCE2050_PANGRAM_SHA256
            or hashlib.sha256(todo_bytes).hexdigest() != SOURCE2050_TODO_SHA256
        ):
            raise TaskFrontmatterError("Source-2050 Pangram cleanup state changed from its reviewed digests.")
        task_text = task_bytes.decode("utf-8")
        task_metadata = require_metadata(task_text, args.root)
        pangram_metadata = require_metadata(pangram_bytes.decode("utf-8"), args.root)
        queue_sha256 = digest_fields("pending-queue-v1", *task_metadata.pending_task_items)
        if (
            task_metadata.status != "running"
            or task_metadata.runat != "config:35"
            or task_metadata.managerat != "config:39"
            or task_metadata.is_manager
            or queue_sha256 != SOURCE2050_QUEUE_SHA256
            or args.items != SOURCE2050_ITEMS
            or args.evidence != SOURCE2050_EVIDENCE
            or any(task_metadata.pending_task_items.count(item) != 1 for item in SOURCE2050_ITEMS)
            or any(task_text.splitlines().count(line) != 1 for line in SOURCE2050_BODY_EVIDENCE)
            or pangram_metadata.status != "done"
            or pangram_metadata.pending_task_items
            or pangram_metadata.runat != "dw:15"
            or todo_bytes.decode("utf-8").splitlines().count("src1964_pangram.md dw:15") != 1
        ):
            raise TaskFrontmatterError("Source-2050 Pangram cleanup bindings changed.")
        removed, count = remove_pending_items(task_text, SOURCE2050_ITEMS)
        if count != len(SOURCE2050_ITEMS):
            raise TaskFrontmatterError("Source-2050 Pangram cleanup did not remove exactly two items.")
        removed_metadata = require_metadata(removed, args.root)
        prepared = render_pending_items(removed, (*removed_metadata.pending_task_items, SOURCE2050_GUARD))
        updated = append_comment(removed, pending_remove_evidence_comment(count, SOURCE2050_EVIDENCE))
        if (
            not same_file_state(pangram_before, pangram.stat())
            or pangram.read_bytes() != pangram_bytes
            or not same_file_state(todo_before, todo.stat())
            or todo.read_bytes() != todo_bytes
        ):
            raise TaskFrontmatterError("Source-2050 Pangram completion evidence changed before cleanup.")
        replace_if_unchanged_locked(path, prepared, task_before)
        prepared_before = path.stat()
        if (
            path.read_bytes() != prepared.encode()
            or not same_file_state(pangram_before, pangram.stat())
            or pangram.read_bytes() != pangram_bytes
            or not same_file_state(todo_before, todo.stat())
            or todo.read_bytes() != todo_bytes
        ):
            restored = restore_source2050_items(path, task_metadata.pending_task_items)
            outcome = "watcher change rolled back" if restored else "publication guard retained"
            raise TaskFrontmatterError(f"Source-2050 Pangram completion evidence changed during cleanup publication; {outcome}.")
        replace_if_unchanged_locked(path, updated, prepared_before)
    print("removed exactly two obsolete Pangram recovery items; no email or lifecycle action taken")
    return 0


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


def source_pointer_disposition_paths(args: Args, path: Path) -> tuple[Path, Path]:
    """Resolve one exact registered source and its disposition record."""

    if path == args.root / SOURCE1528_TASK and args.source_ref == SOURCE1528_REF.as_posix() and args.expected_source_sha256 == SOURCE1528_SHA256:
        if args.expected_disposition_task_sha256:
            raise TaskFrontmatterError("Source-1528 disposition is recorded in the target task; do not supply a separate disposition-task digest.")
        return args.root / SOURCE1528_REF, path
    if path == args.root / SOURCE1788_TASK and args.source_ref == SOURCE1788_REF.as_posix() and args.expected_source_sha256 == SOURCE1788_SHA256:
        if not args.expected_disposition_task_sha256:
            raise TaskFrontmatterError("Source-1788 cleanup requires --expected-disposition-task-sha256.")
        return args.root / SOURCE1788_REF, args.root / SOURCE1788_DISPOSITION_TASK
    raise TaskFrontmatterError("source-pointer-disposition-cleanup requires the exact registered task and Human source binding.")


def dispositioned_source_pointer_cleanup(args: Args, path: Path, text: str, disposition_text: str) -> str:
    """Remove the one registered pointer only after its exact disposition."""

    is_source1788 = path == args.root / SOURCE1788_TASK
    task_sha256 = hashlib.sha256(text.encode()).hexdigest()
    if args.expected_task_sha256 != task_sha256:
        raise TaskFrontmatterError("task bytes do not match --expected-task-sha256.")
    metadata = require_metadata(text, args.root)
    expected_target = "pb:1" if is_source1788 else "dw:18"
    expected_manager = "wl:1" if is_source1788 else "dw:15"
    if metadata.status != "long_running" or metadata.runat != expected_target or metadata.managerat != expected_manager or not metadata.is_manager or has_live_pending_marker(text):
        raise TaskFrontmatterError("registered disposition cleanup requires the unchanged persistent-manager lifecycle and no live pending marker.")
    if is_source1788 and metadata.pending_task_items:
        raise TaskFrontmatterError("Source-1788 cleanup requires the manager queue to remain empty.")
    disposition_records = SOURCE1528_DISPOSITION_RECORDS
    if is_source1788:
        disposition_metadata = require_metadata(disposition_text, args.root)
        if (
            disposition_metadata.status != "done"
            or disposition_metadata.runat != "wl:119"
            or disposition_metadata.managerat != "pb:1"
            or disposition_metadata.is_manager
            or disposition_metadata.pending_task_items
            or has_live_pending_marker(disposition_text)
        ):
            raise TaskFrontmatterError("Source-1788 cleanup requires the completed, queue-empty disposition task with no live pending marker.")
        disposition_records = (SOURCE1788_DISPOSITION_RECORD,)
    visible_lines: list[str] = []
    in_fence = False
    for line in disposition_text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence:
            visible_lines.append(line)
    if any(visible_lines.count(record) != 1 for record in disposition_records):
        raise TaskFrontmatterError("disposition task does not contain the exact unique disposition record(s).")
    source_path = args.root / Path(args.source_ref)
    try:
        source_before = source_path.stat(follow_symlinks=False)
        source_bytes = source_path.read_bytes()
        source_after = source_path.stat(follow_symlinks=False)
    except OSError as error:
        raise TaskFrontmatterError(f"could not bind the exact Human source: {error}") from error
    if (
        not stat.S_ISREG(source_before.st_mode)
        or source_before.st_uid != os.getuid()
        or stat.S_IMODE(source_before.st_mode) & 0o022
        or not same_file_state(source_before, source_after)
        or hashlib.sha256(source_bytes).hexdigest() != args.expected_source_sha256
    ):
        raise TaskFrontmatterError("Human source identity, permissions, or digest does not match the registered request.")
    normalized_source = source_bytes.replace(b"\r\n", b"\n")
    source_matches = normalized_source == "Subject: Re: Mailbox limit blocked — mail_cleanup_t.md\n\nStale\n".encode() if is_source1788 else normalized_source.startswith(
        b"Subject: Re: Calendar owner needed for transcript_tasks.md\n\nFor a manager, close this task is not needed.\n"
    )
    if not source_matches:
        raise TaskFrontmatterError("Human source does not contain the exact registered request.")
    pointer = f"(record and delegate {args.source_ref})"
    lines = text.splitlines(keepends=True)
    pointer_indices: list[int] = []
    in_fence = False
    in_pending_block = False
    for index, line in enumerate(lines):
        physical = line.rstrip("\r\n")
        stripped = physical.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped == PENDING_MARKER:
            in_pending_block = True
            continue
        if physical == pointer:
            if in_pending_block:
                raise TaskFrontmatterError("refusing to remove a source pointer inside a live `(pending)` block.")
            pointer_indices.append(index)
    if is_source1788:
        if not pointer_indices:
            if task_sha256 != SOURCE1788_POINTER_ONLY_REMOVAL_SHA256 or not text.endswith("\n\n"):
                raise TaskFrontmatterError("Source-1788 recovery requires the exact registered pointer-only intermediate bytes.")
            return text[:-1]
        if len(pointer_indices) != 1 or pointer_indices[0] != len(lines) - 1 or pointer_indices[0] < 1 or lines[pointer_indices[0] - 1] != "\n":
            raise TaskFrontmatterError("Source-1788 cleanup requires one final bare pointer with its preceding blank separator.")
        del lines[pointer_indices[0] - 1 : pointer_indices[0] + 1]
        return "".join(lines)
    if len(pointer_indices) != 1:
        raise TaskFrontmatterError("registered disposition cleanup requires exactly one bare source pointer.")
    del lines[pointer_indices[0]]
    updated = "".join(lines)
    record = normalized_comment(
        f"dispositioned source pointer removed for {args.source_ref}: source-sha256={SOURCE1528_SHA256} prior-task-sha256={task_sha256} exact same-task Calendar A15 cancellation records preserved"
    )
    return append_comment_line(updated, record)


def remove_exact_trailing_body_line(text: str, line_number: int, exact_line: str, root: Path) -> str:
    """Remove one unique final body line from a completed queue-empty task."""

    metadata = require_metadata(text, root)
    if metadata.status != "done" or metadata.pending_task_items or has_live_pending_marker(text):
        raise TaskFrontmatterError("trailing body-line cleanup requires a done, queue-empty task with no live `(pending)` marker.")
    lines = text.splitlines(keepends=True)
    if line_number != len(lines) or line_number - 1 <= frontmatter_closing_idx(lines):
        raise TaskFrontmatterError("--line must identify the final physical line in the task body.")

    def physical_line(line: str) -> str:
        return line.removesuffix("\n").removesuffix("\r")

    if physical_line(lines[-1]) != exact_line:
        raise TaskFrontmatterError("final task body line does not match --exact-line.")
    if exact_line and sum(physical_line(line) == exact_line for line in lines) != 1:
        raise TaskFrontmatterError("--exact-line must occur exactly once in the task.")
    updated = "".join(lines[:-1])
    if require_metadata(updated, root) != metadata:
        raise TaskFrontmatterError("trailing body-line cleanup would change task metadata.")
    return updated


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
    if email_path is None:
        raise TaskFrontmatterError("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
    updated = append_marker_clear_ack_sent(current_text, args.line, args.comment, args.clear_kind)
    write_if_changed(path, current_text, updated, current_before)
    try:
        send_marker_clear_ack(email_path)
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
    if has_live_pending_marker(text):
        raise TaskFrontmatterError("delegate-message requires the existing live `(pending)` marker to be consumed first.")
    newline = preferred_newline(text)
    separator = "" if not text or text.endswith("\n") else newline
    message_text = message if message.endswith("\n") else f"{message}{newline}"
    return f"{text}{separator}{PENDING_MARKER}{newline}(from manager omo_task_edit delegate-message){newline}{message_text}"


def has_live_pending_marker(text: str) -> bool:
    """Find a pending marker outside Markdown fences."""

    in_fence = False
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and stripped == PENDING_MARKER:
            next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if not next_line.startswith(ROUTED_PENDING_PREFIXES):
                return True
    return False


def write_if_changed(path: Path, text: str, updated: str, before: os.stat_result) -> None:
    if updated != text:
        replace_if_unchanged(path, updated, before)


def normalize_closed_status(text: str, blocked_on: str, root: Path) -> str:
    lines = text.splitlines(keepends=True)
    closing = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or closing is None:
        raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
    status_rows = [idx for idx in range(1, closing) if lines[idx].partition(":")[0].strip() == "status"]
    blocker_rows = [idx for idx in range(1, closing) if lines[idx].partition(":")[0].strip() == "blocked_on"]
    if len(status_rows) != 1 or lines[status_rows[0]].partition(":")[2].strip() != "closed" or blocker_rows:
        raise TaskFrontmatterError("closed-status normalization requires exactly one `status: closed` and no `blocked_on` field.")
    idx = status_rows[0]
    newline = lines[idx][len(lines[idx].rstrip("\r\n")) :]
    replacement = [f"status: blocked{newline}", f"blocked_on: {blocked_on}{newline}"] if blocked_on else [f"status: done{newline}"]
    lines[idx : idx + 1] = replacement
    updated = "".join(lines)
    metadata = require_metadata(updated, root)
    if metadata.version != TASK_FRONTMATTER_V1:
        raise TaskFrontmatterError("closed-status normalization only supports v1 task records.")
    if blocked_on:
        if metadata.status != "blocked" or metadata.blocked_on != blocked_on or not metadata.pending_task_items:
            raise TaskFrontmatterError("blocked closed-status normalization requires a nonempty preserved queue.")
    elif metadata.status != "done" or metadata.pending_task_items:
        raise TaskFrontmatterError("done closed-status normalization requires an empty queue.")
    elif not metadata.session_id or re.search(rf"\(human closed `{re.escape(metadata.session_id)}` as done\)", text) is None:
        raise TaskFrontmatterError("done closed-status normalization requires exact recorded Human closure of the session.")
    return updated


def normalize_non_codex_session(text: str, root: Path) -> str:
    lines = text.splitlines(keepends=True)
    closing = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or closing is None:
        raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
    session_rows = [idx for idx in range(1, closing) if lines[idx].partition(":")[0].strip() == "session_id"]
    if len(session_rows) != 1:
        raise TaskFrontmatterError("non-Codex session normalization requires exactly one `session_id` field.")
    session_id = lines[session_rows[0]].partition(":")[2].strip()
    try:
        UUID(session_id)
    except ValueError as exc:
        raise TaskFrontmatterError("non-Codex session normalization requires a Codex UUID.") from exc
    del lines[session_rows[0]]
    updated = "".join(lines)
    metadata = require_metadata(updated, root)
    if metadata.tool == "codex" or runat_kind(metadata.runat) != "tmux":
        raise TaskFrontmatterError("non-Codex session normalization requires a non-Codex tmux task.")
    note = f"(historical Codex session_id preserved after tool migration: `{session_id}`.)"
    if note in updated:
        raise TaskFrontmatterError("historical session note already exists.")
    separator = "" if updated.endswith("\n") else preferred_newline(updated)
    return f"{updated}{separator}{note}{preferred_newline(updated)}"


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
            if v2_enabled(args.root) and any(metadata is not None and metadata.version == TASK_FRONTMATTER_V1 for metadata in (source_metadata, target_metadata)):
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
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        if command == "recover-source2050-pangram-cleanup":
            return recover_source2050_pangram_cleanup(args, path)
        if command == "closed-status-normalize":
            if hashlib.sha256(raw_bytes).hexdigest() != args.expected_task_sha256:
                raise TaskFrontmatterError("raw task bytes do not match --expected-task-sha256.")
            updated = normalize_closed_status(text, args.blocked_on, args.root)
            with task_file_lock(path):
                current_before = path.stat()
                current_bytes = path.read_bytes()
                if not same_file_state(before, current_before) or current_bytes != raw_bytes:
                    raise TaskFrontmatterError("task changed before closed-status normalization.")
                replace_if_unchanged_locked(path, updated, current_before)
            outcome = "blocked" if args.blocked_on else "done"
            print(f"normalized invalid closed status in {path.name} to {outcome} without runtime mutation")
            return 0
        if command == "report-todo-remove":
            todo = args.root / "TODO.md"
            if path == todo:
                raise TaskFrontmatterError("report TODO reconciliation requires a report distinct from TODO.md.")
            with ExitStack() as locks:
                for locked_path in sorted({path, todo}, key=str):
                    locks.enter_context(task_file_lock(locked_path))
                current_before = path.stat()
                current_bytes = path.read_bytes()
                todo_before = todo.stat()
                todo_bytes = todo.read_bytes()
                todo_text = todo_bytes.decode("utf-8")
                if not same_file_state(before, current_before) or current_bytes != raw_bytes:
                    raise TaskFrontmatterError("report changed before TODO reconciliation.")
                if hashlib.sha256(current_bytes).hexdigest() != args.expected_task_sha256:
                    raise TaskFrontmatterError("report bytes do not match --expected-task-sha256.")
                if hashlib.sha256(todo_bytes).hexdigest() != args.expected_todo_sha256:
                    raise TaskFrontmatterError("TODO bytes do not match --expected-todo-sha256.")
                parts = frontmatter_parts(text)
                if parts is not None:
                    keys = set(load_v2_mapping("\n".join(parts[0])))
                    task_keys = {"version", "status", "runat", "tool", "managerat", "is_manager", "pending_task_items"}
                    if keys & task_keys:
                        _ = parse_task_metadata(text, args.root)
                        raise TaskFrontmatterError("report TODO reconciliation requires a file without task frontmatter.")
                relative = path.relative_to(args.root).as_posix()
                section = ""
                matches: list[int] = []
                lines = todo_text.splitlines(keepends=True)
                for idx, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.endswith(":"):
                        section = stripped[:-1].casefold()
                    elif stripped == relative:
                        if section != "human pending":
                            raise TaskFrontmatterError("targetless report row must be under `human pending`.")
                        matches.append(idx)
                if len(matches) != 1:
                    raise TaskFrontmatterError("report TODO reconciliation requires exactly one targetless row.")
                del lines[matches[0]]
                replace_if_unchanged_locked(todo, "".join(lines), todo_before)
            print(f"removed targetless non-task report row for {path.name}; report bytes preserved")
            return 0
        if command == "non-codex-session-normalize":
            if hashlib.sha256(raw_bytes).hexdigest() != args.expected_task_sha256:
                raise TaskFrontmatterError("raw task bytes do not match --expected-task-sha256.")
            updated = normalize_non_codex_session(text, args.root)
            with task_file_lock(path):
                current_before = path.stat()
                current_bytes = path.read_bytes()
                if not same_file_state(before, current_before) or current_bytes != raw_bytes:
                    raise TaskFrontmatterError("task changed before non-Codex session normalization.")
                replace_if_unchanged_locked(path, updated, current_before)
            print(f"moved invalid frontmatter session evidence into {path.name} body history")
            return 0
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
            if human_authored_pending_items(args.items):
                raise TaskFrontmatterError(
                    "manager-side pending-add cannot create Human-authored items without a verified Human thread; use omo_record_pending.py or the owner-local omo_pending.py helper."
                )
            updated, count = add_pending_items(text, args.items)
            write_if_changed(path, text, updated, before)
            print(f"added {count} pending item(s) to {path.name}")
            return 0
        if command == "pending-replace":
            updated, changed = replace_pending_item(text, args.old_item, pending_replacement_with_origin(args.old_item, args.new_item))
            write_if_changed(path, text, updated, before)
            action = "replaced" if changed else "left unchanged"
            print(f"{action} pending item in {path.name}")
            return 0
        if command == "pending-remove":
            evidence = normalized_comment_message(args.evidence)
            updated, count = remove_pending_items(text, args.items)
            updated = append_comment(updated, pending_remove_evidence_comment(count, evidence))
            notice_items = human_authored_pending_items(args.items)
            if not require_owner_completion(
                args.root,
                path,
                text,
                "pending item removed after verification",
                items=notice_items,
                evidence=evidence,
                semantic_key=args.completion_key,
            ):
                raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
            email = plan_completion_email(
                args.root,
                path,
                text,
                "pending item removed after verification",
                items=notice_items,
                evidence=evidence,
                semantic_key=args.completion_key,
            )
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
            if should_ack_human and email_path is None:
                raise TaskFrontmatterError("--ack-human requires --email-file so the acknowledgement stays on its verified Human thread.")
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
        if command == "source-pointer-disposition-cleanup":
            source_path, disposition_path = source_pointer_disposition_paths(args, path)
            lock_paths = sorted({path, source_path, disposition_path}, key=str)
            with ExitStack() as locks:
                for lock_path in lock_paths:
                    locks.enter_context(task_file_lock(lock_path))
                current_before = path.stat()
                current_bytes = path.read_bytes()
                if b"\r" in current_bytes or not current_bytes.endswith(b"\n"):
                    raise TaskFrontmatterError("dispositioned source-pointer cleanup requires canonical LF-terminated task bytes.")
                try:
                    current_text = current_bytes.decode("utf-8")
                except UnicodeError as error:
                    raise TaskFrontmatterError(f"task is not valid UTF-8: {error}") from error
                if not same_file_state(before, current_before) or current_text != text:
                    raise TaskFrontmatterError("task changed before dispositioned source-pointer cleanup.")
                if hashlib.sha256(current_bytes).hexdigest() != args.expected_task_sha256:
                    raise TaskFrontmatterError("raw task bytes do not match --expected-task-sha256.")
                disposition_bytes = disposition_path.read_bytes()
                if disposition_path != path and hashlib.sha256(disposition_bytes).hexdigest() != args.expected_disposition_task_sha256:
                    raise TaskFrontmatterError("disposition task bytes do not match --expected-disposition-task-sha256.")
                try:
                    disposition_text = disposition_bytes.decode("utf-8")
                except UnicodeError as error:
                    raise TaskFrontmatterError(f"disposition task is not valid UTF-8: {error}") from error
                updated = dispositioned_source_pointer_cleanup(args, path, current_text, disposition_text)
                replace_if_unchanged_locked(path, updated, current_before)
            print(f"removed one dispositioned bare source pointer for {args.source_ref} from {path.name}")
            return 0
        if command == "trailing-body-line-remove":
            with task_file_lock(path):
                current_before = path.stat()
                current_bytes = path.read_bytes()
                if not same_file_state(before, current_before):
                    raise TaskFrontmatterError("task changed before trailing body-line cleanup.")
                if hashlib.sha256(current_bytes).hexdigest() != args.expected_task_sha256:
                    raise TaskFrontmatterError("raw task bytes do not match --expected-task-sha256.")
                try:
                    current_text = current_bytes.decode("utf-8")
                except UnicodeError as error:
                    raise TaskFrontmatterError(f"task is not valid UTF-8: {error}") from error
                updated = remove_exact_trailing_body_line(current_text, args.line, args.exact_line, args.root)
                replace_if_unchanged_locked(path, updated, current_before)
            print(f"removed exact trailing body line from {path.name}:{args.line}")
            return 0
        if command == "human-envelope-record":
            spec = human_envelope_spec(args, path)
            authority_path = args.root / spec.source_ref
            with task_file_lock(path), task_file_lock(authority_path):
                current_before = path.stat()
                current_text = path.read_text(encoding="utf-8")
                if not same_file_state(before, current_before) or current_text != text:
                    raise TaskFrontmatterError("closure task changed before Human envelope recording.")
                updated, spec = human_authority_envelope(args, path, current_text)
                replace_if_unchanged_locked(path, updated, current_before)
            print(f"recorded exact {spec.source_ref.name} Human envelope in {spec.task_ref}")
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
