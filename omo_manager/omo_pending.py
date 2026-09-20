#!/usr/bin/env python3
"""Read or update the current agent's pending work queue."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
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
from omo_manager.omo_blocking import body_with_comment
from omo_manager.omo_blocking import document_with
from omo_manager.omo_blocking import load_task
from omo_manager.omo_blocking import replace_item
from omo_manager.omo_blocking import resolve_item
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_blocking import write_document
from omo_manager.omo_task_context import current_pending_task
from omo_manager.omo_task_edit import add_pending_items
from omo_manager.omo_task_edit import append_comment
from omo_manager.omo_task_edit import normalized_comment_message
from omo_manager.omo_task_edit import normalized_items
from omo_manager.omo_task_edit import pending_remove_evidence_comment
from omo_manager.omo_task_edit import remove_pending_items
from omo_manager.omo_task_edit import replace_pending_item
from omo_manager.omo_task_edit import replace_if_unchanged
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import PendingTaskItem
from omo_manager.omo_task_metadata import PENDING_ITEM_PROVENANCE_HELP
from omo_manager.omo_task_metadata import human_authored_pending_items
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_metadata import pending_items_with_origin
from omo_manager.omo_task_metadata import pending_replacement_with_origin
from omo_manager.omo_blocking_actor import request as blocking_request
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import commit_ordinary_pending_transition
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import load_ordinary_pending_transition
from omo_manager.omo_completion_email import OrdinaryPendingRecoveryRequest
from omo_manager.omo_completion_email import ordinary_pending_purpose
from omo_manager.omo_completion_email import plan_sent_recovery_completion
from omo_manager.omo_completion_email import prepare_ordinary_pending_transition
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import source1970_eval_evidence
from omo_manager.omo_completion_email import source1970_eval_queue_items
from omo_manager.omo_completion_email import source1970_eval_recovery_request
from omo_manager.omo_completion_email import source1970_eval_resolution_items
from omo_manager.omo_completion_email import watcher_pangram_recovery_request
from omo_manager.omo_completion_email import WATCHER_PANGRAM_EVIDENCE
from omo_manager.omo_completion_email import WATCHER_PANGRAM_ITEMS


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
    no_email: bool = False
    completion_key: str = ""
    recovery_id: str = ""
    expected_task_sha256: str = ""
    expected_queue_sha256: str = ""
    purpose_sha256: str = ""
    prior_claim_key: str = ""
    prior_task_sha256: str = ""
    prior_manager_target: str = ""
    prior_semantic_key: str = ""
    prior_authorization_sha256: str = ""
    message_id: str = ""
    sent_subject_sha256: str = ""
    sent_body_sha256: str = ""
    churn_commit: str = ""
    churn_before_blob: str = ""
    churn_after_blob: str = ""
    churn_diff_sha256: str = ""
    prior_transition_key: str = ""
    extra_claim_key: str = ""
    extra_task_sha256: str = ""
    extra_manager_target: str = ""
    extra_semantic_key: str = ""
    extra_authorization_sha256: str = ""


@dataclass(frozen=True)
class RemovalNoticeRecovery:
    task_name: str
    task_sha256: str
    items: tuple[str, ...]
    evidence: str
    completion_key: str


SOURCE1929_RECOVERY_ID = "source-1929-1936-1942"
REMOVAL_NOTICE_RECOVERIES = {
    SOURCE1929_RECOVERY_ID: RemovalNoticeRecovery(
        task_name="pending_auth_mail.md",
        task_sha256="8093c2111d29672f002c5899d0bc42eb4e1452f3403508073c60099357f54d5e",
        items=(
            "🧑 Source-1929 (manager_mail/85c5dff58359-1929.txt): pending items authored by the Human must email the Human on creation and closure; pending items authored by agents must not email the Human on either event. Correct Source-1926 behavior and verify both authorship paths.",
            "🧑 Source-1936 (manager_mail/85c5dff58359-1936.txt): pending-item created/deleted emails must reuse the subject of the agent previous email and be concise, formatted as an event label followed by the item list.",
            "🧑 Source-1942 (manager_mail/85c5dff58359-1942.txt): explain the delay, acknowledge acceptance immediately, record this Human-authored item, and complete the combined pending-notice correction.",
        ),
        evidence=(
            "Deployed reviewed commits dcdf3c1 and c762b98; focused live checks passed; combined Human report verified in Gmail "
            "Sent Mail as Message-ID <178960835463.274793.18164703339367696389@gmail.com>."
        ),
        completion_key="cce588d2db12689862a5981a619d17bc9c27e73c4c7593dd1d3473966c9fa070",
    )
}


def pending_item_state(item: PendingTaskItem) -> str:
    if any(dependency.state == "cancelled" for dependency in item.blocked_on):
        return "cancelled"
    return "waiting" if item.blocked_on else "ready"


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Print open items, one per line.")
    add = sub.add_parser(
        "add",
        help="Add open work with explicit provenance.",
        description=PENDING_ITEM_PROVENANCE_HELP,
        allow_abbrev=False,
    )
    add.add_argument("--item", action="append", required=True)
    origin = add.add_mutually_exclusive_group(required=True)
    origin.add_argument("--human-authored", action="store_const", const="human", dest="item_origin", help="Mark added items as Human-authored requests.")
    origin.add_argument("--agent-authored", action="store_const", const="agent", dest="item_origin", help="Mark added items as agent-authored work.")
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
    remove.add_argument(
        "--outcome",
        choices=("completed", "cancelled"),
        help="Required with --item-id and accepted as optional documentation with legacy --item.",
    )
    remove.add_argument("--evidence", required=True)
    remove.add_argument("--completion-key", default="", help="Exact shared SHA-256 identity required for completion email.")
    remove.add_argument(
        "--no-email",
        action="store_true",
        help=(
            "Legacy recovery only: after a separate completion email, remove the verified "
            "--item without sending another email, then reconcile its exact Sent-Mail evidence "
            "before task closure. This cannot be combined with answer-email options."
        ),
    )
    remove.add_argument("--answer-subject-file", type=Path, help="One-line email subject for the combined human answer.")
    remove.add_argument("--answer-message-file", type=Path, help="Email body for the combined human answer.")
    recover = sub.add_parser(
        "recover-removal-notice",
        help="Send one missing Human deletion notice without changing the completed queue.",
    )
    recover.add_argument("--recovery-id", choices=sorted(REMOVAL_NOTICE_RECOVERIES), required=True)
    source1990 = sub.add_parser(
        "recover-source1990-pangram",
        help="Apply the one Source-1990-authorized Pangram completion recovery without sending email.",
    )
    _ = sub.add_parser(
        "recover-source1970-eval",
        help="Apply the one Source-1970-authorized evaluation completion recovery without sending email.",
    )
    _ = sub.add_parser(
        "recover-watcher-pangram-reviewed-sent",
        help="Apply the one reviewed-Sent Pangram watcher cleanup without sending email.",
    )
    for recovery in (source1990,):
        recovery.add_argument("--item", action="append", required=True)
        recovery.add_argument("--expected-task-sha256", required=True)
        recovery.add_argument("--expected-queue-sha256", required=True)
        recovery.add_argument("--purpose-sha256", required=True)
        recovery.add_argument("--prior-claim-key", required=True)
        recovery.add_argument("--prior-task-sha256", required=True)
        recovery.add_argument("--prior-manager-target", required=True)
        recovery.add_argument("--prior-semantic-key", required=True)
        recovery.add_argument("--prior-authorization-sha256", required=True)
        recovery.add_argument("--message-id", required=True)
        recovery.add_argument("--sent-subject-sha256", required=True)
        recovery.add_argument("--sent-body-sha256", required=True)
        recovery.add_argument("--churn-commit", default="")
        recovery.add_argument("--churn-before-blob", default="")
        recovery.add_argument("--churn-after-blob", default="")
        recovery.add_argument("--churn-diff-sha256", default="")
        recovery.add_argument("--prior-transition-key", default="")
        recovery.add_argument("--extra-claim-key", default="")
        recovery.add_argument("--extra-task-sha256", default="")
        recovery.add_argument("--extra-manager-target", default="")
        recovery.add_argument("--extra-semantic-key", default="")
        recovery.add_argument("--extra-authorization-sha256", default="")
    source1990.add_argument("--evidence", required=True)
    wake_ack = sub.add_parser("wake-ack", help="Acknowledge one durable ready-item notice.")
    wake_ack.add_argument("--notice-id", required=True)
    parsed = parser.parse_args(argv)
    if parsed.command == "add":
        return Args("add", pending_items_with_origin(normalized_items(tuple(parsed.item)), parsed.item_origin))
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
        if parsed.no_email and parsed.item_id:
            parser.error("--no-email is supported only for legacy --item removal.")
        if parsed.no_email and (parsed.answer_subject_file or parsed.answer_message_file):
            parser.error("--no-email cannot be combined with answer-email options.")
        if parsed.completion_key and re.fullmatch(r"[0-9a-f]{64}", parsed.completion_key) is None:
            parser.error("--completion-key must be a lowercase SHA-256 digest.")
        if bool(parsed.answer_subject_file) != bool(parsed.answer_message_file):
            parser.error("remove requires both --answer-subject-file and --answer-message-file when either is used.")
        if parsed.answer_subject_file:
            parser.error("pending-item notices cannot be combined with another Human answer.")
        items = normalized_items(tuple(parsed.item or ()))
        if parsed.no_email and human_authored_pending_items(items):
            parser.error("--no-email cannot remove Human-authored pending items.")
        if items and human_authored_pending_items(items) and not parsed.no_email and not parsed.completion_key:
            parser.error("Human-authored item removal requires --completion-key as a lowercase SHA-256 digest.")
        return Args(
            "remove",
            items,
            evidence=normalized_comment_message(parsed.evidence),
            item_id=parsed.item_id or "",
            outcome=parsed.outcome or "",
            answer_subject_file=parsed.answer_subject_file,
            answer_message_file=parsed.answer_message_file,
            no_email=parsed.no_email,
            completion_key=parsed.completion_key,
        )
    if parsed.command == "recover-removal-notice":
        return Args("recover-removal-notice", recovery_id=parsed.recovery_id)
    if parsed.command == "recover-source1970-eval":
        return Args(parsed.command, source1970_eval_queue_items(), evidence=source1970_eval_evidence())
    if parsed.command == "recover-watcher-pangram-reviewed-sent":
        return Args(parsed.command, WATCHER_PANGRAM_ITEMS, evidence=WATCHER_PANGRAM_EVIDENCE)
    if parsed.command == "recover-source1990-pangram":
        hashes = (
            parsed.expected_task_sha256,
            parsed.expected_queue_sha256,
            parsed.purpose_sha256,
            parsed.prior_claim_key,
            parsed.prior_task_sha256,
            parsed.prior_semantic_key,
            parsed.prior_authorization_sha256,
            parsed.sent_subject_sha256,
            parsed.sent_body_sha256,
        )
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
            parser.error("Sent recovery SHA-256 bindings must be exact lowercase digests.")
        if parsed.prior_transition_key and re.fullmatch(r"[0-9a-f]{64}", parsed.prior_transition_key) is None:
            parser.error("--prior-transition-key must be a lowercase SHA-256 digest.")
        churn = (
            parsed.churn_commit,
            parsed.churn_before_blob,
            parsed.churn_after_blob,
            parsed.churn_diff_sha256,
        )
        if any(churn) and not all(churn):
            parser.error("Manager-churn commit, blobs, and diff digest must be supplied together.")
        extra = (
            parsed.extra_claim_key,
            parsed.extra_task_sha256,
            parsed.extra_manager_target,
            parsed.extra_semantic_key,
            parsed.extra_authorization_sha256,
        )
        if any(extra) and not all(extra):
            parser.error("Extra stale claim bindings must be supplied together.")
        if any(extra) and any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (extra[0], extra[1], extra[3], extra[4])):
            parser.error("Extra stale claim bindings require exact lowercase SHA-256 values.")
        items = normalized_items(tuple(parsed.item))
        return Args(
            parsed.command,
            items,
            evidence=normalized_comment_message(getattr(parsed, "evidence", "")),
            expected_task_sha256=parsed.expected_task_sha256,
            expected_queue_sha256=parsed.expected_queue_sha256,
            purpose_sha256=parsed.purpose_sha256,
            prior_claim_key=parsed.prior_claim_key,
            prior_task_sha256=parsed.prior_task_sha256,
            prior_manager_target=parsed.prior_manager_target,
            prior_semantic_key=parsed.prior_semantic_key,
            prior_authorization_sha256=parsed.prior_authorization_sha256,
            message_id=parsed.message_id,
            sent_subject_sha256=parsed.sent_subject_sha256,
            sent_body_sha256=parsed.sent_body_sha256,
            churn_commit=parsed.churn_commit,
            churn_before_blob=parsed.churn_before_blob,
            churn_after_blob=parsed.churn_after_blob,
            churn_diff_sha256=parsed.churn_diff_sha256,
            prior_transition_key=parsed.prior_transition_key,
            extra_claim_key=parsed.extra_claim_key,
            extra_task_sha256=parsed.extra_task_sha256,
            extra_manager_target=parsed.extra_manager_target,
            extra_semantic_key=parsed.extra_semantic_key,
            extra_authorization_sha256=parsed.extra_authorization_sha256,
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


PENDING_ADD_NOTICE_RE = re.compile(
    r"(?m)^\(pending item creation notice: ([0-9a-f]{64}):([0-9a-f]{64})\)$"
)


def pending_add_item_digest(items: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(items).encode()).hexdigest()


def pending_add_key(root: Path, path: Path, text: str, items: tuple[str, ...]) -> str:
    """Identify one queue-add notice across a safe retry before mutation."""

    relative = path.resolve().relative_to(root.resolve()).as_posix()
    item_digest = pending_add_item_digest(items)
    generation = sum(match.group(1) == item_digest for match in PENDING_ADD_NOTICE_RE.finditer(text))
    identity = "\0".join(("pending-item-create", str(root.resolve()), relative, item_digest, str(generation)))
    return hashlib.sha256(identity.encode()).hexdigest()


def pending_add_notice_comment(items: tuple[str, ...], semantic_key: str) -> str:
    """Return the durable successful-transition marker for one add notice."""

    return f"pending item creation notice: {pending_add_item_digest(items)}:{semantic_key}"


def delivered_pending_add_notice(root: Path, path: Path, text: str, items: tuple[str, ...]) -> tuple[str, bool]:
    """Detect a delivered add notice whose task mutation lost a race."""

    semantic_key = pending_add_key(root, path, text, items)
    email = plan_completion_email(
        root,
        path,
        text,
        "pending item created",
        items=items,
        semantic_key=semantic_key,
        pending_item_owner=True,
    )
    return semantic_key, email is not None and completion_email_is_delivered(email)


def require_pending_add_notice(root: Path, path: Path, text: str, items: tuple[str, ...]) -> bool:
    """Deliver an allowed creation notice before adding its exact items."""

    semantic_key = pending_add_key(root, path, text, items)
    email = plan_completion_email(
        root,
        path,
        text,
        "pending item created",
        items=items,
        semantic_key=semantic_key,
        pending_item_owner=True,
    )
    if not require_owner_completion(
        root,
        path,
        text,
        "pending item created",
        items=items,
        owner_may_mutate_after_delivery=True,
        semantic_key=semantic_key,
        pending_item_owner=True,
    ):
        raise BlockingError("responsible-owner pending-item creation email requested; retry addition after owner delivery")
    return email is not None


def require_human_completion_key(items: tuple[str, ...], completion_key: str) -> None:
    """Require replay identity only for a Human-authored pending closure."""
    if items and not completion_key:
        raise BlockingError("Human-authored item removal requires --completion-key")


def pending_queue_sha256(items: tuple[str, ...]) -> str:
    return digest_fields("pending-queue-v1", *items)


def fsync_task_parent(path: Path) -> None:
    """Make an already-replaced task name durable before committing recovery state."""

    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sent_recovery_request(args: Args) -> OrdinaryPendingRecoveryRequest:
    if args.command not in {
        "recover-source1990-pangram",
        "recover-source1970-eval",
        "recover-watcher-pangram-reviewed-sent",
    }:
        raise BlockingError("only authenticated incident recovery adapters are supported")
    if args.command == "recover-source1970-eval":
        return source1970_eval_recovery_request()
    if args.command == "recover-watcher-pangram-reviewed-sent":
        return watcher_pangram_recovery_request()
    mode = "source1990-pangram-remove"
    semantic_key = args.purpose_sha256
    return OrdinaryPendingRecoveryRequest(
        mode,
        args.expected_task_sha256,
        args.expected_queue_sha256,
        args.purpose_sha256,
        semantic_key,
        args.prior_claim_key,
        args.prior_task_sha256,
        args.prior_manager_target,
        args.prior_semantic_key,
        args.prior_authorization_sha256,
        args.message_id,
        args.sent_subject_sha256,
        args.sent_body_sha256,
        args.churn_commit,
        args.churn_before_blob,
        args.churn_after_blob,
        args.churn_diff_sha256,
        args.prior_transition_key,
        args.extra_claim_key,
        args.extra_task_sha256,
        args.extra_manager_target,
        args.extra_semantic_key,
        args.extra_authorization_sha256,
    )


# 🧑 Human: "do not create a domain owner, edit the queue manually, or send a duplicate Human email."
def recover_sent_pending_transition(args: Args, root: Path, path: Path) -> int:
    """Apply one exact add/remove transition using already-delivered Sent evidence."""

    if not args.items or human_authored_pending_items(args.items) != args.items or len(set(args.items)) != len(args.items):
        raise BlockingError("Sent recovery requires distinct Human-authored items")
    if args.command not in {
        "recover-source1990-pangram",
        "recover-source1970-eval",
        "recover-watcher-pangram-reviewed-sent",
    }:
        raise BlockingError("only authenticated incident recovery adapters are supported")
    outcome = "pending item removed after verification"
    request = sent_recovery_request(args)
    resolution_items = source1970_eval_resolution_items() if args.command == "recover-source1970-eval" else args.items
    with task_file_lock(path):
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        metadata = read_task_metadata(path, root)
        if metadata is None or metadata.version != "v1.0.0" or metadata.is_manager:
            raise BlockingError("Sent recovery requires one legacy worker queue")
        current_task_sha256 = hashlib.sha256(text.encode()).hexdigest()
        current_queue_sha256 = pending_queue_sha256(metadata.pending_task_items)
        if current_task_sha256 != request.expected_task_sha256:
            transition = load_ordinary_pending_transition(
                root,
                path,
                outcome,
                resolution_items,
                args.evidence,
                request,
                current_task_sha256,
                current_queue_sha256,
            )
            if transition is None:
                raise BlockingError("Sent recovery task bytes changed without an exact prepared transition")
            commit_ordinary_pending_transition(transition, current_task_sha256)
            print(f"replayed committed {args.command} for {len(args.items)} pending item(s); no email sent")
            return 0
        if current_queue_sha256 != request.expected_queue_sha256:
            raise BlockingError("Sent recovery ordered live queue changed")
        if ordinary_pending_purpose(outcome, resolution_items, args.evidence) != request.purpose_sha256:
            raise BlockingError("Sent recovery purpose digest changed")
        if args.command != "recover-watcher-pangram-reviewed-sent" and metadata.pending_task_items != args.items:
            raise BlockingError("Sent recovery removal must cover the complete ordered live queue")
        updated, count = remove_pending_items(text, args.items)
        updated = append_comment(updated, pending_remove_evidence_comment(count, args.evidence))
        updated_metadata = parse_task_metadata(updated, root)
        if updated_metadata is None:
            raise TaskFrontmatterError("updated pending queue metadata is invalid")
        after_queue_sha256 = pending_queue_sha256(updated_metadata.pending_task_items)
        after_task_sha256 = hashlib.sha256(updated.encode()).hexdigest()
        plan = plan_sent_recovery_completion(
            root,
            path,
            text,
            outcome,
            items=resolution_items,
            evidence=args.evidence,
            semantic_key=request.semantic_key,
        )
        if plan is None:
            raise BlockingError("Sent recovery requires the exact pending-task owner")
        transition = prepare_ordinary_pending_transition(
            plan,
            resolution_items,
            args.evidence,
            after_task_sha256,
            after_queue_sha256,
            request,
            text,
        )
        replace_if_unchanged_locked(path, updated, before)
        fsync_task_parent(path)
        committed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        commit_ordinary_pending_transition(transition, committed_sha256)
        print(f"reconciled {args.command} for {count} pending item(s); no email sent")
        return 0


def run(args: Args, root: Path = DEFAULT_ROOT) -> int:
    if args.command in {"recover-sent-add", "recover-sent-remove"}:
        raise ValueError("generic Sent recovery is not a public command")
    if args.no_email and (args.item_id or args.answer_subject_file or args.answer_message_file):
        raise ValueError("--no-email requires legacy --item removal without answer-email options")
    if args.no_email and human_authored_pending_items(args.items):
        raise ValueError("--no-email cannot remove Human-authored pending items")
    if args.answer_subject_file or args.answer_message_file:
        raise ValueError("pending-item notices cannot be combined with another Human answer")
    path = current_pending_task(root)
    metadata = read_task_metadata(path, root)
    if metadata is None:
        raise TaskFrontmatterError("current work queue metadata is invalid")
    with task_target_lock(root, metadata.runat):
        if current_pending_task(root) != path:
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
        if args.command in {"recover-source1990-pangram", "recover-source1970-eval", "recover-watcher-pangram-reviewed-sent"}:
            return recover_sent_pending_transition(args, root, path)
        answer_subject, answer_body = human_answer(args)
        if args.command == "recover-removal-notice":
            recovery = REMOVAL_NOTICE_RECOVERIES.get(args.recovery_id)
            if recovery is None:
                raise BlockingError("removal-notice recovery id is not supported")
            if current.version != "v1.0.0":
                raise BlockingError("removal-notice recovery requires a legacy queue")
            if path.name != recovery.task_name:
                raise BlockingError("removal-notice recovery task does not match")
            if hashlib.sha256(text.encode()).hexdigest() != recovery.task_sha256:
                raise BlockingError("removal-notice recovery task digest changed")
            if current.pending_task_items:
                raise BlockingError("removal-notice recovery requires the completed queue to remain empty")
            if human_authored_pending_items(recovery.items) != recovery.items or len(set(recovery.items)) != len(recovery.items):
                raise BlockingError("removal-notice recovery record has invalid item provenance")
            evidence_line = f"({pending_remove_evidence_comment(len(recovery.items), recovery.evidence)})"
            if text.splitlines().count(evidence_line) != 1:
                raise BlockingError("removal-notice recovery requires one exact removal evidence record")
            email = plan_completion_email(
                root,
                path,
                text,
                "pending item removed after verification",
                items=recovery.items,
                evidence=recovery.evidence,
                semantic_key=recovery.completion_key,
                pending_item_owner=True,
            )
            if email is None:
                raise BlockingError("removal-notice recovery is forbidden by an explicit blanket no-contact rule")
            if not require_owner_completion(
                root,
                path,
                text,
                "pending item removed after verification",
                items=recovery.items,
                evidence=recovery.evidence,
                owner_may_mutate_after_delivery=True,
                semantic_key=recovery.completion_key,
                pending_item_owner=True,
            ):
                raise BlockingError("responsible-owner deletion notice requested; retry recovery after owner delivery")
            print(f"recovered deletion notice for {len(recovery.items)} completed pending item(s)")
            return 0
        if current.version == "v1.0.0" and v2_enabled(root):
            raise BlockingError("v1 pending writes are disabled after v2 enablement")
        if current.version == "v2.0.0":
            if not v2_enabled(root):
                raise BlockingError("v2 pending writes are disabled until reviewed migration enablement")
            document = load_task(path, root=root)
            if args.command == "add":
                if current.status == "done":
                    raise BlockingError("task is already done")
                existing = set(current.pending_task_items)
                if len(set(args.items)) != len(args.items):
                    raise BlockingError("pending item text is repeated in this request")
                missing_items = tuple(item for item in args.items if item not in existing)
                requested_notice_items = human_authored_pending_items(args.items)
                semantic_key, delivered = (
                    delivered_pending_add_notice(root, path, text, requested_notice_items)
                    if requested_notice_items
                    else ("", False)
                )
                notice_items = requested_notice_items
                if not delivered:
                    if not missing_items:
                        print("added 0 pending item(s)")
                        return 0
                    if len(missing_items) != len(args.items):
                        raise BlockingError("pending item text already exists")
                    notice_items = human_authored_pending_items(missing_items)
                    if notice_items:
                        semantic_key = pending_add_key(root, path, text, notice_items)
                        emailed = require_pending_add_notice(root, path, text, notice_items)
                    else:
                        emailed = False
                else:
                    emailed = False
                comment = pending_add_notice_comment(notice_items, semantic_key) if notice_items and (delivered or emailed) else ""
                if missing_items:
                    item_ids = add_items(document, missing_items, body_comment=comment)
                else:
                    write_document(document_with(document, document.metadata, body_with_comment(document.body, comment)))
                    item_ids = ()
                for item_id in item_ids:
                    print(item_id)
                if emailed:
                    print("Emailed the human with the exact created work.")
                return 0
            if args.command == "replace":
                if not args.item_id:
                    raise BlockingError("v2 replacement requires --item-id")
                matching = [item for item in current.pending_items if item.id == args.item_id]
                if len(matching) != 1:
                    raise BlockingError("pending item was not found exactly once")
                replace_item(document, args.item_id, pending_replacement_with_origin(matching[0].text, args.new_item))
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
                notice_items = human_authored_pending_items((matching[0].text,))
                require_human_completion_key(notice_items, args.completion_key)
                if answer_subject and not notice_items:
                    raise BlockingError("combined human answer requires a Human-authored pending item")
                if not notice_items:
                    resolve_item(document, args.item_id, args.outcome, args.evidence)
                    _ = blocking_request(root, {"operation": "reconcile"})
                    print(f"resolved pending item {args.item_id} as {args.outcome}")
                    return 0
                email = plan_completion_email(
                    root,
                    path,
                    text,
                    f"pending item {args.outcome}",
                    items=notice_items,
                    evidence=args.evidence,
                    human_subject=answer_subject,
                    human_body=answer_body,
                    semantic_key=args.completion_key,
                    pending_item_owner=True,
                )
                if answer_subject and email is None:
                    raise BlockingError("combined human answer is not allowed by this task's reporting policy")
                if not require_owner_completion(
                    root,
                    path,
                    text,
                    f"pending item {args.outcome}",
                    items=notice_items,
                    evidence=args.evidence,
                    human_subject=answer_subject,
                    human_body=answer_body,
                    owner_may_mutate_after_delivery=True,
                    semantic_key=args.completion_key,
                    pending_item_owner=True,
                ):
                    raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
                resolve_item(document, args.item_id, args.outcome, args.evidence)
                _ = blocking_request(root, {"operation": "reconcile"})
                print(f"resolved pending item {args.item_id} as {args.outcome}")
                if email is not None:
                    print("Verified the Human completion notice.")
                return 0
            if args.command == "wake-ack":
                item_id, item_text = acknowledge(document, args.notice_id)
                print(f"{item_id}\t{item_text}")
                return 0
            raise BlockingError("unsupported v2 pending command")
        if args.command == "wake-ack":
            raise BlockingError("wake acknowledgment requires a v2 task")
        if args.command == "add":
            if len(set(args.items)) != len(args.items):
                raise BlockingError("pending item text is repeated in this request")
            existing = set(current.pending_task_items)
            added_items = tuple(item for item in args.items if item not in existing)
            requested_notice_items = human_authored_pending_items(args.items)
            semantic_key, delivered = (
                delivered_pending_add_notice(root, path, text, requested_notice_items)
                if requested_notice_items
                else ("", False)
            )
            if not added_items:
                if delivered:
                    updated = append_comment(text, pending_add_notice_comment(requested_notice_items, semantic_key))
                    replace_if_unchanged(path, updated, before)
                print("added 0 pending item(s)")
                return 0
            updated, count = add_pending_items(text, added_items)
            notice_items = requested_notice_items if delivered else human_authored_pending_items(added_items)
            if not delivered:
                if notice_items:
                    semantic_key = pending_add_key(root, path, text, notice_items)
                    emailed = require_pending_add_notice(root, path, text, notice_items)
                else:
                    emailed = False
            else:
                emailed = False
            if notice_items and (delivered or emailed):
                updated = append_comment(updated, pending_add_notice_comment(notice_items, semantic_key))
            replace_if_unchanged(path, updated, before)
            print(f"added {count} pending item(s)")
            if emailed:
                print("Emailed the human with the exact created work.")
            return 0
        if args.command == "replace":
            updated, changed = replace_pending_item(text, args.old_item, pending_replacement_with_origin(args.old_item, args.new_item))
            replace_if_unchanged(path, updated, before)
            print("replaced pending item" if changed else "pending item unchanged")
            return 0
        updated, count = remove_pending_items(text, args.items)
        updated = append_comment(updated, pending_remove_evidence_comment(count, args.evidence))
        if args.no_email:
            replace_if_unchanged(path, updated, before)
            print(f"removed {count} pending item(s) without email; verify each item was actually done or cancelled")
            return 0
        notice_items = human_authored_pending_items(args.items)
        require_human_completion_key(notice_items, args.completion_key)
        if answer_subject and not notice_items:
            raise BlockingError("combined human answer requires a Human-authored pending item")
        if not notice_items:
            replace_if_unchanged(path, updated, before)
            print(f"removed {count} pending item(s); verify each item was actually done or cancelled")
            return 0
        email = plan_completion_email(
            root,
            path,
            text,
            "pending item removed after verification",
            items=notice_items,
            evidence=args.evidence,
            human_subject=answer_subject,
            human_body=answer_body,
            semantic_key=args.completion_key,
            pending_item_owner=True,
        )
        if answer_subject and email is None:
            raise BlockingError("combined human answer is not allowed by this task's reporting policy")
        if not require_owner_completion(
            root,
            path,
            text,
            "pending item removed after verification",
            items=notice_items,
            evidence=args.evidence,
            human_subject=answer_subject,
            human_body=answer_body,
            owner_may_mutate_after_delivery=True,
            semantic_key=args.completion_key,
            pending_item_owner=True,
        ):
            raise BlockingError("responsible-owner completion email requested; retry removal after owner delivery")
        replace_if_unchanged(path, updated, before)
        print(f"removed {count} pending item(s); verify each item was actually done or cancelled")
        if email is not None:
            print("Verified the Human completion notice.")
        return 0


def main(argv: list[str]) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_pending.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
