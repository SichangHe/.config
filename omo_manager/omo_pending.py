#!/usr/bin/env python3
"""Read or update the current agent's pending work queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, replace
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
from omo_manager.omo_task_lock import task_file_lock_at_path
from omo_manager.omo_task_metadata import PendingTaskItem
from omo_manager.omo_task_metadata import PENDING_ITEM_PROVENANCE_HELP
from omo_manager.omo_task_metadata import human_authored_pending_items
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_metadata import pending_items_with_origin
from omo_manager.omo_task_metadata import pending_replacement_with_origin
from omo_manager.omo_blocking_actor import request as blocking_request
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import completion_email_state_dir
from omo_manager.omo_completion_email import claims_rows
from omo_manager.omo_completion_email import commit_ordinary_pending_transition
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import load_ordinary_pending_transition
from omo_manager.omo_completion_email import mail_compress_recovery_request
from omo_manager.omo_completion_email import OrdinaryPendingRecoveryRequest
from omo_manager.omo_completion_email import ordinary_pending_purpose
from omo_manager.omo_completion_email import ordinary_completion_participant_evidence
from omo_manager.omo_completion_email import plan_sent_recovery_completion
from omo_manager.omo_completion_email import prepare_ordinary_pending_transition
from omo_manager.omo_completion_email import reconcile_ordinary_sent_completion
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import source1970_eval_evidence
from omo_manager.omo_completion_email import source1970_eval_queue_items
from omo_manager.omo_completion_email import source1970_eval_recovery_request
from omo_manager.omo_completion_email import source1970_eval_resolution_items
from omo_manager.omo_completion_email import source1994_plot_recovery_request
from omo_manager.omo_completion_email import source2003_pangram_recovery_request
from omo_manager.omo_completion_email import watcher_pangram_recovery_request
from omo_manager.omo_completion_email import WATCHER_PANGRAM_EVIDENCE
from omo_manager.omo_completion_email import WATCHER_PANGRAM_ITEMS
from omo_manager.omo_completion_email import MAIL_COMPRESS_EVIDENCE
from omo_manager.omo_completion_email import MAIL_COMPRESS_ITEMS
from omo_manager.omo_completion_email import SOURCE1994_AFTER_QUEUE_SHA256
from omo_manager.omo_completion_email import SOURCE1994_ACK_MESSAGE_ID
from omo_manager.omo_completion_email import SOURCE1994_COMPLETED_ITEMS
from omo_manager.omo_completion_email import SOURCE1994_EVIDENCE
from omo_manager.omo_completion_email import SOURCE1994_ITEMS
from omo_manager.omo_completion_email import SOURCE1994_PATH
from omo_manager.omo_completion_email import SOURCE2003_AFTER_QUEUE_SHA256
from omo_manager.omo_completion_email import SOURCE2003_ITEMS


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
    delivery_transcript: Path | None = None
    delivery_message_file: Path | None = None
    delivery_message_sha256: str = ""


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

# 🧑 Human: "send no email during configuration work ... bound to exact item text/evidence and successful invocation delivery"
SOURCE2048_BATCH_PATH_ITEM = (
    "🧑 Provide a supported owner-local completion path for paper_finish.md to send exactly one final reviewed Human result email and remove exactly the four Source-2048 Human items while retaining the distinct broader review item. "
    "Current omo_pending.py remove accepts only one --item; separate normal calls risk duplicate completion emails, while --no-email is only for recovery after a separately sent answer. "
    "Do not advise manual task mutation or an invented key protocol. Implement or identify the smallest fail-closed batch/combined-answer path with one generated lowercase SHA-256 completion key shared across the one transition as documented, focused realistic tests, and independent review. "
    "Preserve all non-Source-2048 items, send no email during configuration work, and return the exact reviewed owner-local invocation to DeGenTWeb_writeup:0."
)
SOURCE2048_BATCH_PATH_EVIDENCE = (
    "The normal owner-local combined-answer batch path passed focused tests and independent review; its exact invocation was delivered "
    "to DeGenTWeb_writeup:0 and recorded in that Codex session. No Human email was sent and paper_finish.md was not mutated by configuration work."
)
SOURCE2048_PAPER_SESSION = "01a0bb49-92ae-70e2-a0eb-0ea7373862e7"
SOURCE2048_PAPER_CWD = "/ssd1/sichangheagent/DeGenTWeb_writeup"
SOURCE2048_CONFIG_TASK = "watch_err_email.md"
SOURCE2048_DELIVERY_MESSAGE_SHA256 = "ee471f6a3aedffd385dead9215198a2a2a311a148d3dadd3dc89d7ad43a79a2a"
SOURCE2048_PAPER_TRANSCRIPT = Path("2026/09/19/rollout-2026-09-19T13-09-16-01a0bb49-92ae-70e2-a0eb-0ea7373862e7.jsonl")
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
SOURCE2048_DELIVERY_SUPPORT_ITEM = "🧑 Recover the exact failed Source-2048 batch completion delivery for paper_finish.md without duplicate mail or queue mutation outside the supported path. The owner executed the reviewed four-item batch exactly once with completion key 803ba79e684265b9d33fc98159bc0c94366cd93206916a36c5ae5825771dda2e; exit 2 said the verified thread is addressed to dw:15 and DeGenTWeb_writeup:0 may not retag it, then reported uncertain delivery/replay suppressed. No Message-ID was returned and all five queue items remain intact. First establish whether any email was delivered using authoritative sender/Sent evidence. If none was delivered, implement or invoke the smallest authenticated same-key recovery preserving the original Human thread and responsible-owner policy; if delivered, reconcile exact evidence without resending. Do not generate a new key, retag the thread, use ordinary email, send from config scope, remove queue items prematurely, or close the support item as delivered. Obtain independent review and return the exact safe command/evidence to the paper owner."
SOURCE2048_DELIVERY_EVIDENCE = (
    "Owner recovery preserved completion key 803ba79e684265b9d33fc98159bc0c94366cd93206916a36c5ae5825771dda2e and the verified dw:15 thread; "
    "Gmail Sent Mail authenticated Message-ID <179003304739.3930722.4995183145126656828@gmail.com>; the four Source-2048 items were removed and the distinct broader review item remains."
)
SOURCE2048_DELIVERY_MESSAGE_ID = "<179003304739.3930722.4995183145126656828@gmail.com>"
SOURCE2048_SENT_SUBJECT_SHA256 = "b2439c1be93a90abff001abd6a6c39bfaddb49354dfd1722f48301ee549f1aa8"
SOURCE2048_SENT_BODY_SHA256 = "e87ffd70b14f8e41a613740c2c5d3b475e36c8879b4d5ea769c77a19793ef81a"
SOURCE2048_BROADER_ITEM = "Guide the remaining paper review two concrete items at a time: await the original scoring/training procedure for saved Web labels and the preferred search-result emphasis, adapt edits to feedback, integrate retained workers final reviewed outputs, and review abstract/introduction last. Preserve eight body pages and CC2014 true negatives. Source: paper-owner delegation and Human Sources 1944, 1948, 1952, 1963."

SOURCE2057_ROOT = "/ssd1/sichangheagent/work_logs"
SOURCE2057_TASK = "paper_finish.md"
SOURCE2057_SOURCE = "manager_mail/85c5dff58359-2057.txt"
SOURCE2057_SOURCE_SHA256 = "45eb652b8b5519190aeafc9f1754b161fdac823226a99e0ddcd316b492ef4a44"
SOURCE2057_TASK_SHA256 = "8d79b29d973b497fb6966c5c07595481ac1e2bcdd5cd91eecc0346cf05e00c40"
SOURCE2057_OWNER = "DeGenTWeb_writeup:0"
SOURCE2057_MANAGER = "wl:1"
SOURCE2057_AUTHORIZATION = "793e5b4636130347a40928a8ac40990beff9e0737db5fd2f5a3871b3065afd0b"
SOURCE2057_CLAIM_TASK_SHA256 = "6f77fa058a62528624125812baa02aafdd836be09d196d0ae1f6d8cfd9cf46f1"
SOURCE2057_CLAIM_NOTICE_KEY = "196ba3b396c2fc92abc78d4ae9bf3945d8b9c4d14c768106559e40ce8f95b573"
SOURCE2057_CLAIM_SEMANTIC_KEY = "319ffd6d0e5677c23fb28359108853517174c42b6e0720661280819332490132"
SOURCE2057_MESSAGE_ID = "<179004678830.1685206.18344588834047058574@gmail.com>"
SOURCE2057_SUBJECT = "Re: [DeGenTWeb_writeup:0] Try Pangram for hard data"
SOURCE2057_SUBJECT_SHA256 = "2d0f42d96fd91d5b3434512cc18bdee00f37fb52689b66fe519beffa6b1615e7"
SOURCE2057_BODY_SHA256 = "58f10a618150066461f4602c0cf38840134fc851104278742aab089dd524622b"
SOURCE2057_ITEM = (
    "🧑 Source-2057 (manager_mail/85c5dff58359-2057.txt): Is the writeup updated with the figure and wording and pushed? "
    "Is the writeup fully up-to-date with all the changes I requested?"
)
SOURCE2057_PRESERVED_ITEM = SOURCE2048_BROADER_ITEM
SOURCE2057_EVIDENCE = (
    "Source-2057 was answered by exact verified Sent Message-ID "
    "<179004678830.1685206.18344588834047058574@gmail.com>; the figure, caption, and wording are pushed, "
    "the latest Pangram request is complete, and the broader paper review remains open."
)

# 🧑 Human: “Fill in the comments in /shagent/work_logs/dw_progress_2026-09-14_20.md by drafting relevant things, then ping me for review.”
SOURCE2059_ROOT = SOURCE2057_ROOT
SOURCE2059_TASK = SOURCE2057_TASK
SOURCE2059_SOURCE = "manager_mail/85c5dff58359-2059.txt"
SOURCE2059_SOURCE_SHA256 = "37861b7f9e567c78c59a48fa1eb0a9034347e11a8ef066fa58f61d72a4ddecaf"
SOURCE2059_OWNER = SOURCE2057_OWNER
SOURCE2059_MANAGER = SOURCE2057_MANAGER
SOURCE2059_AUTHORIZATION = "1d66346c7ae06e36e996c9c5a1d3e3bd2527405a8198103c2bb253026b5d8e08"
SOURCE2059_CLAIM_TASK_SHA256 = "7402f7eaa92e38f298f26bdfce3ebd20eb201edd44fc09344f0a43feeb7f11cb"
SOURCE2059_CLAIM_NOTICE_KEY = "feff21cdc89e754a23edfee8874799701531ea98e968b57456eb4fb6d6915234"
SOURCE2059_CLAIM_SEMANTIC_KEY = "a96fefc508f41ef1b49fa955434e9ae19e1152800a2cb1f2f79aae93b1391093"
SOURCE2059_MESSAGE_ID = "<179004890105.2317468.7322670588214599687@gmail.com>"
SOURCE2059_SUBJECT = SOURCE2057_SUBJECT
SOURCE2059_SUBJECT_SHA256 = SOURCE2057_SUBJECT_SHA256
SOURCE2059_BODY_SHA256 = "57f9430b1c390e644ed849a7368ee088adad8723382ec87b46b1799f70bd6117"
SOURCE2059_ITEM = (
    "🧑 Source-2059 (manager_mail/85c5dff58359-2059.txt): Fill in the comments in "
    "/shagent/work_logs/dw_progress_2026-09-14_20.md by drafting relevant things, then ping me for review."
)


def source2059_record_payload(canonical_before_sha256: str, recorded_task_sha256: str) -> dict[str, object]:
    """Return the exact add/ack reconciliation payload."""

    return {
        "authorization_sha256": SOURCE2059_AUTHORIZATION,
        "canonical_before_task_sha256": canonical_before_sha256,
        "claim_notice_key": SOURCE2059_CLAIM_NOTICE_KEY,
        "claim_semantic_key": SOURCE2059_CLAIM_SEMANTIC_KEY,
        "claim_task_sha256": SOURCE2059_CLAIM_TASK_SHA256,
        "items": [SOURCE2059_ITEM],
        "message_id": SOURCE2059_MESSAGE_ID,
        "outcome": "recorded; acceptance already delivered",
        "recorded_task_sha256": recorded_task_sha256,
        "sent_body_sha256": SOURCE2059_BODY_SHA256,
        "sent_subject": SOURCE2059_SUBJECT,
        "sent_subject_sha256": SOURCE2059_SUBJECT_SHA256,
        "source": SOURCE2059_SOURCE,
        "source_sha256": SOURCE2059_SOURCE_SHA256,
        "version": "v1",
    }


def source2059_record_comment(canonical_before_sha256: str, recorded_task_sha256: str) -> str:
    """Return the durable exact add/ack reconciliation record."""

    payload = source2059_record_payload(canonical_before_sha256, recorded_task_sha256)
    return "Source-2059 recovery: " + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source2059_record_status(text: str) -> bool | None:
    """Return true for the exact reconstructable record, false for malformed, or none when absent."""

    prefix = "(Source-2059 recovery: "
    records = [line for line in text.splitlines() if "Source-2059 recovery:" in line]
    if not records:
        return None
    if len(records) != 1 or not records[0].startswith(prefix) or not records[0].endswith(")"):
        return False
    try:
        payload = json.loads(records[0][len(prefix) : -1])
    except json.JSONDecodeError:
        return False
    canonical_before_sha256 = payload.get("canonical_before_task_sha256", "") if isinstance(payload, dict) else ""
    recorded_task_sha256 = payload.get("recorded_task_sha256", "") if isinstance(payload, dict) else ""
    if (
        not isinstance(canonical_before_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", canonical_before_sha256) is None
        or not isinstance(recorded_task_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_task_sha256) is None
        or payload != source2059_record_payload(canonical_before_sha256, recorded_task_sha256)
    ):
        return False
    record_line = records[0]
    lines = text.splitlines(keepends=True)
    without_record = "".join(line for line in lines if line.rstrip("\r\n") != record_line)
    if len(lines) - len(without_record.splitlines(keepends=True)) != 1:
        return False
    before_text, removed = remove_pending_items(without_record, (SOURCE2059_ITEM,))
    reconstructed, added = add_pending_items(before_text, (SOURCE2059_ITEM,))
    return (
        removed == 1
        and added == 1
        and reconstructed == without_record
        and hashlib.sha256(before_text.encode()).hexdigest() == canonical_before_sha256
        and hashlib.sha256(without_record.encode()).hexdigest() == recorded_task_sha256
    )


def recover_source2059(root: Path, path: Path) -> int:
    """Record the one already-acknowledged Source-2059 item without email."""

    source = root / SOURCE2059_SOURCE
    if str(root.resolve()) != SOURCE2059_ROOT or path.resolve() != (root / SOURCE2059_TASK).resolve():
        raise BlockingError("Source-2059 recovery requires its exact owner task")
    with task_file_lock(path):
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        metadata = read_task_metadata(path, root)
        record_status = source2059_record_status(text)
        already_recorded = record_status is True
        if (
            metadata is None
            or metadata.version != "v1.0.0"
            or metadata.status != "running"
            or metadata.blocked_on
            or metadata.runat != SOURCE2059_OWNER
            or metadata.managerat != SOURCE2059_MANAGER
            or metadata.is_manager
            or metadata.session_id != SOURCE2048_PAPER_SESSION
            or record_status is False
            or metadata.pending_task_items.count(SOURCE2059_ITEM) != int(already_recorded)
            or metadata.pending_task_items.count(SOURCE2057_PRESERVED_ITEM) != 1
            or not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE2059_SOURCE_SHA256
            or text.splitlines().count("(record and delegate manager_mail/85c5dff58359-2059.txt)") != 1
        ):
            raise BlockingError("Source-2059 owner task or Human authority changed")
        state = completion_email_state_dir()
        with task_file_lock_at_path(state / "completion-email-claims.lock"):
            _ledger, claims, _payload = claims_rows(state)
            expected_claim = [
                SOURCE2059_AUTHORIZATION,
                SOURCE2059_OWNER,
                SOURCE2059_TASK,
                SOURCE2059_MANAGER,
                SOURCE2059_CLAIM_TASK_SHA256,
                SOURCE2059_CLAIM_NOTICE_KEY,
                SOURCE2059_CLAIM_SEMANTIC_KEY,
            ]
            if (
                claims.count(expected_claim) != 1
                or sum(len(row) == 7 and row[0] == SOURCE2059_AUTHORIZATION for row in claims) != 1
                or sum(len(row) == 7 and row[4] == SOURCE2059_CLAIM_TASK_SHA256 for row in claims) != 1
                or sum(len(row) == 7 and row[5] == SOURCE2059_CLAIM_NOTICE_KEY for row in claims) != 1
                or sum(len(row) == 7 and row[6] == SOURCE2059_CLAIM_SEMANTIC_KEY for row in claims) != 1
            ):
                raise BlockingError("Source-2059 completion authorization claim is missing or changed")
            if already_recorded:
                print("Source-2059 was already recorded from exact existing acceptance; no email sent")
                return 0
            participants = ordinary_completion_participant_evidence(
                SOURCE2059_MESSAGE_ID,
                SOURCE2059_SUBJECT_SHA256,
                SOURCE2059_BODY_SHA256,
            )
            if participants is None:
                raise BlockingError("Source-2059 acceptance lacks exact Sent-Mail evidence")
            updated, added = add_pending_items(text, (SOURCE2059_ITEM,))
            if added != 1:
                raise BlockingError("Source-2059 recovery must record its exact Human item once")
            canonical_before, removed = remove_pending_items(updated, (SOURCE2059_ITEM,))
            if removed != 1:
                raise BlockingError("Source-2059 recovery cannot reconstruct its exact queue transition")
            updated = append_comment(
                updated,
                source2059_record_comment(
                    hashlib.sha256(canonical_before.encode()).hexdigest(),
                    hashlib.sha256(updated.encode()).hexdigest(),
                ),
            )
            updated_metadata = parse_task_metadata(updated, root)
            if (
                updated_metadata is None
                or updated_metadata.status != metadata.status
                or updated_metadata.blocked_on != metadata.blocked_on
                or updated_metadata.runat != metadata.runat
                or updated_metadata.managerat != metadata.managerat
                or updated_metadata.session_id != metadata.session_id
                or updated_metadata.pending_task_items != (*metadata.pending_task_items, SOURCE2059_ITEM)
            ):
                raise BlockingError("Source-2059 recovery changed preserved paper state")
            replace_if_unchanged_locked(path, updated, before)
            fsync_task_parent(path)
    print("recorded Source-2059 from exact existing acceptance; no email sent")
    return 0


# 🧑 Human: “Also add a part to mention our trying out the other site generators ... Focus on our objective of generating many sites each with 15 qualified blogs from LLMs and whether it seems feasible in free trial and how much would it cost per site if not using free trial.”
SOURCE2062_ROOT = SOURCE2057_ROOT
SOURCE2062_TASK = SOURCE2057_TASK
SOURCE2062_SOURCE = "manager_mail/85c5dff58359-2062.txt"
SOURCE2062_SOURCE_SHA256 = "1309740b7d8a561b833923a59ef0c32b9212da397f91065fc7c65512e1c7426f"
SOURCE2062_OWNER = SOURCE2057_OWNER
SOURCE2062_MANAGER = SOURCE2057_MANAGER
SOURCE2062_AUTHORIZATION = "993afc3401b89e983827407afb7246ec03be81cefdd1277385245f44ecc08c81"
SOURCE2062_CLAIM_TASK_SHA256 = "3c17e0a8eee7ff781eb447141bd8196195ec928e72f8bca7528a33c0fac03842"
SOURCE2062_CLAIM_NOTICE_KEY = "6b2fcf65e03c22bbe4e292dcd2cdc8b6a83fda8d973393f8458bcfec327ebab1"
SOURCE2062_CLAIM_SEMANTIC_KEY = "224d9871a28dc19d430ba93a641ba38e09f9ddde45ec6a72a58a33a1108f7947"
SOURCE2062_MESSAGE_ID = "<179004942971.2490238.13319295266527919748@gmail.com>"
SOURCE2062_SUBJECT = SOURCE2057_SUBJECT
SOURCE2062_SUBJECT_SHA256 = SOURCE2057_SUBJECT_SHA256
SOURCE2062_BODY_SHA256 = "391985c728fc86e67ea3b288d960df6dfd04d0dcfeca1225a9937ee8b765e679"
SOURCE2062_ITEM = (
    "🧑 Source-2062 (manager_mail/85c5dff58359-2062.txt): Add to the September 14–20 memo what we know about Hostinger, "
    "Framer, 10Web, and Durable, focusing on generating many sites with 15 qualifying LLM-written blogs each, free-trial feasibility, "
    "and paid cost per site; preserve the supplied observed outcomes and incomplete tests."
)


def source2062_record_payload(canonical_before_sha256: str, recorded_task_sha256: str) -> dict[str, object]:
    """Return the exact add/ack reconciliation payload."""

    return {
        "authorization_sha256": SOURCE2062_AUTHORIZATION,
        "canonical_before_task_sha256": canonical_before_sha256,
        "claim_notice_key": SOURCE2062_CLAIM_NOTICE_KEY,
        "claim_semantic_key": SOURCE2062_CLAIM_SEMANTIC_KEY,
        "claim_task_sha256": SOURCE2062_CLAIM_TASK_SHA256,
        "items": [SOURCE2062_ITEM],
        "message_id": SOURCE2062_MESSAGE_ID,
        "outcome": "recorded; acceptance already delivered",
        "recorded_task_sha256": recorded_task_sha256,
        "sent_body_sha256": SOURCE2062_BODY_SHA256,
        "sent_subject": SOURCE2062_SUBJECT,
        "sent_subject_sha256": SOURCE2062_SUBJECT_SHA256,
        "source": SOURCE2062_SOURCE,
        "source_sha256": SOURCE2062_SOURCE_SHA256,
        "version": "v1",
    }


def source2062_record_comment(canonical_before_sha256: str, recorded_task_sha256: str) -> str:
    """Return the durable exact add/ack reconciliation record."""

    payload = source2062_record_payload(canonical_before_sha256, recorded_task_sha256)
    return "Source-2062 recovery: " + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def source2062_record_status(text: str) -> bool | None:
    """Return true for the exact reconstructable record, false for malformed, or none when absent."""

    prefix = "(Source-2062 recovery: "
    records = [line for line in text.splitlines() if "Source-2062 recovery:" in line]
    if not records:
        return None
    if len(records) != 1 or not records[0].startswith(prefix) or not records[0].endswith(")"):
        return False
    try:
        payload = json.loads(records[0][len(prefix) : -1])
    except json.JSONDecodeError:
        return False
    canonical_before_sha256 = payload.get("canonical_before_task_sha256", "") if isinstance(payload, dict) else ""
    recorded_task_sha256 = payload.get("recorded_task_sha256", "") if isinstance(payload, dict) else ""
    if (
        not isinstance(canonical_before_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", canonical_before_sha256) is None
        or not isinstance(recorded_task_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_task_sha256) is None
        or payload != source2062_record_payload(canonical_before_sha256, recorded_task_sha256)
    ):
        return False
    record_line = records[0]
    lines = text.splitlines(keepends=True)
    without_record = "".join(line for line in lines if line.rstrip("\r\n") != record_line)
    if len(lines) - len(without_record.splitlines(keepends=True)) != 1:
        return False
    before_text, removed = remove_pending_items(without_record, (SOURCE2062_ITEM,))
    reconstructed, added = add_pending_items(before_text, (SOURCE2062_ITEM,))
    return (
        removed == 1
        and added == 1
        and reconstructed == without_record
        and hashlib.sha256(before_text.encode()).hexdigest() == canonical_before_sha256
        and hashlib.sha256(without_record.encode()).hexdigest() == recorded_task_sha256
    )


def recover_source2062(root: Path, path: Path) -> int:
    """Record the one already-acknowledged Source-2062 item without email."""

    source = root / SOURCE2062_SOURCE
    if str(root.resolve()) != SOURCE2062_ROOT or path.resolve() != (root / SOURCE2062_TASK).resolve():
        raise BlockingError("Source-2062 recovery requires its exact owner task")
    with task_file_lock(path):
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        metadata = read_task_metadata(path, root)
        record_status = source2062_record_status(text)
        already_recorded = record_status is True
        prerequisite_text = text
        if already_recorded:
            record_line = next(line for line in text.splitlines() if line.startswith("(Source-2062 recovery: "))
            prerequisite_text = "".join(
                line for line in text.splitlines(keepends=True) if line.rstrip("\r\n") != record_line
            )
            prerequisite_text, removed = remove_pending_items(prerequisite_text, (SOURCE2062_ITEM,))
            if removed != 1:
                raise BlockingError("Source-2062 recovery record cannot reconstruct its prerequisite")
        if (
            metadata is None
            or metadata.version != "v1.0.0"
            or metadata.status != "running"
            or metadata.blocked_on
            or metadata.runat != SOURCE2062_OWNER
            or metadata.managerat != SOURCE2062_MANAGER
            or metadata.is_manager
            or metadata.session_id != SOURCE2048_PAPER_SESSION
            or record_status is False
            or metadata.pending_task_items.count(SOURCE2062_ITEM) != int(already_recorded)
            or metadata.pending_task_items.count(SOURCE2057_PRESERVED_ITEM) != 1
            or metadata.pending_task_items.count(SOURCE2059_ITEM) != 1
            or source2059_record_status(prerequisite_text) is not True
            or not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE2062_SOURCE_SHA256
            or text.splitlines().count("(record and delegate manager_mail/85c5dff58359-2062.txt)") != 1
        ):
            raise BlockingError("Source-2062 owner task or Human authority changed")
        state = completion_email_state_dir()
        with task_file_lock_at_path(state / "completion-email-claims.lock"):
            _ledger, claims, _payload = claims_rows(state)
            expected_claim = [
                SOURCE2062_AUTHORIZATION,
                SOURCE2062_OWNER,
                SOURCE2062_TASK,
                SOURCE2062_MANAGER,
                SOURCE2062_CLAIM_TASK_SHA256,
                SOURCE2062_CLAIM_NOTICE_KEY,
                SOURCE2062_CLAIM_SEMANTIC_KEY,
            ]
            if (
                claims.count(expected_claim) != 1
                or sum(len(row) == 7 and row[0] == SOURCE2062_AUTHORIZATION for row in claims) != 1
                or sum(len(row) == 7 and row[4] == SOURCE2062_CLAIM_TASK_SHA256 for row in claims) != 1
                or sum(len(row) == 7 and row[5] == SOURCE2062_CLAIM_NOTICE_KEY for row in claims) != 1
                or sum(len(row) == 7 and row[6] == SOURCE2062_CLAIM_SEMANTIC_KEY for row in claims) != 1
            ):
                raise BlockingError("Source-2062 completion authorization claim is missing or changed")
            if already_recorded:
                print("Source-2062 was already recorded from exact existing acceptance; no email sent")
                return 0
            participants = ordinary_completion_participant_evidence(
                SOURCE2062_MESSAGE_ID,
                SOURCE2062_SUBJECT_SHA256,
                SOURCE2062_BODY_SHA256,
            )
            if participants is None:
                raise BlockingError("Source-2062 acceptance lacks exact Sent-Mail evidence")
            updated, added = add_pending_items(text, (SOURCE2062_ITEM,))
            if added != 1:
                raise BlockingError("Source-2062 recovery must record its exact Human item once")
            canonical_before, removed = remove_pending_items(updated, (SOURCE2062_ITEM,))
            if removed != 1:
                raise BlockingError("Source-2062 recovery cannot reconstruct its exact queue transition")
            updated = append_comment(
                updated,
                source2062_record_comment(
                    hashlib.sha256(canonical_before.encode()).hexdigest(),
                    hashlib.sha256(updated.encode()).hexdigest(),
                ),
            )
            updated_metadata = parse_task_metadata(updated, root)
            if (
                updated_metadata is None
                or updated_metadata.status != metadata.status
                or updated_metadata.blocked_on != metadata.blocked_on
                or updated_metadata.runat != metadata.runat
                or updated_metadata.managerat != metadata.managerat
                or updated_metadata.session_id != metadata.session_id
                or updated_metadata.pending_task_items != (*metadata.pending_task_items, SOURCE2062_ITEM)
            ):
                raise BlockingError("Source-2062 recovery changed preserved paper state")
            replace_if_unchanged_locked(path, updated, before)
            fsync_task_parent(path)
    print("recorded Source-2062 from exact existing acceptance; no email sent")
    return 0


def source2057_record_comment() -> str:
    """Return the durable exact add/answer reconciliation record."""

    payload = {
        "authorization_sha256": SOURCE2057_AUTHORIZATION,
        "before_task_sha256": SOURCE2057_TASK_SHA256,
        "claim_notice_key": SOURCE2057_CLAIM_NOTICE_KEY,
        "claim_semantic_key": SOURCE2057_CLAIM_SEMANTIC_KEY,
        "claim_task_sha256": SOURCE2057_CLAIM_TASK_SHA256,
        "evidence": SOURCE2057_EVIDENCE,
        "items": [SOURCE2057_ITEM],
        "message_id": SOURCE2057_MESSAGE_ID,
        "outcome": "recorded and resolved",
        "sent_body_sha256": SOURCE2057_BODY_SHA256,
        "sent_subject": SOURCE2057_SUBJECT,
        "sent_subject_sha256": SOURCE2057_SUBJECT_SHA256,
        "source": SOURCE2057_SOURCE,
        "source_sha256": SOURCE2057_SOURCE_SHA256,
        "version": "v1",
    }
    return "Source-2057 recovery: " + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def recover_source2057(root: Path, path: Path) -> int:
    """Record and resolve the one already-answered Source-2057 item without email."""

    source = root / SOURCE2057_SOURCE
    if str(root.resolve()) != SOURCE2057_ROOT or path.resolve() != (root / SOURCE2057_TASK).resolve():
        raise BlockingError("Source-2057 recovery requires its exact owner task")
    with task_file_lock(path):
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        metadata = read_task_metadata(path, root)
        if (
            metadata is None
            or metadata.version != "v1.0.0"
            or metadata.status != "long_running"
            or metadata.blocked_on != "human"
            or metadata.runat != SOURCE2057_OWNER
            or metadata.managerat != SOURCE2057_MANAGER
            or metadata.is_manager
            or metadata.pending_task_items != (SOURCE2057_PRESERVED_ITEM,)
            or hashlib.sha256(text.encode()).hexdigest() != SOURCE2057_TASK_SHA256
            or not source.is_file()
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE2057_SOURCE_SHA256
            or text.splitlines().count("(record and delegate manager_mail/85c5dff58359-2057.txt)") != 1
        ):
            raise BlockingError("Source-2057 owner task or Human authority changed")
        state = completion_email_state_dir()
        with task_file_lock_at_path(state / "completion-email-claims.lock"):
            _ledger, claims, _payload = claims_rows(state)
            expected_claim = [
                SOURCE2057_AUTHORIZATION,
                SOURCE2057_OWNER,
                SOURCE2057_TASK,
                SOURCE2057_MANAGER,
                SOURCE2057_CLAIM_TASK_SHA256,
                SOURCE2057_CLAIM_NOTICE_KEY,
                SOURCE2057_CLAIM_SEMANTIC_KEY,
            ]
            if claims.count(expected_claim) != 1 or sum(row[0] == SOURCE2057_AUTHORIZATION for row in claims) != 1:
                raise BlockingError("Source-2057 completion authorization claim is missing or changed")
            participants = ordinary_completion_participant_evidence(
                SOURCE2057_MESSAGE_ID,
                SOURCE2057_SUBJECT_SHA256,
                SOURCE2057_BODY_SHA256,
            )
            if participants is None:
                raise BlockingError("Source-2057 answer lacks exact Sent-Mail evidence")
            updated, added = add_pending_items(text, (SOURCE2057_ITEM,))
            if added != 1:
                raise BlockingError("Source-2057 recovery must record its exact Human item once")
            updated, removed = remove_pending_items(updated, (SOURCE2057_ITEM,))
            if removed != 1:
                raise BlockingError("Source-2057 recovery must resolve its exact Human item once")
            updated = append_comment(updated, source2057_record_comment())
            updated_metadata = parse_task_metadata(updated, root)
            if (
                updated_metadata is None
                or updated_metadata.status != "long_running"
                or updated_metadata.blocked_on != "human"
                or updated_metadata.pending_task_items != (SOURCE2057_PRESERVED_ITEM,)
            ):
                raise BlockingError("Source-2057 recovery changed preserved paper state")
            replace_if_unchanged_locked(path, updated, before)
            fsync_task_parent(path)
    print("recorded and resolved Source-2057 from exact Sent evidence; no email sent")
    return 0


def verify_source2048_invocation_delivery(args: Args) -> None:
    transcript_arg = args.delivery_transcript
    message_path = args.delivery_message_file
    if transcript_arg is None or message_path is None:
        raise BlockingError("Source-2048 recovery requires its delivery transcript and message file")
    if not message_path.is_file() or message_path.is_symlink():
        raise BlockingError("Source-2048 delivery message must be a regular file")
    message_bytes = message_path.read_bytes()
    message = message_bytes.decode().rstrip()
    if args.delivery_message_sha256 != SOURCE2048_DELIVERY_MESSAGE_SHA256 or hashlib.sha256(message.encode()).hexdigest() != SOURCE2048_DELIVERY_MESSAGE_SHA256:
        raise BlockingError("Source-2048 delivery message digest changed")
    sessions_root = CODEX_SESSIONS_ROOT.resolve()
    transcript = transcript_arg.resolve()
    if not transcript_arg.is_file() or transcript_arg.is_symlink() or transcript_arg != transcript or sessions_root not in transcript.parents:
        raise BlockingError("Source-2048 delivery transcript must be an exact canonical Codex session path")
    if transcript != sessions_root / SOURCE2048_PAPER_TRANSCRIPT:
        raise BlockingError("Source-2048 delivery transcript session does not match")
    expected_message = f'<agent_message from="config:4">\n{message}\n</agent_message>'
    session_meta = 0
    deliveries = 0
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BlockingError("Source-2048 delivery transcript is malformed") from exc
        payload = record.get("payload", {})
        if record.get("type") == "session_meta" and payload.get("id") == SOURCE2048_PAPER_SESSION and payload.get("cwd") == SOURCE2048_PAPER_CWD:
            session_meta += 1
        if record.get("type") != "response_item" or payload.get("role") != "user":
            continue
        content = payload.get("content")
        if isinstance(content, list) and any(part.get("type") == "input_text" and part.get("text") == expected_message for part in content if isinstance(part, dict)):
            deliveries += 1
    if session_meta != 1 or deliveries != 1:
        raise BlockingError("Source-2048 invocation delivery requires one exact paper session and one exact delivered message")


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
            "one or more exact Human-authored pending items with one email, repeat --item and pass both --answer-subject-file and "
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
    reconcile = sub.add_parser(
        "reconcile-sent-remove",
        help="Verify an already-sent Human answer, then remove its exact completed pending items without another email.",
    )
    reconcile.add_argument("--item", action="append", required=True)
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--completion-key", required=True)
    reconcile.add_argument("--message-id", required=True)
    reconcile.add_argument("--sent-subject-sha256", required=True)
    reconcile.add_argument("--sent-body-sha256", required=True)
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
        "recover-source1994-plot",
        help="Record six Source-1994 items and resolve only the five covered by the existing result, without sending email.",
    )
    _ = sub.add_parser(
        "recover-source2003-pangram",
        help="Record the three acknowledged Source-2003 Pangram items without sending email.",
    )
    _ = sub.add_parser(
        "recover-watcher-pangram-reviewed-sent",
        help="Apply the one reviewed-Sent Pangram watcher cleanup without sending email.",
    )
    _ = sub.add_parser(
        "recover-mail-compress-reviewed-sent",
        help="Apply the stopped mailbox-compression reviewed-Sent reconciliation without sending email.",
    )
    source2048 = sub.add_parser(
        "recover-source2048-batch-path",
        help="Remove the delivered Source-2048 configuration-helper item without sending email.",
    )
    source2048.add_argument("--delivery-transcript", type=Path, required=True)
    source2048.add_argument("--delivery-message-file", type=Path, required=True)
    source2048.add_argument("--delivery-message-sha256", required=True)
    _ = sub.add_parser(
        "recover-source2048-batch-delivery",
        help="Reconcile the authenticated Source-2048 result and exact support item without email.",
    )
    _ = sub.add_parser(
        "recover-source2057",
        help="Record and resolve the exact already-sent Source-2057 answer without email.",
    )
    _ = sub.add_parser(
        "recover-source2059",
        help="Record the exact already-acknowledged Source-2059 item without email.",
    )
    _ = sub.add_parser(
        "recover-source2062",
        help="Record the exact already-acknowledged Source-2062 item without email.",
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
        items = normalized_items(tuple(parsed.item or ()))
        if len(set(items)) != len(items):
            parser.error("remove requires unique pending items.")
        if parsed.answer_subject_file and parsed.item_id:
            parser.error("combined Human answer requires legacy --item removal.")
        if parsed.answer_subject_file and human_authored_pending_items(items) != items:
            parser.error("combined Human answer requires only Human-authored pending items.")
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
    if parsed.command == "reconcile-sent-remove":
        digests = (parsed.completion_key, parsed.sent_subject_sha256, parsed.sent_body_sha256)
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests):
            parser.error("completion and Sent-Mail digest values must be lowercase SHA-256 digests.")
        if re.fullmatch(r"<[^<>\s]+>", parsed.message_id) is None:
            parser.error("--message-id must be an exact RFC Message-ID enclosed in angle brackets.")
        items = normalized_items(tuple(parsed.item))
        if len(set(items)) != len(items):
            parser.error("reconcile-sent-remove requires unique pending items.")
        if human_authored_pending_items(items) != items:
            parser.error("reconcile-sent-remove supports only Human-authored pending items.")
        return Args(
            parsed.command,
            items,
            evidence=normalized_comment_message(parsed.evidence),
            completion_key=parsed.completion_key,
            message_id=parsed.message_id,
            sent_subject_sha256=parsed.sent_subject_sha256,
            sent_body_sha256=parsed.sent_body_sha256,
        )
    if parsed.command == "recover-source1970-eval":
        return Args(parsed.command, source1970_eval_queue_items(), evidence=source1970_eval_evidence())
    if parsed.command == "recover-source1994-plot":
        return Args(parsed.command, SOURCE1994_ITEMS, evidence=SOURCE1994_EVIDENCE)
    if parsed.command == "recover-source2003-pangram":
        return Args(parsed.command, SOURCE2003_ITEMS)
    if parsed.command == "recover-watcher-pangram-reviewed-sent":
        return Args(parsed.command, WATCHER_PANGRAM_ITEMS, evidence=WATCHER_PANGRAM_EVIDENCE)
    if parsed.command == "recover-mail-compress-reviewed-sent":
        return Args(parsed.command, MAIL_COMPRESS_ITEMS, evidence=MAIL_COMPRESS_EVIDENCE)
    if parsed.command == "recover-source2048-batch-path":
        if re.fullmatch(r"[0-9a-f]{64}", parsed.delivery_message_sha256) is None:
            parser.error("--delivery-message-sha256 must be a lowercase SHA-256 digest.")
        return Args(
            parsed.command,
            (SOURCE2048_BATCH_PATH_ITEM,),
            evidence=SOURCE2048_BATCH_PATH_EVIDENCE,
            delivery_transcript=parsed.delivery_transcript,
            delivery_message_file=parsed.delivery_message_file,
            delivery_message_sha256=parsed.delivery_message_sha256,
        )
    if parsed.command == "recover-source2048-batch-delivery":
        return Args(
            parsed.command,
            (SOURCE2048_DELIVERY_SUPPORT_ITEM,),
            evidence=SOURCE2048_DELIVERY_EVIDENCE,
        )
    if parsed.command == "recover-source2057":
        return Args(parsed.command, (SOURCE2057_ITEM,), evidence=SOURCE2057_EVIDENCE)
    if parsed.command == "recover-source2059":
        return Args(parsed.command, (SOURCE2059_ITEM,))
    if parsed.command == "recover-source2062":
        return Args(parsed.command, (SOURCE2062_ITEM,))
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


PENDING_ADD_NOTICE_RE = re.compile(r"(?m)^\(pending item creation notice: ([0-9a-f]{64}):([0-9a-f]{64})\)$")


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


def source1994_pending_record_comment(item: str) -> str:
    """Return one durable exact-item record for the failed Source-1994 add operation."""

    return f"recorded pending item from Human Source-1994 authority {SOURCE1994_PATH} after acknowledgement Message-ID {SOURCE1994_ACK_MESSAGE_ID}: {item}"


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
        "recover-source1994-plot",
        "recover-source2003-pangram",
        "recover-watcher-pangram-reviewed-sent",
        "recover-mail-compress-reviewed-sent",
    }:
        raise BlockingError("only authenticated incident recovery adapters are supported")
    if args.command == "recover-source1970-eval":
        return source1970_eval_recovery_request()
    if args.command == "recover-source1994-plot":
        return source1994_plot_recovery_request()
    if args.command == "recover-source2003-pangram":
        return source2003_pangram_recovery_request()
    if args.command == "recover-watcher-pangram-reviewed-sent":
        return watcher_pangram_recovery_request()
    if args.command == "recover-mail-compress-reviewed-sent":
        return mail_compress_recovery_request()
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
        "recover-source1994-plot",
        "recover-source2003-pangram",
        "recover-watcher-pangram-reviewed-sent",
        "recover-mail-compress-reviewed-sent",
    }:
        raise BlockingError("only authenticated incident recovery adapters are supported")
    source2003_add = args.command == "recover-source2003-pangram"
    outcome = "pending item created" if source2003_add else "pending item removed after verification"
    request = sent_recovery_request(args)
    if args.command == "recover-source1970-eval":
        resolution_items = source1970_eval_resolution_items()
    elif args.command == "recover-source1994-plot":
        resolution_items = SOURCE1994_COMPLETED_ITEMS
    else:
        resolution_items = args.items
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
            if source2003_add:
                print("replayed committed Source-2003 recovery: recorded 3 Human items; no email sent")
            elif args.command == "recover-source1994-plot":
                print("replayed committed Source-1994 recovery: recorded 6, reconciled 5, 1 remains; no email sent")
            else:
                print(f"replayed committed {args.command} for {len(args.items)} pending item(s); no email sent")
            return 0
        if current_queue_sha256 != request.expected_queue_sha256:
            raise BlockingError("Sent recovery ordered live queue changed")
        if ordinary_pending_purpose(outcome, resolution_items, args.evidence) != request.purpose_sha256:
            raise BlockingError("Sent recovery purpose digest changed")
        subset_recoveries = {
            "recover-source1994-plot",
            "recover-watcher-pangram-reviewed-sent",
            "recover-mail-compress-reviewed-sent",
        }
        if not source2003_add and args.command not in subset_recoveries and metadata.pending_task_items != args.items:
            raise BlockingError("Sent recovery removal must cover the complete ordered live queue")
        if source2003_add:
            updated, count = add_pending_items(text, args.items)
        elif args.command == "recover-source1994-plot":
            updated, added_count = add_pending_items(text, args.items)
            if added_count != len(SOURCE1994_ITEMS):
                raise BlockingError("Source-1994 recovery must record all six exact items once")
            for item in SOURCE1994_ITEMS:
                updated = append_comment(updated, source1994_pending_record_comment(item))
            updated, count = remove_pending_items(updated, resolution_items)
        else:
            updated, count = remove_pending_items(text, args.items)
        if not source2003_add:
            updated = append_comment(updated, pending_remove_evidence_comment(count, args.evidence))
        updated_metadata = parse_task_metadata(updated, root)
        if updated_metadata is None:
            raise TaskFrontmatterError("updated pending queue metadata is invalid")
        after_queue_sha256 = pending_queue_sha256(updated_metadata.pending_task_items)
        if source2003_add and (count != len(SOURCE2003_ITEMS) or updated_metadata.pending_task_items != SOURCE2003_ITEMS or after_queue_sha256 != SOURCE2003_AFTER_QUEUE_SHA256):
            raise BlockingError("Source-2003 recovery must record all three exact items once")
        if args.command == "recover-source1994-plot" and (
            count != len(SOURCE1994_COMPLETED_ITEMS) or updated_metadata.pending_task_items != (SOURCE1994_ITEMS[-1],) or after_queue_sha256 != SOURCE1994_AFTER_QUEUE_SHA256
        ):
            raise BlockingError("Source-1994 recovery did not preserve its exact one-item review queue")
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
        if source2003_add:
            print("recorded 3 Source-2003 Human pending items; no email sent")
        elif args.command == "recover-source1994-plot":
            print("recorded 6 Source-1994 pending items, reconciled 5 completed items, and left 1 awaiting Human approval; no email sent")
        else:
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
        if args.item_id:
            raise ValueError("combined Human answer requires legacy --item removal")
        if human_authored_pending_items(args.items) != args.items:
            raise ValueError("combined Human answer requires only Human-authored pending items")
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
        if args.command == "recover-source2057":
            return recover_source2057(root, path)
        if args.command == "recover-source2059":
            return recover_source2059(root, path)
        if args.command == "recover-source2062":
            return recover_source2062(root, path)
        if args.command in {
            "recover-source1990-pangram",
            "recover-source1970-eval",
            "recover-source1994-plot",
            "recover-source2003-pangram",
            "recover-watcher-pangram-reviewed-sent",
            "recover-mail-compress-reviewed-sent",
        }:
            return recover_sent_pending_transition(args, root, path)
        answer_subject, answer_body = human_answer(args)
        if args.command == "reconcile-sent-remove":
            if current.version != "v1.0.0":
                raise BlockingError("already-sent pending removal currently requires a legacy queue")
            _updated, count = remove_pending_items(text, args.items)
            if count != len(args.items):
                raise BlockingError("already-sent pending removal requires every item exactly once")
            # 🧑 Human: "Do not resend the Human answer."
            reconcile_ordinary_sent_completion(
                root,
                path,
                "pending item removed after verification",
                args.message_id,
                args.sent_subject_sha256,
                args.sent_body_sha256,
                items=args.items,
                evidence=args.evidence,
                semantic_key=args.completion_key,
                pending_item_owner=True,
            )
            args = replace(args, command="remove")
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
        if args.command == "recover-source2048-batch-path":
            if current.version != "v1.0.0" or path.resolve() != (root / SOURCE2048_CONFIG_TASK).resolve():
                raise BlockingError("Source-2048 batch-path recovery requires its exact legacy configuration task")
            if args.items != (SOURCE2048_BATCH_PATH_ITEM,) or args.evidence != SOURCE2048_BATCH_PATH_EVIDENCE:
                raise BlockingError("Source-2048 batch-path recovery bindings changed")
            updated, count = remove_pending_items(text, args.items)
            if count != 1:
                raise BlockingError("Source-2048 batch-path recovery requires its exact pending item once")
            verify_source2048_invocation_delivery(args)
            updated = append_comment(updated, pending_remove_evidence_comment(1, args.evidence))
            replace_if_unchanged(path, updated, before)
            print("reconciled the delivered Source-2048 batch path; no email sent")
            return 0
        if args.command == "recover-source2048-batch-delivery":
            if current.version != "v1.0.0" or path.resolve() != (root / SOURCE2048_CONFIG_TASK).resolve():
                raise BlockingError("Source-2048 delivery recovery requires its exact legacy configuration task")
            if args.items != (SOURCE2048_DELIVERY_SUPPORT_ITEM,) or args.evidence != SOURCE2048_DELIVERY_EVIDENCE:
                raise BlockingError("Source-2048 delivery recovery bindings changed")
            paper = root / "paper_finish.md"
            if (
                ordinary_completion_participant_evidence(
                    SOURCE2048_DELIVERY_MESSAGE_ID,
                    SOURCE2048_SENT_SUBJECT_SHA256,
                    SOURCE2048_SENT_BODY_SHA256,
                )
                is None
            ):
                raise BlockingError("Source-2048 exact Sent-Mail evidence is missing")
            updated, count = remove_pending_items(text, args.items)
            if count != 1:
                raise BlockingError("Source-2048 delivery recovery requires its exact support item once")
            updated = append_comment(updated, pending_remove_evidence_comment(1, args.evidence))
            with task_file_lock(paper):
                paper_metadata = read_task_metadata(paper, root)
                if paper_metadata is None or paper_metadata.pending_task_items != (SOURCE2048_BROADER_ITEM,):
                    raise BlockingError("Source-2048 paper queue does not retain exactly the broader review item")
                replace_if_unchanged(path, updated, before)
            print("reconciled the authenticated Source-2048 delivery support item; no email sent")
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
                semantic_key, delivered = delivered_pending_add_notice(root, path, text, requested_notice_items) if requested_notice_items else ("", False)
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
            semantic_key, delivered = delivered_pending_add_notice(root, path, text, requested_notice_items) if requested_notice_items else ("", False)
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
        if answer_subject and count != len(args.items):
            raise BlockingError("combined Human answer requires every pending item exactly once")
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
