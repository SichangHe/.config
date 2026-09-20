from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending
from omo_manager import omo_completion_email
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_completion_email import build_completion_email
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import reconcile_delivered_completion
from omo_manager.omo_completion_email import reconcile_ordinary_sent_completion
from omo_manager.omo_completion_email import refresh_unattempted_completion_claim
from omo_manager.omo_completion_email import require_completion_entrypoint
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import main
from omo_manager.omo_completion_email import mark_completion_email_delivered
from omo_manager.omo_completion_email import mark_completion_email_request_queued
from omo_manager.omo_completion_email import OrdinaryPendingRecoveryRequest
from omo_manager.omo_completion_email import ordinary_pending_purpose
from omo_manager.omo_completion_email import plan_sent_recovery_completion
from omo_manager.omo_completion_email import prepare_ordinary_pending_transition
from omo_manager.omo_completion_email import commit_ordinary_pending_transition
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_completion_email import validate_completion_notice_delivery
from omo_manager.omo_completion_email import verify_ordinary_completion_in_sent
from omo_manager.omo_completion_email import SOURCE1241_ENVELOPE
from omo_manager.omo_completion_email import SOURCE1241_CONTEXT
from omo_manager.omo_completion_email import SOURCE1241_HUMAN
from omo_manager.omo_completion_email import SOURCE1241_META_LINE


def task_text(body: str = "", *, human_report: bool = True) -> str:
    human_report_line = "Report results directly to the Human.\n" if human_report else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: running\n"
        "runat: cfg:2\n"
        "tool: codex\n"
        "managerat: cfg:1\n"
        "is_manager: false\n"
        "pending_task_items:\n"
        "  - finish review\n"
        "---\n"
        f"{human_report_line}"
        f"{body}\n"
    )


def source1241_task(
    root: Path,
    *,
    body_suffix: str = "",
    source_text: str = SOURCE1241_HUMAN,
    human_report: bool = True,
) -> tuple[Path, str]:
    source = root / "manager_mail/85c5dff58359-1241.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text(source_text + "\n", encoding="utf-8")
    source.chmod(0o600)
    task = root / "hmanager_replace_fix.md"
    text = task_text(f"{SOURCE1241_CONTEXT}{body_suffix}", human_report=human_report)
    task.write_text(text, encoding="utf-8")
    return task, text


class CompletionEmailTest(unittest.TestCase):
    def source1970_fixture(self, root: Path, state: Path) -> tuple[Path, str, dict[str, object], tuple[str, ...]]:
        items = omo_completion_email.SOURCE1970_LIVE_ITEMS
        rendered_items = "".join(f"  - '{item.replace(chr(39), chr(39) * 2)}'\n" for item in items)
        text = (
            "---\n"
            "version: v1.0.0\n"
            "status: blocked\n"
            "blocked_on: test Source-1970 recovery\n"
            "runat: dw:58\n"
            "tool: codex\n"
            "managerat: dw:60\n"
            "is_manager: false\n"
            "pending_task_items:\n"
            f"{rendered_items}"
            "---\n"
            "Report results directly to the Human.\n"
        )
        task = root / omo_completion_email.SOURCE1970_TASK
        task.write_text(text, encoding="utf-8")
        delivered: list[tuple[str, str, str, str, str, str]] = []
        used_dir = state / "completion-email-authorization-used"
        used_dir.mkdir(mode=0o700, parents=True)
        for index, item in enumerate(items):
            semantic_key = hashlib.sha256(f"source1970-delivered-{index}".encode()).hexdigest()
            plan = build_completion_email(
                root,
                task,
                text,
                "pending item created",
                items=(item,),
                semantic_key=semantic_key,
            )
            assert plan is not None
            self.assertTrue(claim_completion_email(plan))
            used = used_dir / plan.key
            used.write_text(f"{plan.target}\t{task.name}\n", encoding="utf-8")
            used.chmod(0o600)
            mark_completion_email_delivered(plan)
            authorization = state / "completion-email-authorizations" / plan.key
            delivered.append(
                (
                    plan.key,
                    plan.task_sha256,
                    plan.manager_target,
                    plan.notice_key,
                    plan.notice_semantic_key,
                    hashlib.sha256(authorization.read_bytes()).hexdigest(),
                )
            )

        def unused_binding(outcome: str, claim_items: tuple[str, ...], semantic_label: str) -> tuple[str, str, str, str, str]:
            plan = build_completion_email(
                root,
                task,
                text,
                outcome,
                items=claim_items,
                semantic_key=hashlib.sha256(semantic_label.encode()).hexdigest(),
            )
            assert plan is not None
            self.assertTrue(claim_completion_email(plan))
            authorization = state / "completion-email-authorizations" / plan.key
            return (
                plan.key,
                plan.task_sha256,
                plan.manager_target,
                plan.notice_semantic_key,
                hashlib.sha256(authorization.read_bytes()).hexdigest(),
            )

        stale_add = unused_binding("pending item created", (omo_completion_email.SOURCE1970_MISSING_ITEM,), "source1970-stale-add")
        stale_removal = unused_binding("pending item completed", (items[0],), "source1970-stale-removal")
        unrelated = unused_binding("pending item completed", (items[1],), "source1970-unrelated")
        constants: dict[str, object] = {
            "SOURCE1970_ROOT": str(root.resolve()),
            "SOURCE1970_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
            "SOURCE1970_BLOCKED_ON": "test Source-1970 recovery",
            "SOURCE1970_DELIVERED_CREATION_CLAIMS": tuple(delivered),
            "SOURCE1970_STALE_ADD_CLAIM": stale_add,
            "SOURCE1970_STALE_REMOVAL_CLAIM": stale_removal,
        }
        return task, text, constants, (stale_add[0], stale_removal[0], unrelated[0], *(binding[0] for binding in delivered))

    def test_source1970_production_preflight_is_read_only_and_reaches_sent(self) -> None:
        root = Path(omo_completion_email.SOURCE1970_ROOT)
        task = root / omo_completion_email.SOURCE1970_TASK
        source = root / omo_completion_email.SOURCE1970_PATH
        state = omo_completion_email.completion_email_state_dir()
        text = task.read_text(encoding="utf-8")
        request = omo_completion_email.source1970_eval_recovery_request()
        outcome = "pending item removed after verification"
        tracked = (task, source, state / "completion-email-claims.tsv")
        before = tuple(path.read_bytes() for path in tracked)
        transition_key = omo_completion_email.ordinary_pending_transition_key(
            root,
            task,
            outcome,
            omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
            omo_completion_email.SOURCE1970_EVIDENCE,
            request,
        )
        message_marker = state / "ordinary-completion-by-message" / hashlib.sha256(request.message_id.encode()).hexdigest()
        state_tracked = (
            message_marker,
            state / "ordinary-pending-transitions" / transition_key,
            state / "ordinary-completion-by-notice" / omo_completion_email.SOURCE1970_NOTICE_KEY,
        )
        state_before = tuple(path.read_bytes() if path.exists() else None for path in state_tracked)
        if hashlib.sha256(text.encode()).hexdigest() == omo_completion_email.SOURCE1970_TASK_SHA256:
            canonical = build_completion_email(
                root,
                task,
                text,
                outcome,
                items=omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                evidence=omo_completion_email.SOURCE1970_EVIDENCE,
                semantic_key=request.semantic_key,
            )
            assert canonical is not None
            plan = replace(canonical, send_allowed=False)
            self.assertFalse(message_marker.exists())
            omo_completion_email.validate_source1970_eval_authority(
                root,
                plan,
                omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                omo_completion_email.SOURCE1970_EVIDENCE,
                request,
                text,
            )
            _ledger, rows, _previous = omo_completion_email.claims_rows(state)
            omo_completion_email.validate_source1970_eval_state(state, plan, request, transition_key, rows)
        else:
            metadata = parse_task_metadata(text, root)
            assert metadata is not None
            self.assertEqual((), metadata.pending_task_items)
            transition = omo_completion_email.load_ordinary_pending_transition(
                root,
                task,
                outcome,
                omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                omo_completion_email.SOURCE1970_EVIDENCE,
                request,
                hashlib.sha256(text.encode()).hexdigest(),
                digest_fields("pending-queue-v1", *metadata.pending_task_items),
            )
            assert transition is not None
            self.assertEqual(transition_key, transition.key)
            recorded = omo_completion_email.read_transition_record(transition.key)
            assert recorded is not None
            values, payload = recorded
            omo_completion_email.validate_ordinary_pending_transition_record(transition_key, values, payload)
            static = omo_completion_email.transition_static_values(
                root,
                task,
                outcome,
                omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                omo_completion_email.SOURCE1970_EVIDENCE,
                request,
            )
            self.assertTrue(all(values.get(name) == value for name, value in static.items()))
            self.assertEqual("committed", values["status"])
            self.assertEqual(values["message_record"], message_marker.read_text(encoding="utf-8"))
            notice_marker = state / "ordinary-completion-by-notice" / values["plan_notice_key"]
            self.assertEqual(values["ordinary_record"], notice_marker.read_text(encoding="utf-8"))
        self.assertTrue(
            verify_ordinary_completion_in_sent(
                request.message_id,
                request.sent_subject_sha256,
                request.sent_body_sha256,
            )
        )
        self.assertEqual(before, tuple(path.read_bytes() for path in tracked))
        self.assertEqual(state_before, tuple(path.read_bytes() if path.exists() else None for path in state_tracked))

    def test_source1970_authority_rejects_task_and_message_drift(self) -> None:
        root = Path(omo_completion_email.SOURCE1970_ROOT)
        task = root / omo_completion_email.SOURCE1970_TASK
        text = task.read_text(encoding="utf-8")
        request = omo_completion_email.source1970_eval_recovery_request()
        canonical = build_completion_email(
            root,
            task,
            text,
            "pending item removed after verification",
            items=omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
            evidence=omo_completion_email.SOURCE1970_EVIDENCE,
            semantic_key=request.semantic_key,
        )
        assert canonical is not None
        plan = replace(canonical, send_allowed=False)
        with self.assertRaisesRegex(OSError, "does not bind"):
            omo_completion_email.validate_source1970_eval_authority(
                root,
                plan,
                omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                omo_completion_email.SOURCE1970_EVIDENCE,
                request,
                text + "drift\n",
            )
        with self.assertRaisesRegex(OSError, "does not bind"):
            omo_completion_email.validate_source1970_eval_authority(
                root,
                plan,
                omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                omo_completion_email.SOURCE1970_EVIDENCE,
                replace(request, message_id="<different@example.test>"),
                text,
            )
        parsed = omo_pending.parse_args(["recover-source1970-eval"])
        self.assertEqual(omo_completion_email.SOURCE1970_LIVE_ITEMS, parsed.items)
        with self.assertRaises(SystemExit):
            omo_pending.parse_args(["recover-source1970-eval", "--message-id", request.message_id])

    def test_source1970_cli_succeeds_replays_preserves_other_claims_and_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, keys = self.source1970_fixture(root, state)
                stale_add, stale_removal, unrelated, *delivered = keys
                before_rows = {
                    row[0]: row
                    for row in (line.split("\t") for line in (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines())
                    if row[0] in {unrelated, *delivered}
                }
                unrelated_authorization = (state / "completion-email-authorizations" / unrelated).read_bytes()
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1970_eval_authority"
                ) as authority, patch(
                    "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
                ), patch(
                    "omo_manager.omo_pending.require_owner_completion"
                ) as owner_sender, patch(
                    "omo_manager.omo_completion_email.send_completion_email"
                ) as sender, patch(
                    "omo_manager.omo_completion_email.subprocess.run"
                ) as email_process:
                    args = omo_pending.parse_args(["recover-source1970-eval"])
                    request = omo_pending.sent_recovery_request(args)
                    plan = plan_sent_recovery_completion(
                        root,
                        task,
                        text,
                        "pending item removed after verification",
                        items=omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                        evidence=omo_completion_email.SOURCE1970_EVIDENCE,
                        semantic_key=request.semantic_key,
                    )
                    assert plan is not None
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual((), metadata.pending_task_items)
                    rows = [line.split("\t") for line in (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines()]
                    current_rows = {row[0]: row for row in rows if row[0] in before_rows}
                    self.assertEqual(before_rows, current_rows)
                    self.assertEqual(unrelated_authorization, (state / "completion-email-authorizations" / unrelated).read_bytes())
                    for key in (stale_add, stale_removal):
                        selected = [row for row in rows if row[0] == key]
                        self.assertEqual(1, len(selected))
                        self.assertTrue(selected[0][1].startswith("retired:"))
                        self.assertTrue((state / "completion-email-retired-authorizations" / key).is_file())
                    transitions = list((state / "ordinary-pending-transitions").iterdir())
                    messages = list((state / "ordinary-completion-by-message").iterdir())
                    self.assertEqual(1, len(transitions))
                    self.assertEqual(1, len(messages))
                    self.assertIn('"status":"committed"', transitions[0].read_text(encoding="utf-8"))
                    self.assertTrue(messages[0].read_text(encoding="utf-8").startswith("transition_key="))
                    self.assertTrue(omo_completion_email.ordinary_completion_is_reconciled(plan))
                    first_snapshot = {
                        path.relative_to(root): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    self.assertEqual(
                        first_snapshot,
                        {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()},
                    )
                authority.assert_called_once_with(
                    root,
                    plan,
                    omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                    omo_completion_email.SOURCE1970_EVIDENCE,
                    request,
                    text,
                )
                owner_sender.assert_not_called()
                sender.assert_not_called()
                email_process.assert_not_called()

    def test_source1970_reconciled_marker_rejects_malformed_or_cross_bound_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, _keys = self.source1970_fixture(root, state)
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1970_eval_authority"
                ) as authority, patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                    args = omo_pending.parse_args(["recover-source1970-eval"])
                    request = omo_pending.sent_recovery_request(args)
                    plan = plan_sent_recovery_completion(
                        root,
                        task,
                        text,
                        "pending item removed after verification",
                        items=omo_completion_email.SOURCE1970_RESOLUTION_ITEMS,
                        evidence=omo_completion_email.SOURCE1970_EVIDENCE,
                        semantic_key=request.semantic_key,
                    )
                    assert plan is not None
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    transition_path = next((state / "ordinary-pending-transitions").iterdir())
                    original_payload = transition_path.read_text(encoding="utf-8")
                    values = json.loads(original_payload)
                    values["request_sha256"] = "0" * 64
                    transition_path.write_text(
                        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(OSError, "canonical record"):
                        omo_completion_email.ordinary_completion_is_reconciled(plan)
                    values = json.loads(original_payload)
                    values["semantic_key"] = "1" * 64
                    cross_request = omo_completion_email.request_from_transition_values(values)
                    values["request_sha256"] = omo_completion_email.recovery_request_sha256(cross_request)
                    cross_key = digest_fields(
                        "ordinary-pending-transition-v1",
                        values["root"],
                        values["task"],
                        values["purpose_sha256"],
                        *cross_request.__dict__.values(),
                    )
                    values["transition_key"] = cross_key
                    values["message_record"] = f"transition_key={cross_key}\n{values['ordinary_record']}"
                    values["message_record_sha256"] = hashlib.sha256(values["message_record"].encode()).hexdigest()
                    cross_path = transition_path.with_name(cross_key)
                    transition_path.rename(cross_path)
                    cross_path.write_text(omo_completion_email.canonical_json_record(values), encoding="utf-8")
                    message_path = next((state / "ordinary-completion-by-message").iterdir())
                    message_path.write_text(values["message_record"], encoding="utf-8")
                    with self.assertRaisesRegex(OSError, "canonical record"):
                        omo_completion_email.ordinary_completion_is_reconciled(plan)
                authority.assert_called_once()

    def test_source1970_composite_message_record_precedes_claim_and_task_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, _keys = self.source1970_fixture(root, state)
                ledger = state / "completion-email-claims.tsv"
                claims_before = ledger.read_bytes()
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1970_eval_authority"
                ) as authority, patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                    args = omo_pending.parse_args(["recover-source1970-eval"])

                    def crash_before_claim_rewrite(_ledger: Path, _previous: str, _rows: list[list[str]]) -> None:
                        messages = list((state / "ordinary-completion-by-message").iterdir())
                        self.assertEqual(1, len(messages))
                        self.assertTrue(messages[0].read_text(encoding="utf-8").startswith("transition_key="))
                        self.assertEqual(text, task.read_text(encoding="utf-8"))
                        raise RuntimeError("crash before claim rewrite")

                    with patch("omo_manager.omo_completion_email.rewrite_claims", side_effect=crash_before_claim_rewrite):
                        with self.assertRaisesRegex(RuntimeError, "crash before claim rewrite"):
                            omo_pending.run(args, root=root)
                    self.assertEqual(text, task.read_text(encoding="utf-8"))
                    self.assertEqual(claims_before, ledger.read_bytes())
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual((), metadata.pending_task_items)
                self.assertEqual(2, authority.call_count)

    def test_source1970_replays_prepared_transition_after_task_replace_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, _text, constants, _keys = self.source1970_fixture(root, state)
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1970_eval_authority"
                ) as authority, patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                    args = omo_pending.parse_args(["recover-source1970-eval"])
                    with patch("omo_manager.omo_pending.fsync_task_parent", side_effect=OSError("crash after task replace")):
                        with self.assertRaisesRegex(OSError, "crash after task replace"):
                            omo_pending.run(args, root=root)
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual((), metadata.pending_task_items)
                    transitions = list((state / "ordinary-pending-transitions").iterdir())
                    self.assertEqual(1, len(transitions))
                    self.assertIn('"status":"prepared"', transitions[0].read_text(encoding="utf-8"))
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    self.assertIn('"status":"committed"', transitions[0].read_text(encoding="utf-8"))
                authority.assert_called_once()

    def test_source1970_rejects_global_message_id_reuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, _keys = self.source1970_fixture(root, state)
                message_dir = state / "ordinary-completion-by-message"
                message_dir.mkdir(mode=0o700)
                message = message_dir / hashlib.sha256(omo_completion_email.SOURCE1970_MESSAGE_ID.encode()).hexdigest()
                message.write_text("different transition\n", encoding="utf-8")
                message.chmod(0o600)
                ledger = state / "completion-email-claims.tsv"
                claims_before = ledger.read_bytes()
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1970_eval_authority"
                ) as authority, patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                    args = omo_pending.parse_args(["recover-source1970-eval"])
                    with self.assertRaisesRegex(OSError, "Message-ID is already bound"):
                        omo_pending.run(args, root=root)
                authority.assert_called_once()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(claims_before, ledger.read_bytes())

    def test_source1990_production_identity_preflight_is_read_only(self) -> None:
        """Production constants authenticate the Pangram incident before or after its one-shot recovery."""

        root = Path("/ssd1/sichangheagent/work_logs")
        task = root / omo_completion_email.SOURCE1990_PANGRAM_TASK
        current = task.read_text(encoding="utf-8")
        request = OrdinaryPendingRecoveryRequest(
            "source1990-pangram-remove",
            omo_completion_email.SOURCE1990_PANGRAM_TASK_SHA256,
            omo_completion_email.SOURCE1990_PANGRAM_QUEUE_SHA256,
            omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
            omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
            *omo_completion_email.SOURCE1990_PANGRAM_PRIMARY_CLAIM,
            omo_completion_email.SOURCE1990_PANGRAM_MESSAGE_ID,
            omo_completion_email.SOURCE1990_PANGRAM_SUBJECT_SHA256,
            omo_completion_email.SOURCE1990_PANGRAM_BODY_SHA256,
            *omo_completion_email.SOURCE1990_PANGRAM_CHURN,
            "",
            *omo_completion_email.SOURCE1990_PANGRAM_EXTRA_CLAIM,
        )
        if hashlib.sha256(current.encode()).hexdigest() == omo_completion_email.SOURCE1990_PANGRAM_TASK_SHA256:
            plan = omo_completion_email.CompletionEmail(
                root,
                task,
                omo_completion_email.SOURCE1990_PANGRAM_OWNER,
                omo_completion_email.SOURCE1990_PANGRAM_MANAGER,
                omo_completion_email.SOURCE1990_PANGRAM_TASK_SHA256,
                "pending item removed after verification",
                "",
                "",
                "preflight",
                "preflight-notice",
                omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
                send_allowed=False,
            )
            omo_completion_email.validate_source1990_pangram_authority(
                root,
                plan,
                omo_completion_email.SOURCE1990_PANGRAM_ITEMS,
                omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE,
                request,
                current,
            )
        else:
            metadata = parse_task_metadata(current, root)
            assert metadata is not None
            self.assertEqual((), metadata.pending_task_items)
            static = omo_completion_email.transition_static_values(
                root,
                task,
                "pending item removed after verification",
                omo_completion_email.SOURCE1990_PANGRAM_ITEMS,
                omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE,
                request,
            )
            recorded = omo_completion_email.read_transition_record(static["transition_key"])
            assert recorded is not None
            values, _payload = recorded
            self.assertTrue(all(values.get(name) == value for name, value in static.items()))
            self.assertEqual("committed", values["status"])
        self.assertTrue(
            verify_ordinary_completion_in_sent(
                request.message_id,
                request.sent_subject_sha256,
                request.sent_body_sha256,
            )
        )

    def test_source1990_adapter_authenticates_real_git_churn_and_rejects_drift(self) -> None:
        """The incident path requires a real before-to-prefix Git transition."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / omo_completion_email.SOURCE1990_PATH
            source.parent.mkdir()
            source.write_text(omo_completion_email.SOURCE1990_TEXT, encoding="utf-8")
            source.chmod(0o600)
            items = omo_completion_email.SOURCE1990_PANGRAM_ITEMS
            task = root / omo_completion_email.SOURCE1990_PANGRAM_TASK
            before = task_text().replace("cfg:2", "dw:15").replace("cfg:1", "dw:60").replace(
                "  - finish review", "".join(f"  - {item}\n" for item in items).rstrip()
            )
            claim_bound = f"{before}manager claim-bound update\n"
            authorized = f"{claim_bound}manager custody update\n"
            current = f"{authorized}human authorization update\n"

            def git(*arguments: str) -> str:
                return subprocess.run(
                    ["git", *arguments], cwd=root, check=True, capture_output=True, text=True, timeout=10
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "test")
            task.write_text(before, encoding="utf-8")
            git("add", task.name)
            git("commit", "-qm", "before")
            task.write_text(claim_bound, encoding="utf-8")
            git("add", task.name)
            git("commit", "-qm", "claim-bound")
            claim_bound_commit = git("rev-parse", "HEAD")
            claim_bound_parent = git("rev-parse", "HEAD^")
            before_blob = git("rev-parse", f"{claim_bound_parent}:{task.name}")
            after_blob = git("rev-parse", f"{claim_bound_commit}:{task.name}")
            diff_sha256 = hashlib.sha256(
                subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--binary", claim_bound_parent, claim_bound_commit, "--", task.name],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=10,
                ).stdout
            ).hexdigest()
            task.write_text(authorized, encoding="utf-8")
            git("add", task.name)
            git("commit", "-qm", "authorized")
            authorized_commit = git("rev-parse", "HEAD")
            authorized_parent = git("rev-parse", "HEAD^")
            authorized_before_blob = git("rev-parse", f"{authorized_parent}:{task.name}")
            authorized_after_blob = git("rev-parse", f"{authorized_commit}:{task.name}")
            authorized_diff_sha256 = hashlib.sha256(
                subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--binary", authorized_parent, authorized_commit, "--", task.name],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=10,
                ).stdout
            ).hexdigest()
            task.write_text(current, encoding="utf-8")
            git("add", task.name)
            git("commit", "-qm", "post-authority")
            post_commit = git("rev-parse", "HEAD")
            post_parent = git("rev-parse", "HEAD^")
            post_before_blob = git("rev-parse", f"{post_parent}:{task.name}")
            post_after_blob = git("rev-parse", f"{post_commit}:{task.name}")
            post_diff_sha256 = hashlib.sha256(
                subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--binary", post_parent, post_commit, "--", task.name],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=10,
                ).stdout
            ).hexdigest()
            primary = ("a" * 64, hashlib.sha256(b"older independent claim").hexdigest(), "dw:60", "b" * 64, "c" * 64)
            extra = ("d" * 64, hashlib.sha256(before.encode()).hexdigest(), "dw:60", "e" * 64, "f" * 64)
            evidence = "reviewed"
            purpose = ordinary_pending_purpose("pending item removed after verification", items, evidence)
            request = OrdinaryPendingRecoveryRequest(
                "source1990-pangram-remove",
                hashlib.sha256(current.encode()).hexdigest(),
                digest_fields("pending-queue-v1", *items),
                purpose,
                purpose,
                *primary,
                "<churn@example.test>",
                "1" * 64,
                "2" * 64,
                claim_bound_commit,
                before_blob,
                after_blob,
                diff_sha256,
                "",
                *extra,
            )
            plan = omo_completion_email.CompletionEmail(
                root,
                task,
                "dw:15",
                "dw:60",
                request.expected_task_sha256,
                "pending item removed after verification",
                "",
                "",
                "key",
                "notice",
                purpose,
                send_allowed=False,
            )
            authority = {
                "SOURCE1990_PANGRAM_ROOT": str(root.resolve()),
                "SOURCE1990_PANGRAM_TASK_SHA256": request.expected_task_sha256,
                "SOURCE1990_PANGRAM_CLAIM_BOUND_TASK_SHA256": hashlib.sha256(claim_bound.encode()).hexdigest(),
                "SOURCE1990_PANGRAM_AUTHORIZED_TASK_SHA256": hashlib.sha256(authorized.encode()).hexdigest(),
                "SOURCE1990_PANGRAM_QUEUE_SHA256": request.expected_queue_sha256,
                "SOURCE1990_PANGRAM_PURPOSE_SHA256": purpose,
                "SOURCE1990_PANGRAM_EVIDENCE": evidence,
                "SOURCE1990_PANGRAM_MESSAGE_ID": request.message_id,
                "SOURCE1990_PANGRAM_SUBJECT_SHA256": request.sent_subject_sha256,
                "SOURCE1990_PANGRAM_BODY_SHA256": request.sent_body_sha256,
                "SOURCE1990_PANGRAM_PRIMARY_CLAIM": primary,
                "SOURCE1990_PANGRAM_EXTRA_CLAIM": extra,
                "SOURCE1990_PANGRAM_CHURN": (claim_bound_commit, before_blob, after_blob, diff_sha256),
                "SOURCE1990_PANGRAM_CUSTODY_CHURN": (
                    authorized_commit,
                    authorized_before_blob,
                    authorized_after_blob,
                    authorized_diff_sha256,
                ),
                "SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN": (post_commit, post_before_blob, post_after_blob, post_diff_sha256),
            }
            with patch.multiple(omo_completion_email, **authority):
                omo_completion_email.validate_source1990_pangram_authority(root, plan, items, evidence, request, current)
                wrong_prefix = f"{current[:-1]}!"
                with self.assertRaisesRegex(OSError, "does not bind"):
                    omo_completion_email.validate_source1990_pangram_authority(root, plan, items, evidence, request, wrong_prefix)
                wrong_blob = replace(request, churn_before_blob="0" * 40)
                with patch.object(
                    omo_completion_email, "SOURCE1990_PANGRAM_CHURN", (claim_bound_commit, wrong_blob.churn_before_blob, after_blob, diff_sha256)
                ):
                    with self.assertRaisesRegex(OSError, "does not contain"):
                        omo_completion_email.validate_source1990_pangram_authority(root, plan, items, evidence, wrong_blob, current)
                wrong_diff = replace(request, churn_diff_sha256="0" * 64)
                with patch.object(
                    omo_completion_email, "SOURCE1990_PANGRAM_CHURN", (claim_bound_commit, before_blob, after_blob, wrong_diff.churn_diff_sha256)
                ):
                    with self.assertRaisesRegex(OSError, "diff does not match"):
                        omo_completion_email.validate_source1990_pangram_authority(root, plan, items, evidence, wrong_diff, current)

    def test_public_cli_rejects_generic_sent_recovery(self) -> None:
        with self.assertRaises(SystemExit):
            omo_pending.parse_args(["recover-sent-add"])
        with self.assertRaises(SystemExit):
            omo_pending.parse_args(["recover-sent-remove"])

    def test_source1990_adapter_rejects_any_delivered_message_drift(self) -> None:
        """The incident adapter is limited to its literal Human authority and answer."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / omo_completion_email.SOURCE1990_PATH
            source.parent.mkdir()
            source.write_text(omo_completion_email.SOURCE1990_TEXT, encoding="utf-8")
            source.chmod(0o600)
            task = root / omo_completion_email.SOURCE1990_PANGRAM_TASK
            plan = omo_completion_email.CompletionEmail(
                root,
                task,
                omo_completion_email.SOURCE1990_PANGRAM_OWNER,
                omo_completion_email.SOURCE1990_PANGRAM_MANAGER,
                omo_completion_email.SOURCE1990_PANGRAM_TASK_SHA256,
                "pending item removed after verification",
                "",
                "",
                "key",
                "notice",
                omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
                send_allowed=False,
            )
            primary = omo_completion_email.SOURCE1990_PANGRAM_PRIMARY_CLAIM
            extra = omo_completion_email.SOURCE1990_PANGRAM_EXTRA_CLAIM
            request = OrdinaryPendingRecoveryRequest(
                "source1990-pangram-remove",
                omo_completion_email.SOURCE1990_PANGRAM_TASK_SHA256,
                omo_completion_email.SOURCE1990_PANGRAM_QUEUE_SHA256,
                omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
                omo_completion_email.SOURCE1990_PANGRAM_PURPOSE_SHA256,
                *primary,
                omo_completion_email.SOURCE1990_PANGRAM_MESSAGE_ID,
                omo_completion_email.SOURCE1990_PANGRAM_SUBJECT_SHA256,
                omo_completion_email.SOURCE1990_PANGRAM_BODY_SHA256,
                *omo_completion_email.SOURCE1990_PANGRAM_CHURN,
                "",
                *extra,
            )
            current = "prefix\ncustody clearance\n"
            current_sha256 = hashlib.sha256(current.encode()).hexdigest()
            plan = replace(plan, task_sha256=current_sha256)
            request = replace(request, expected_task_sha256=current_sha256)
            authority_constants = {
                "SOURCE1990_PANGRAM_ROOT": str(root.resolve()),
                "SOURCE1990_PANGRAM_TASK_SHA256": current_sha256,
            }
            with patch.multiple(omo_completion_email, **authority_constants), patch(
                "omo_manager.omo_completion_email.validate_manager_churn"
            ):
                omo_completion_email.validate_source1990_pangram_authority(
                    root,
                    plan,
                    omo_completion_email.SOURCE1990_PANGRAM_ITEMS,
                    omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE,
                    request,
                    current,
                )
                wrong_message = replace(request, message_id="<unrelated@example.test>")
                with self.assertRaisesRegex(OSError, "does not bind"):
                    omo_completion_email.validate_source1990_pangram_authority(
                        root,
                        plan,
                        omo_completion_email.SOURCE1990_PANGRAM_ITEMS,
                        omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE,
                        wrong_message,
                        current,
                    )
                wrong_body = replace(request, sent_body_sha256="0" * 64)
                with self.assertRaisesRegex(OSError, "does not bind"):
                    omo_completion_email.validate_source1990_pangram_authority(
                        root,
                        plan,
                        omo_completion_email.SOURCE1990_PANGRAM_ITEMS,
                        omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE,
                        wrong_body,
                        current,
                    )

    def test_source1990_pangram_cli_retires_both_claims_and_replays(self) -> None:
        """Exercise the incident adapter through its no-send command boundary."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            source = root / omo_completion_email.SOURCE1990_PATH
            source.parent.mkdir()
            source.write_text(omo_completion_email.SOURCE1990_TEXT, encoding="utf-8")
            source.chmod(0o600)
            items = omo_completion_email.SOURCE1990_PANGRAM_ITEMS
            task = root / "src1964_pangram.md"
            text = task_text().replace("cfg:2", "dw:15").replace("cfg:1", "dw:60").replace(
                "  - finish review", "".join(f"  - {item}\n" for item in items).rstrip()
            )
            task.write_text(text, encoding="utf-8")
            evidence = omo_completion_email.SOURCE1990_PANGRAM_EVIDENCE
            purpose = ordinary_pending_purpose("pending item removed after verification", items, evidence)
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_pending.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
            ):
                primary = plan_completion_email(
                    root, task, text, "pending item completed", items=(items[0],), semantic_key="a" * 64, pending_item_owner=True
                )
                extra = plan_completion_email(
                    root, task, text, "pending item completed", items=(items[1],), semantic_key="b" * 64, pending_item_owner=True
                )
                assert primary is not None and extra is not None
                self.assertTrue(claim_completion_email(primary))
                self.assertTrue(claim_completion_email(extra))
                primary_binding = (
                    primary.key,
                    primary.task_sha256,
                    primary.manager_target,
                    primary.notice_semantic_key,
                    hashlib.sha256((state / "completion-email-authorizations" / primary.key).read_bytes()).hexdigest(),
                )
                extra_binding = (
                    extra.key,
                    extra.task_sha256,
                    extra.manager_target,
                    extra.notice_semantic_key,
                    hashlib.sha256((state / "completion-email-authorizations" / extra.key).read_bytes()).hexdigest(),
                )
                source_constants = {
                    "SOURCE1990_PANGRAM_ROOT": str(root.resolve()),
                    "SOURCE1990_PANGRAM_TASK": task.name,
                    "SOURCE1990_PANGRAM_OWNER": "dw:15",
                    "SOURCE1990_PANGRAM_MANAGER": "dw:60",
                    "SOURCE1990_PANGRAM_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
                    "SOURCE1990_PANGRAM_QUEUE_SHA256": digest_fields("pending-queue-v1", *items),
                    "SOURCE1990_PANGRAM_PURPOSE_SHA256": purpose,
                    "SOURCE1990_PANGRAM_EVIDENCE": evidence,
                    "SOURCE1990_PANGRAM_MESSAGE_ID": "<source1990@example.test>",
                    "SOURCE1990_PANGRAM_SUBJECT_SHA256": "c" * 64,
                    "SOURCE1990_PANGRAM_BODY_SHA256": "d" * 64,
                    "SOURCE1990_PANGRAM_PRIMARY_CLAIM": primary_binding,
                    "SOURCE1990_PANGRAM_EXTRA_CLAIM": extra_binding,
                    "SOURCE1990_PANGRAM_CHURN": ("", "", "", ""),
                }
                argv = [
                    "recover-source1990-pangram",
                    *(value for item in items for value in ("--item", item)),
                    "--evidence",
                    evidence,
                    "--expected-task-sha256",
                    source_constants["SOURCE1990_PANGRAM_TASK_SHA256"],
                    "--expected-queue-sha256",
                    source_constants["SOURCE1990_PANGRAM_QUEUE_SHA256"],
                    "--purpose-sha256",
                    purpose,
                    "--prior-claim-key",
                    primary.key,
                    "--prior-task-sha256",
                    primary.task_sha256,
                    "--prior-manager-target",
                    primary.manager_target,
                    "--prior-semantic-key",
                    primary.notice_semantic_key,
                    "--prior-authorization-sha256",
                    primary_binding[-1],
                    "--message-id",
                    "<source1990@example.test>",
                    "--sent-subject-sha256",
                    "c" * 64,
                    "--sent-body-sha256",
                    "d" * 64,
                    "--extra-claim-key",
                    extra.key,
                    "--extra-task-sha256",
                    extra.task_sha256,
                    "--extra-manager-target",
                    extra.manager_target,
                    "--extra-semantic-key",
                    extra.notice_semantic_key,
                    "--extra-authorization-sha256",
                    extra_binding[-1],
                ]
                with patch.multiple(omo_completion_email, **source_constants), patch(
                    "omo_manager.omo_completion_email.validate_manager_churn"
                ):
                    parsed = omo_pending.parse_args(argv)
                    updated, _count = omo_pending.remove_pending_items(text, items)
                    updated = omo_pending.append_comment(updated, omo_pending.pending_remove_evidence_comment(len(items), evidence))
                    recovery = plan_sent_recovery_completion(
                        root,
                        task,
                        text,
                        "pending item removed after verification",
                        items=items,
                        evidence=evidence,
                        semantic_key=omo_pending.sent_recovery_request(parsed).semantic_key,
                    )
                    assert recovery is not None
                    original_fsync_directory = omo_completion_email.fsync_directory

                    def lose_recovery_directory(directory: Path) -> None:
                        if directory == state:
                            raise OSError("power loss before recovery directory link")
                        original_fsync_directory(directory)

                    with patch("omo_manager.omo_completion_email.fsync_directory", side_effect=lose_recovery_directory):
                        with self.assertRaisesRegex(OSError, "power loss"):
                            prepare_ordinary_pending_transition(
                                recovery,
                                items,
                                evidence,
                                hashlib.sha256(updated.encode()).hexdigest(),
                                digest_fields("pending-queue-v1"),
                                omo_pending.sent_recovery_request(parsed),
                                text,
                            )
                    self.assertEqual([], list((state / "ordinary-completion-by-message").iterdir()))
                    prepared = prepare_ordinary_pending_transition(
                        recovery,
                        items,
                        evidence,
                        hashlib.sha256(updated.encode()).hexdigest(),
                        digest_fields("pending-queue-v1"),
                        omo_pending.sent_recovery_request(parsed),
                        text,
                    )
                    self.assertIn('"status":"prepared"', prepared.record)
                    with patch("omo_manager.omo_pending.fsync_task_parent", side_effect=OSError("power loss after task replace")):
                        with self.assertRaisesRegex(OSError, "power loss"):
                            omo_pending.run(parsed, root=root)
                    self.assertEqual((), parse_task_metadata(task.read_text(encoding="utf-8"), root).pending_task_items)
                    self.assertEqual(0, omo_pending.run(parsed, root=root))
            self.assertEqual((), parse_task_metadata(task.read_text(encoding="utf-8"), root).pending_task_items)
            claims = (state / "completion-email-claims.tsv").read_text(encoding="utf-8")
            self.assertIn(f"{primary.key}\tretired:", claims)
            self.assertIn(f"{extra.key}\tretired:", claims)

    def test_internal_recovery_replays_only_the_same_prepared_four_item_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            items = tuple(f"🧑 Human item {number}" for number in range(1, 5))
            text = task_text().replace("  - finish review", "".join(f"  - {item}\n" for item in items).rstrip())
            task.write_text(text, encoding="utf-8")
            semantic_key = "a" * 64
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_pending.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
            ):
                prior = plan_completion_email(
                    root,
                    task,
                    text,
                    "pending item completed",
                    items=(items[0],),
                    semantic_key=semantic_key,
                    pending_item_owner=True,
                )
                assert prior is not None
                self.assertTrue(claim_completion_email(prior))
                evidence = "reviewed"
                purpose = ordinary_pending_purpose("pending item removed after verification", items, evidence)
                body = "pending item deleted:\n" + "".join(f"- Human item {number}\n" for number in range(1, 5))
                request = OrdinaryPendingRecoveryRequest(
                    "supersede-remove",
                    hashlib.sha256(text.encode()).hexdigest(),
                    digest_fields("pending-queue-v1", *items),
                    purpose,
                    purpose,
                    prior.key,
                    prior.task_sha256,
                    prior.manager_target,
                    prior.notice_semantic_key,
                    hashlib.sha256((state / "completion-email-authorizations" / prior.key).read_bytes()).hexdigest(),
                    "<canonical@example.test>",
                    "b" * 64,
                    hashlib.sha256(body.encode()).hexdigest(),
                )
                updated, _count = omo_pending.remove_pending_items(text, items)
                updated = omo_pending.append_comment(updated, omo_pending.pending_remove_evidence_comment(len(items), evidence))
                recovery = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=items,
                    evidence=evidence,
                    semantic_key=request.semantic_key,
                )
                assert recovery is not None
                prepared = prepare_ordinary_pending_transition(
                    recovery,
                    items,
                    evidence,
                    hashlib.sha256(updated.encode()).hexdigest(),
                    digest_fields("pending-queue-v1"),
                    request,
                    text,
                )
                self.assertIn('"status":"prepared"', prepared.record)
                task.write_text(updated, encoding="utf-8")
                commit_ordinary_pending_transition(prepared, hashlib.sha256(updated.encode()).hexdigest())
                self.assertEqual((), parse_task_metadata(task.read_text(encoding="utf-8"), root).pending_task_items)
                commit_ordinary_pending_transition(prepared, hashlib.sha256(updated.encode()).hexdigest())
            self.assertIn("retired:", (state / "completion-email-claims.tsv").read_text(encoding="utf-8"))

    def test_sent_recovery_rejects_a_verified_noncanonical_body_before_tombstoning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("  - finish review", "  - 🧑 first")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                prior = plan_completion_email(
                    root, task, text, "pending item completed", items=("🧑 first",), semantic_key="a" * 64, pending_item_owner=True
                )
                assert prior is not None
                self.assertTrue(claim_completion_email(prior))
                request = OrdinaryPendingRecoveryRequest(
                    "supersede-remove",
                    hashlib.sha256(text.encode()).hexdigest(),
                    digest_fields("pending-queue-v1", "🧑 first"),
                    ordinary_pending_purpose("pending item removed after verification", ("🧑 first",), "reviewed"),
                    ordinary_pending_purpose("pending item removed after verification", ("🧑 first",), "reviewed"),
                    prior.key,
                    prior.task_sha256,
                    prior.manager_target,
                    prior.notice_semantic_key,
                    hashlib.sha256((state / "completion-email-authorizations" / prior.key).read_bytes()).hexdigest(),
                    "<wrong-body@example.test>",
                    "b" * 64,
                    "c" * 64,
                )
                recovery = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=("🧑 first",),
                    evidence="reviewed",
                    semantic_key=request.semantic_key,
                )
                assert recovery is not None
                with self.assertRaisesRegex(OSError, "does not match the canonical"):
                    prepare_ordinary_pending_transition(recovery, ("🧑 first",), "reviewed", "d" * 64, digest_fields("pending-queue-v1"), request, text)
            self.assertFalse((state / "ordinary-pending-transitions").exists())

    def test_sent_recovery_crash_after_message_marker_rejects_other_transition(self) -> None:
        """A durable Message-ID binding survives a crash before claim retirement."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            items = ("🧑 first",)
            text = task_text().replace("  - finish review", "  - 🧑 first")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                prior = plan_completion_email(
                    root, task, text, "pending item completed", items=items, semantic_key="a" * 64, pending_item_owner=True
                )
                assert prior is not None
                self.assertTrue(claim_completion_email(prior))
                authorization_sha256 = hashlib.sha256((state / "completion-email-authorizations" / prior.key).read_bytes()).hexdigest()
                evidence = "reviewed"
                purpose = ordinary_pending_purpose("pending item removed after verification", items, evidence)
                request = OrdinaryPendingRecoveryRequest(
                    "supersede-remove",
                    hashlib.sha256(text.encode()).hexdigest(),
                    digest_fields("pending-queue-v1", *items),
                    purpose,
                    purpose,
                    prior.key,
                    prior.task_sha256,
                    prior.manager_target,
                    prior.notice_semantic_key,
                    authorization_sha256,
                    "<marker-crash@example.test>",
                    "b" * 64,
                    hashlib.sha256(b"pending item deleted:\n- first\n").hexdigest(),
                )
                recovery = plan_sent_recovery_completion(
                    root, task, text, "pending item removed after verification", items=items, evidence=evidence, semantic_key=purpose
                )
                assert recovery is not None
                with patch("omo_manager.omo_completion_email.rewrite_claims", side_effect=RuntimeError("crash")):
                    with self.assertRaisesRegex(RuntimeError, "crash"):
                        prepare_ordinary_pending_transition(
                            recovery, items, evidence, "d" * 64, digest_fields("pending-queue-v1"), request, text
                        )
                prepared = prepare_ordinary_pending_transition(
                    recovery, items, evidence, "d" * 64, digest_fields("pending-queue-v1"), request, text
                )
                self.assertIn('"status":"prepared"', prepared.record)
                changed_evidence = "different reviewed evidence"
                changed_purpose = ordinary_pending_purpose("pending item removed after verification", items, changed_evidence)
                changed_request = replace(request, purpose_sha256=changed_purpose, semantic_key=changed_purpose)
                changed = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=items,
                    evidence=changed_evidence,
                    semantic_key=changed_purpose,
                )
                assert changed is not None
                with self.assertRaisesRegex(OSError, "Message-ID is already bound"):
                    prepare_ordinary_pending_transition(
                        changed,
                        items,
                        changed_evidence,
                        "e" * 64,
                        digest_fields("pending-queue-v1"),
                        changed_request,
                        text,
                    )
            self.assertEqual(text, task.read_text(encoding="utf-8"))

    def test_sent_recovery_retires_all_bound_claims_before_committing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            items = ("🧑 first", "🧑 second")
            text = task_text().replace("  - finish review", "  - 🧑 first\n  - 🧑 second")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                primary = plan_completion_email(
                    root, task, text, "pending item completed", items=("🧑 first",), semantic_key="a" * 64, pending_item_owner=True
                )
                extra = plan_completion_email(
                    root, task, text, "pending item completed", items=("🧑 second",), semantic_key="b" * 64, pending_item_owner=True
                )
                assert primary is not None and extra is not None
                self.assertTrue(claim_completion_email(primary))
                self.assertTrue(claim_completion_email(extra))
                authorization_dir = state / "completion-email-authorizations"
                request = OrdinaryPendingRecoveryRequest(
                    "supersede-remove",
                    hashlib.sha256(text.encode()).hexdigest(),
                    digest_fields("pending-queue-v1", *items),
                    ordinary_pending_purpose("pending item removed after verification", items, "reviewed"),
                    ordinary_pending_purpose("pending item removed after verification", items, "reviewed"),
                    primary.key,
                    primary.task_sha256,
                    primary.manager_target,
                    primary.notice_semantic_key,
                    hashlib.sha256((authorization_dir / primary.key).read_bytes()).hexdigest(),
                    "<sent@example.test>",
                    "c" * 64,
                    hashlib.sha256(b"pending item deleted:\n- first\n- second\n").hexdigest(),
                    extra_claim_key=extra.key,
                    extra_task_sha256=extra.task_sha256,
                    extra_manager_target=extra.manager_target,
                    extra_semantic_key=extra.notice_semantic_key,
                    extra_authorization_sha256=hashlib.sha256((authorization_dir / extra.key).read_bytes()).hexdigest(),
                )
                recovery = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=items,
                    evidence="reviewed",
                    semantic_key=request.semantic_key,
                )
                assert recovery is not None
                updated = text.replace("  - 🧑 first\n  - 🧑 second\n", "") + "(verified removed pending items: reviewed)\n"
                transition = prepare_ordinary_pending_transition(
                    recovery,
                    items,
                    "reviewed",
                    hashlib.sha256(updated.encode()).hexdigest(),
                    digest_fields("pending-queue-v1"),
                    request,
                    text,
                )
                task.write_text(updated, encoding="utf-8")
                commit_ordinary_pending_transition(transition, hashlib.sha256(updated.encode()).hexdigest())
            claims = (state / "completion-email-claims.tsv").read_text(encoding="utf-8")
            self.assertIn(f"{primary.key}\tretired:{transition.key}", claims)
            self.assertIn(f"{extra.key}\tretired:{transition.key}", claims)
            self.assertTrue((state / "completion-email-retired-authorizations" / primary.key).is_file())
            self.assertTrue((state / "completion-email-retired-authorizations" / extra.key).is_file())

    def test_manager_maintenance_without_requested_human_result_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "mail_close_diag.md"
            text = task_text(
                """<manager_delegation from="config:27">
Diagnose and complete the supported done-live closure for `mail_cleanup_v.md`.

- do not reopen work, replay reports, send Human mail, commit work-log files, infer evidence, or stop the pane separately
- report terminal completion or one exact blocker to config:27 through `omo_report.sh`
</manager_delegation>""",
                human_report=False,
            )
            task.write_text(text, encoding="utf-8")

            self.assertIsNone(build_completion_email(root, task, text, "task done"))

    def test_normal_close_uses_latest_thread_and_exact_window_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text(human_report=False)
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as email:
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert plan is not None
                self.assertEqual("", plan.subject)
                self.assertEqual("Closed cfg:2\n", plan.body)
                self.assertTrue(send_completion_email(plan))
                self.assertFalse(send_completion_email(plan))

            command = email.call_args.args[0]
            email.assert_called_once()
            self.assertNotIn("--subject-file", command)
            self.assertIn("--message-file", command)

    def test_normal_close_reports_window_not_nonzero_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(human_report=False).replace("runat: cfg:2", "runat: cfg:2.1")
            task.write_text(text, encoding="utf-8")

            plan = build_completion_email(root, task, text, "task done")

            assert plan is not None
            self.assertEqual("Closed cfg:2\n", plan.body)

    def test_pending_notice_reuses_thread_and_has_exact_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")

            plan = build_completion_email(root, task, text, "pending item completed", items=("🧑 finish review",))

            assert plan is not None
            self.assertEqual("", plan.subject)
            self.assertEqual("pending item deleted:\n- finish review\n", plan.body)

    def test_pending_notice_multiple_item_bodies_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(human_report=False)
            task.write_text(text, encoding="utf-8")

            created = build_completion_email(root, task, text, "pending item created", items=("🧑 first", "🧑 second"))
            deleted = build_completion_email(root, task, text, "pending item cancelled", items=("🧑 first", "🧑 second"))

            assert created is not None and deleted is not None
            self.assertEqual("pending item created:\n- first\n- second\n", created.body)
            self.assertEqual("pending item deleted:\n- first\n- second\n", deleted.body)
            self.assertEqual("", created.subject)
            self.assertEqual("", deleted.subject)

    def test_pending_item_notice_does_not_require_general_human_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text("Return only a concise report to your manager.", human_report=False)
            task.write_text(text, encoding="utf-8")

            self.assertIsNotNone(build_completion_email(root, task, text, "pending item created", items=("🧑 finish review",)))

    def test_pending_item_notice_honors_explicit_no_contact(self) -> None:
        for policy in (
            "Never email the Human.",
            "Never email the Human for Human- or agent-authored pending items.",
            "Never email the Human for Human-originated or agent-authored pending items.",
            "Without weakening the no-contact rule, do not email the Human under that policy.",
        ):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "task.md"
                text = task_text(policy, human_report=False)
                task.write_text(text, encoding="utf-8")

                for outcome in ("pending item created", "pending item removed after verification"):
                    with self.subTest(outcome=outcome):
                        self.assertIsNone(build_completion_email(root, task, text, outcome, items=("🧑 finish review",)))

    def test_pending_item_notice_ignores_agent_scoped_no_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(
                "Human-authored pending items email the Human; agent-authored pending items must not email the Human.",
                human_report=False,
            )
            task.write_text(text, encoding="utf-8")

            for outcome in ("pending item created", "pending item removed after verification"):
                with self.subTest(outcome=outcome):
                    self.assertIsNotNone(build_completion_email(root, task, text, outcome, items=("🧑 finish review",)))

    def test_pending_item_notice_ignores_no_contact_safeguard_meta_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(
                "Implement the correction without weakening explicit no-contact or delivery safeguards.",
                human_report=False,
            )
            task.write_text(text, encoding="utf-8")

            for outcome in ("pending item created", "pending item removed after verification"):
                with self.subTest(outcome=outcome):
                    self.assertIsNotNone(build_completion_email(root, task, text, outcome, items=("🧑 finish review",)))

    def test_pending_item_notice_rejects_agent_and_ambiguous_legacy_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(human_report=False)
            task.write_text(text, encoding="utf-8")

            for items in (("agent work",), ("legacy work",), ("🧑 Human work", "agent work"), ()):
                with self.subTest(items=items):
                    self.assertIsNone(build_completion_email(root, task, text, "pending item created", items=items))

    def test_task_close_rejects_answer_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                with self.assertRaisesRegex(ValueError, "cannot override"):
                    plan_completion_email(
                        root,
                        task,
                        text,
                        "task done",
                        human_subject="Re: Different thread",
                        human_body="Different body",
                    )

    def test_task_close_rejects_non_digest_semantic_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text(human_report=False)
            task.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                build_completion_email(root, task, text, "task done", semantic_key="garbage")

    def test_compound_no_human_mail_rule_overrides_result_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text("Do not reopen work, replay reports, send Human mail, or commit work-log files.")
            task.write_text(text, encoding="utf-8")

            self.assertIsNone(build_completion_email(root, task, text, "task done"))

    def test_negated_direct_human_report_rule_is_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text("Never report results directly to the Human.")
            task.write_text(text, encoding="utf-8")

            self.assertIsNone(build_completion_email(root, task, text, "task done"))

    def test_manager_task_cannot_send_even_with_result_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            text = task_text().replace("is_manager: false", "is_manager: true")
            task.write_text(text, encoding="utf-8")

            self.assertIsNone(build_completion_email(root, task, text, "task done"))

    def test_pending_item_notice_uses_explicit_queue_owner_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch(
                "omo_manager.omo_completion_email.current_active_task",
                side_effect=TaskFrontmatterError("multiple active work queues match the current agent"),
            ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task) as pending:
                plan = plan_completion_email(
                    root,
                    task,
                    text,
                    "pending item completed",
                    items=("🧑 finish review",),
                    pending_item_owner=True,
                )

            self.assertIsNotNone(plan)
            pending.assert_called_once_with(root)

    def test_task_completion_does_not_use_queue_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch(
                "omo_manager.omo_completion_email.current_active_task",
                side_effect=TaskFrontmatterError("multiple active work queues match the current agent"),
            ), patch("omo_manager.omo_completion_email.current_pending_task") as pending:
                plan = plan_completion_email(root, task, text, "task done", items=("completion context",))

            self.assertIsNone(plan)
            pending.assert_not_called()

    def test_ordinary_sent_verification_binds_message_participants_and_content(self) -> None:
        message = EmailMessage()
        message["From"] = "agent@example.test"
        message["To"] = "human@example.test"
        message["Subject"] = "Exact subject"
        message["Message-ID"] = "<sent@example.test>"
        message.set_content("Exact body\n")

        class FakeImap:
            def login(self, _address: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
                self.assert_readonly = readonly
                return "OK", []

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "search":
                    return "OK", [b"1"]
                return "OK", [(b"1", message.as_bytes())]

            def logout(self) -> None:
                return None

        settings = type(
            "Settings",
            (),
            {"agent_address": "agent@example.test", "human_address": "human@example.test", "app_password": "secret"},
        )()
        with patch("omo_manager.omo_completion_email.configured_agent_mail", return_value=settings), patch(
            "omo_manager.omo_completion_email.imaplib.IMAP4_SSL", return_value=FakeImap()
        ):
            self.assertTrue(
                verify_ordinary_completion_in_sent(
                    "<sent@example.test>",
                    hashlib.sha256(b"Exact subject").hexdigest(),
                    hashlib.sha256(b"Exact body\n").hexdigest(),
                )
            )

    def delivered_receipt(
        self,
        state: Path,
        root: Path,
        task: Path,
        text: str,
        *,
        items: tuple[str, ...] = (),
        evidence: str = "",
        semantic_key: str = "",
    ) -> tuple[Path, str]:
        plan = build_completion_email(root, task, text, "task done", items=items, evidence=evidence, semantic_key=semantic_key)
        assert plan is not None
        with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
            self.assertTrue(claim_completion_email(plan))
            mark_completion_email_delivered(plan)
        receipt = state / "completion-email-delivered" / plan.key
        return receipt, hashlib.sha256(receipt.read_bytes()).hexdigest()

    def test_recovery_validates_existing_semantic_delivery_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            original = task_text()
            task.write_text(original, encoding="utf-8")
            semantic_key = "a" * 64
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                original_plan = build_completion_email(root, task, original, "task done", semantic_key=semantic_key)
                assert original_plan is not None
                self.assertTrue(claim_completion_email(original_plan))
                used = state / "completion-email-authorization-used"
                used.mkdir(mode=0o700)
                used_marker = used / original_plan.key
                used_marker.write_text(f"{original_plan.target}\t{task.name}\n", encoding="utf-8")
                used_marker.chmod(0o600)
                mark_completion_email_delivered(original_plan)
                blocked = original.replace(
                    "status: running",
                    "status: blocked\nblocked_on: done_close_in_progress: manager is closing the agent before marking done",
                )
                changed = f"{blocked}manager note\n"
                task.write_text(changed, encoding="utf-8")
                recovery_plan = build_completion_email(root, task, changed, "task done", semantic_key=semantic_key)
                assert recovery_plan is not None
                before = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
                self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), validate_completion_notice_delivery(recovery_plan))
                self.assertEqual(before, {path: path.read_bytes() for path in state.rglob("*") if path.is_file()})

    def test_recovery_rejects_delivery_bound_to_another_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            original = task_text()
            task.write_text(original, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                plan = build_completion_email(root, task, original, "task done", semantic_key="b" * 64)
                assert plan is not None
                self.assertTrue(claim_completion_email(plan))
                used = state / "completion-email-authorization-used"
                used.mkdir(mode=0o700)
                used_marker = used / plan.key
                used_marker.write_text(f"{plan.target}\t{task.name}\n", encoding="utf-8")
                used_marker.chmod(0o600)
                mark_completion_email_delivered(plan)
                other = original.replace("runat: cfg:2", "runat: cfg:3")
                other_plan = build_completion_email(root, task, other, "task done", semantic_key="b" * 64)
                assert other_plan is not None
                with self.assertRaises(OSError):
                    validate_completion_notice_delivery(other_plan)

    def test_ordinary_completion_cannot_suppress_close_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            before = task_text()
            after = before.replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(after, encoding="utf-8")
            message_id = "<already-sent@example.test>"
            subject_sha256 = hashlib.sha256(b"Exact completion subject").hexdigest()
            body_sha256 = hashlib.sha256(b"Exact completion body\n").hexdigest()
            semantic_key = "a" * 64
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch(
                "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
            ) as verify, patch("omo_manager.omo_completion_email.subprocess.run") as send:
                reconcile_ordinary_sent_completion(
                    root,
                    task,
                    "legacy pending items removed without email",
                    message_id,
                    subject_sha256,
                    body_sha256,
                    semantic_key=semantic_key,
                )
                reconcile_ordinary_sent_completion(
                    root,
                    task,
                    "legacy pending items removed without email",
                    message_id,
                    subject_sha256,
                    body_sha256,
                    semantic_key=semantic_key,
                )
                final = after + "(verified repair item completed)\n"
                task.write_text(final, encoding="utf-8")
                plan = plan_completion_email(root, task, final, "task done", semantic_key=semantic_key)
                assert plan is not None
                self.assertFalse(completion_email_is_delivered(plan))
                self.assertTrue(send_completion_email(plan))
            self.assertEqual(2, verify.call_count)
            send.assert_called_once()

    def test_ordinary_sent_message_cannot_be_reused_for_another_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            first = root / "first.md"
            second = root / "second.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            first.write_text(text, encoding="utf-8")
            second.write_text(text, encoding="utf-8")
            message_id = "<already-sent@example.test>"
            digest = hashlib.sha256(b"exact").hexdigest()
            active = first
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", side_effect=lambda _root: active
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                reconcile_ordinary_sent_completion(
                    root, first, "completed", message_id, digest, digest, semantic_key="a" * 64
                )
                active = second
                with self.assertRaisesRegex(OSError, "different evidence"):
                    reconcile_ordinary_sent_completion(
                        root, second, "completed", message_id, digest, digest, semantic_key="a" * 64
                    )

    def test_ordinary_sent_reconciliation_cannot_satisfy_task_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch(
                "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent"
            ) as verify:
                with self.assertRaisesRegex(ValueError, "cannot satisfy"):
                    reconcile_ordinary_sent_completion(
                        root,
                        task,
                        "task done",
                        "<arbitrary@example.test>",
                        "b" * 64,
                        "c" * 64,
                        semantic_key="a" * 64,
                    )
            verify.assert_not_called()

    def test_unverified_ordinary_message_creates_no_completion_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(b"exact").hexdigest()
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=False):
                with self.assertRaisesRegex(OSError, "not exact verified"):
                    reconcile_ordinary_sent_completion(
                        root,
                        task,
                        "completed",
                        "<missing@example.test>",
                        digest,
                        digest,
                        semantic_key="a" * 64,
                    )
            self.assertFalse(state.exists())

    def test_ordinary_reconciliation_rejects_existing_structured_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            digest = hashlib.sha256(b"exact").hexdigest()
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                plan = plan_completion_email(root, task, text, "completed", semantic_key="a" * 64)
                assert plan is not None
                self.assertTrue(claim_completion_email(plan))
                with self.assertRaisesRegex(OSError, "structured completion state"):
                    reconcile_ordinary_sent_completion(
                        root,
                        task,
                        "completed",
                        "<already-sent@example.test>",
                        digest,
                        digest,
                        semantic_key="a" * 64,
                    )
            self.assertEqual((), tuple((state / "ordinary-completion-by-notice").iterdir()))

    def test_ordinary_reconciliation_rejects_pre_churn_orphan_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            before = task_text()
            task.write_text(before, encoding="utf-8")
            semantic_key = "a" * 64
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ):
                old_plan = plan_completion_email(
                    root,
                    task,
                    before,
                    "pending item completed",
                    items=("🧑 finish review",),
                    semantic_key=semantic_key,
                )
                assert old_plan is not None
                with patch("omo_manager.omo_completion_email.os.replace", side_effect=OSError("crash before claim")):
                    with self.assertRaisesRegex(OSError, "crash before claim"):
                        claim_completion_email(old_plan)
                self.assertTrue((state / "completion-email-authorizations" / old_plan.key).is_file())
                after = before.replace("pending_task_items:\n  - finish review", "pending_task_items: []")
                task.write_text(after, encoding="utf-8")
                with patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                    with self.assertRaisesRegex(OSError, "structured completion state"):
                        reconcile_ordinary_sent_completion(
                            root,
                            task,
                            "legacy pending items removed without email",
                            "<already-sent@example.test>",
                            "b" * 64,
                            "c" * 64,
                            semantic_key=semantic_key,
                        )

    def test_unrelated_legacy_authorization_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            authorization_dir = state / "completion-email-authorizations"
            authorization_dir.mkdir(mode=0o700, parents=True)
            legacy = authorization_dir / ("d" * 64)
            legacy.write_text(
                "version=1\ntarget=cfg:9\nroot=/unrelated\ntask=old.md\n"
                f"notice_key={'e' * 64}\nsubject_sha256={'f' * 64}\nbody_sha256={'0' * 64}\n",
                encoding="utf-8",
            )
            legacy.chmod(0o600)
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                reconcile_ordinary_sent_completion(
                    root,
                    task,
                    "completed",
                    "<already-sent@example.test>",
                    "b" * 64,
                    "c" * 64,
                    semantic_key="a" * 64,
                )

    def test_structured_claim_cannot_race_past_ordinary_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                reconcile_ordinary_sent_completion(
                    root,
                    task,
                    "completed",
                    "<already-sent@example.test>",
                    "b" * 64,
                    "c" * 64,
                    semantic_key="a" * 64,
                )
                plan = plan_completion_email(root, task, text, "completed", semantic_key="a" * 64)
                assert plan is not None
                self.assertFalse(claim_completion_email(plan))
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_v1_pending_completion_cannot_suppress_close_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            semantic_key = "a" * 64
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.subprocess.run") as send:
                pending_plan = plan_completion_email(
                    root,
                    task,
                    initial,
                    "pending item completed",
                    items=("🧑 finish review",),
                    evidence="review passed",
                    semantic_key=semantic_key,
                )
                assert pending_plan is not None
                self.assertTrue(send_completion_email(pending_plan))
                finished = initial.replace("pending_task_items:\n  - finish review", "pending_task_items: []")
                task.write_text(finished, encoding="utf-8")
                done_plan = plan_completion_email(root, task, finished, "task done", semantic_key=semantic_key)
                assert done_plan is not None
                self.assertNotEqual(pending_plan.key, done_plan.key)
                self.assertNotEqual(pending_plan.notice_key, done_plan.notice_key)
                self.assertEqual(pending_plan.semantic_key, done_plan.semantic_key)
                self.assertNotEqual(pending_plan.notice_semantic_key, done_plan.notice_semantic_key)
                self.assertFalse(completion_email_is_delivered(done_plan))
                self.assertTrue(send_completion_email(done_plan))
            self.assertEqual(2, send.call_count)

    def test_legacy_exact_receipt_is_readable_with_its_exact_claim(self) -> None:
        for width in (3, 5, 6):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                state.mkdir(mode=0o700)
                task = root / "task.md"
                text = task_text()
                task.write_text(text, encoding="utf-8")
                plan = build_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert plan is not None
                fields = [plan.key, plan.target, plan.task.name, plan.manager_target, plan.task_sha256, plan.notice_key]
                ledger = state / "completion-email-claims.tsv"
                ledger.write_text("\t".join(fields[:width]) + "\n", encoding="utf-8")
                ledger.chmod(0o600)
                delivered = state / "completion-email-delivered"
                delivered.mkdir(mode=0o700)
                receipt = delivered / plan.key
                receipt.write_text(f"{plan.target}\t{plan.task.name}\n", encoding="utf-8")
                receipt.chmod(0o600)
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    self.assertTrue(completion_email_is_delivered(plan))
                self.assertEqual(f"{plan.target}\t{plan.task.name}\n", receipt.read_text(encoding="utf-8"))

    def test_exact_receipt_digest_must_match_its_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            plan = build_completion_email(root, task, text, "task done", semantic_key="a" * 64)
            assert plan is not None
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                self.assertTrue(claim_completion_email(plan))
                delivered = state / "completion-email-delivered"
                delivered.mkdir(mode=0o700)
                receipt = delivered / plan.key
                receipt.write_text(f"{plan.target}\t{plan.task.name}\t{'0' * 64}\n", encoding="utf-8")
                receipt.chmod(0o600)
                with self.assertRaisesRegex(OSError, "does not match"):
                    completion_email_is_delivered(plan)

    def test_v2_pending_completion_has_distinct_close_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            initial = """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000001
status: running
runat: cfg:2
tool: codex
managerat: cfg:1
is_manager: false
pending_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000002
    text: 🧑 finish review
    blocked_on: []
    notices: []
resolved_task_items: []
---
Report results directly to the Human.
work
"""
            task.write_text(initial, encoding="utf-8")
            first = build_completion_email(
                root,
                task,
                initial,
                "pending item completed",
                items=("🧑 finish review",),
                evidence="review passed",
                semantic_key="a" * 64,
            )
            finished = initial.replace(
                "pending_task_items:\n  - id: pi_019f0000-0000-7000-8000-000000000002\n"
                "    text: 🧑 finish review\n    blocked_on: []\n    notices: []",
                "pending_task_items: []",
            )
            task.write_text(finished, encoding="utf-8")
            second = build_completion_email(root, task, finished, "task done", semantic_key="a" * 64)
            assert first is not None and second is not None
            self.assertNotEqual(first.key, second.key)
            self.assertNotEqual(first.notice_key, second.notice_key)

    def test_guest_lifecycle_notice_is_suppressed_without_changing_primary_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary.md"
            primary_text = task_text()
            primary.write_text(primary_text, encoding="utf-8")
            primary_plan = build_completion_email(root, primary, primary_text, "task done")
            assert primary_plan is not None
            self.assertEqual("", primary_plan.subject)
            self.assertEqual("Closed cfg:2\n", primary_plan.body)

            guest = root / "guest.md"
            guest_text = primary_text.replace("runat: cfg:2", "runat: guest_hees:7")
            guest.write_text(guest_text, encoding="utf-8")
            self.assertIsNone(build_completion_email(root, guest, guest_text, "task done"))

    def test_delivery_and_request_markers_fsync_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ):
                plan = plan_completion_email(root, task, text, "completed")
                assert plan is not None
                with patch("omo_manager.omo_completion_email.os.fsync", wraps=__import__("os").fsync) as fsync:
                    mark_completion_email_request_queued(plan)
                    self.assertGreaterEqual(fsync.call_count, 2)
                with patch("omo_manager.omo_completion_email.os.fsync", wraps=__import__("os").fsync) as fsync:
                    mark_completion_email_delivered(plan)
                    self.assertGreaterEqual(fsync.call_count, 2)

    def test_manager_queues_owner_entrypoint_once_with_exact_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            manager = root / "manager.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=manager
            ), patch("omo_manager.omo_tmux_send.send_system_to_codex") as queue:
                self.assertFalse(
                    require_owner_completion(
                        root, task, text, "pending item completed", items=("🧑 finish review",), evidence="passed", semantic_key="d" * 64
                    )
                )
                self.assertFalse(
                    require_owner_completion(
                        root, task, text, "pending item completed", items=("🧑 finish review",), evidence="passed", semantic_key="d" * 64
                    )
                )
            queue.assert_called_once()
            queued_message = queue.call_args.args[1]
            self.assertIn(f"--semantic-key {'d' * 64}", queued_message)
            target, message = queue.call_args.args
            self.assertEqual("cfg:2", target)
            self.assertIn(str(Path(__file__).parents[1] / "omo_completion_email.py"), message)
            self.assertIn("--item '🧑 finish review'", message)
            self.assertIn("--evidence passed", message)

    def test_evidence_bound_receipt_reconciles_without_pane_or_task_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_state = root / "owner-state"
            manager_state = root / "manager-state"
            task = root / "task.md"
            manager = root / "manager.md"
            text = task_text("blocked_on: physical Mac evidence")
            task.write_text(text, encoding="utf-8")
            manager.write_text(task_text().replace("runat: cfg:2", "runat: cfg:1"), encoding="utf-8")
            manager_state.mkdir(mode=0o700)
            items = ("finish review", "preserve physical Mac blocker")
            evidence = "scoped review PASS"
            receipt, receipt_sha256 = self.delivered_receipt(
                source_state,
                root,
                task,
                text,
                items=items,
                evidence=evidence,
                semantic_key="c" * 64,
            )
            receipt.write_text(f"cfg:2\t{task.name}\n", encoding="utf-8")
            receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
            argv = [
                "--root",
                str(root),
                "--task",
                str(task),
                "--outcome",
                "task done",
                "--item",
                items[0],
                "--item",
                items[1],
                "--evidence",
                evidence,
                "--reconcile-delivered",
                "--owner",
                "cfg:2",
                "--task-sha256",
                hashlib.sha256(text.encode()).hexdigest(),
                "--receipt",
                str(receipt),
                "--receipt-sha256",
                receipt_sha256,
                "--semantic-key",
                "c" * 64,
            ]
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}),
                patch("omo_manager.omo_completion_email.current_active_task", return_value=manager),
                patch("omo_manager.omo_tmux_send.send_system_to_codex") as pane,
            ):
                self.assertEqual(0, main(argv))
                self.assertTrue(
                    require_owner_completion(
                        root, task, text, "task done", items=items, evidence=evidence, semantic_key="c" * 64
                    )
                )
            pane.assert_not_called()
            self.assertEqual(text.encode(), task.read_bytes())
            self.assertEqual(1, len(tuple((manager_state / "completion-email-reconciled").iterdir())))

    def test_reconciliation_rejects_recomputed_digest_after_source_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_state = root / "owner-state"
            manager_state = root / "manager-state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            manager_state.mkdir(mode=0o700)
            receipt, receipt_sha256 = self.delivered_receipt(source_state, root, task, text, semantic_key="c" * 64)
            original_sha256 = hashlib.sha256(text.encode()).hexdigest()
            original_plan = build_completion_email(root, task, text, "task done", semantic_key="c" * 64)
            assert original_plan is not None
            claim = (source_state / "completion-email-claims.tsv").read_text(encoding="utf-8")
            self.assertEqual(
                f"{receipt.name}\tcfg:2\t{task.name}\tcfg:1\t{original_sha256}\t{original_plan.notice_key}\t{original_plan.notice_semantic_key}\n",
                claim,
            )
            changed = text.replace("managerat: cfg:1", "managerat: cfg:1.0") + "blocked_on: physical Mac evidence\n"
            task.write_text(changed, encoding="utf-8")
            changed_plan = build_completion_email(root, task, changed, "task done")
            assert changed_plan is not None
            self.assertNotEqual(receipt.name, changed_plan.key)
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}), self.assertRaisesRegex(
                OSError, "canonical completion message"
            ):
                reconcile_delivered_completion(
                    root,
                    task,
                    "task done",
                    "cfg:2",
                    hashlib.sha256(changed.encode()).hexdigest(),
                    receipt,
                    receipt_sha256,
                )
            self.assertFalse((manager_state / "completion-email-reconciled").exists())

    def test_legacy_three_field_claim_does_not_block_new_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            (state / "completion-email-claims.tsv").write_text(f"{'a' * 64}\told:1\told.md\n", encoding="utf-8")
            (state / "completion-email-claims.tsv").chmod(0o600)
            plan = build_completion_email(root, task, text, "task done", semantic_key="b" * 64)
            assert plan is not None
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                self.assertTrue(claim_completion_email(plan))
                after_first = (state / "completion-email-claims.tsv").read_bytes()
                self.assertFalse(claim_completion_email(plan))
            rows = (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(3, len(rows[0].split("\t")))
            self.assertEqual(7, len(rows[1].split("\t")))
            self.assertEqual(after_first, (state / "completion-email-claims.tsv").read_bytes())

    def test_four_field_claim_remains_malformed_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            malformed = f"{'a' * 64}\told:1\told.md\tmanager:1\n"
            ledger = state / "completion-email-claims.tsv"
            ledger.write_text(malformed, encoding="utf-8")
            ledger.chmod(0o600)
            plan = build_completion_email(root, task, text, "task done", semantic_key="b" * 64)
            assert plan is not None

            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), self.assertRaisesRegex(
                OSError, "completion claims ledger is malformed"
            ):
                claim_completion_email(plan)

            self.assertEqual(malformed, ledger.read_text(encoding="utf-8"))

    def test_cross_state_receipt_reconciliation_survives_ready_running_churn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_state = root / "owner-state"
            manager_state = root / "manager-state"
            task = root / "task.md"
            manager = root / "manager.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            manager_state.mkdir(mode=0o700)
            manager.write_text(task_text().replace("runat: cfg:2", "runat: cfg:1").replace("managerat: cfg:1", "managerat: main:0"), encoding="utf-8")
            receipt, receipt_sha256 = self.delivered_receipt(source_state, root, task, text, semantic_key="c" * 64)
            plan = build_completion_email(root, task, text, "task done", semantic_key="c" * 64)
            assert plan is not None
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=manager
            ), patch(
                "omo_manager.omo_tmux_send.send_system_to_codex", side_effect=OSError("pane changed from ready to running")
            ) as queue:
                with self.assertRaisesRegex(OSError, "ready to running"):
                    require_owner_completion(root, task, text, "task done", semantic_key="c" * 64)
                self.assertFalse((manager_state / "completion-email-requests" / plan.key).exists())
                self.assertFalse(completion_email_is_delivered(plan))
                reconcile_delivered_completion(
                    root,
                    task,
                    "task done",
                    "cfg:2",
                    hashlib.sha256(text.encode()).hexdigest(),
                    receipt,
                    receipt_sha256,
                    semantic_key="c" * 64,
                )
                self.assertTrue(require_owner_completion(root, task, text, "task done", semantic_key="c" * 64))
            queue.assert_called_once()
            queued_message = queue.call_args.args[1]
            self.assertIn(f"--semantic-key {'c' * 64}", queued_message)
            self.assertNotIn(plan.notice_semantic_key, queued_message)

    def test_duplicate_suppressed_delivery_receipt_reconciles_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_state = root / "owner-state"
            manager_state = root / "manager-state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            manager_state.mkdir(mode=0o700)
            receipt, receipt_sha256 = self.delivered_receipt(source_state, root, task, text, semantic_key="c" * 64)
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(source_state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), patch("omo_manager.omo_completion_email.subprocess.run") as email:
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task",
                            str(task),
                            "--outcome",
                            "task done",
                            "--semantic-key",
                            "c" * 64,
                        ]
                    ),
                )
            email.assert_not_called()
            values = (
                root,
                task,
                "task done",
                "cfg:2",
                hashlib.sha256(text.encode()).hexdigest(),
                receipt,
                receipt_sha256,
            )
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}):
                reconcile_delivered_completion(*values, semantic_key="c" * 64)
                with self.assertRaisesRegex(OSError, "already consumed"):
                    reconcile_delivered_completion(*values, semantic_key="c" * 64)

    def test_receipt_reconciliation_rejects_wrong_bindings_and_ambiguity(self) -> None:
        for case in (
            "task",
            "owner",
            "outcome",
            "receipt",
            "changed bytes",
            "missing item",
            "altered item",
            "reordered items",
            "altered evidence",
            "missing claim",
            "ambiguous claim",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source_state = root / "owner-state"
                manager_state = root / "manager-state"
                task = root / "task.md"
                text = task_text()
                task.write_text(text, encoding="utf-8")
                manager_state.mkdir(mode=0o700)
                items = ("finish review", "preserve blocker")
                evidence = "scoped review PASS"
                receipt, receipt_sha256 = self.delivered_receipt(
                    source_state,
                    root,
                    task,
                    text,
                    items=items,
                    evidence=evidence,
                )
                owner = "cfg:2"
                outcome = "task done"
                task_sha256 = hashlib.sha256(text.encode()).hexdigest()
                if case == "task":
                    task_sha256 = "0" * 64
                elif case == "owner":
                    owner = "cfg:3"
                elif case == "outcome":
                    outcome = "other outcome"
                elif case == "receipt":
                    receipt_sha256 = "0" * 64
                elif case == "changed bytes":
                    task.write_text(text + "changed\n", encoding="utf-8")
                elif case == "missing item":
                    items = items[:1]
                elif case == "altered item":
                    items = ("finish review", "changed blocker")
                elif case == "reordered items":
                    items = tuple(reversed(items))
                elif case == "altered evidence":
                    evidence = "different evidence"
                elif case == "missing claim":
                    (source_state / "completion-email-claims.tsv").unlink()
                elif case == "ambiguous claim":
                    with (source_state / "completion-email-claims.tsv").open("a", encoding="utf-8") as handle:
                        handle.write(f"{receipt.name}\t{owner}\t{task.name}\n")
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}), self.assertRaises(OSError):
                    reconcile_delivered_completion(
                        root,
                        task,
                        outcome,
                        owner,
                        task_sha256,
                        receipt,
                        receipt_sha256,
                        items=items,
                        evidence=evidence,
                    )
                self.assertFalse((manager_state / "completion-email-reconciled").exists())

    def test_executable_entrypoint_delivers_once_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            task.write_text(task_text(), encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as email:
                argv = [
                    "--root",
                    str(root),
                    "--task",
                    str(task),
                    "--outcome",
                    "task done",
                    "--semantic-key",
                    "a" * 64,
                ]
                self.assertEqual(0, main(argv))
                self.assertEqual(0, main(argv))
            email.assert_called_once()
            self.assertTrue(Path(__file__).parents[1].joinpath("omo_completion_email.py").stat().st_mode & 0o111)

    def test_semantic_key_cannot_cross_task_or_owner_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            parent = root / "parent.md"
            child = root / "child.md"
            text = task_text()
            child_text = text.replace("runat: cfg:2", "runat: cfg:3").replace("managerat: cfg:1", "managerat: cfg:2")
            parent.write_text(text, encoding="utf-8")
            child.write_text(child_text, encoding="utf-8")
            semantic_key = "b" * 64

            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                parent_plan = build_completion_email(root, parent, text, "task done", semantic_key=semantic_key)
                child_plan = build_completion_email(root, child, child_text, "task done", semantic_key=semantic_key)
                assert parent_plan is not None and child_plan is not None
                self.assertNotEqual(parent_plan.notice_key, child_plan.notice_key)
                self.assertTrue(claim_completion_email(parent_plan))
                self.assertFalse(completion_email_is_delivered(child_plan))
                self.assertFalse(claim_completion_email(child_plan))
                mark_completion_email_delivered(parent_plan)
                self.assertFalse(completion_email_is_delivered(child_plan))

    def test_completion_entrypoint_rejects_unkeyed_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_text(), encoding="utf-8")
            with patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    main(["--root", str(root), "--task", str(task), "--outcome", "task done"])

    def test_programmatic_sender_rejects_unkeyed_notice_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ):
                plan = plan_completion_email(root, task, text, "task done")
                with self.assertRaisesRegex(ValueError, "semantic completion key"):
                    send_completion_email(plan)
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_missing_or_unexecutable_entrypoint_fails_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            entrypoint = root / "omo_completion_email.py"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            entrypoint.chmod(0o600)
            with patch("omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=task
            ), self.assertRaisesRegex(OSError, "not safely executable"):
                _ = plan_completion_email(root, task, text, "completed")
            entrypoint.chmod(0o700)
            with patch("omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint):
                require_completion_entrypoint()
            entrypoint.unlink()
            with patch("omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint), self.assertRaisesRegex(OSError, "unavailable"):
                require_completion_entrypoint()

    def test_exact_owner_can_send_exact_context_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.subprocess.run"
            ) as run:
                plan = plan_completion_email(
                    root, task, text, "completed", items=("finish review",), evidence="review passed", semantic_key="a" * 64
                )
                self.assertIsNotNone(plan)
                assert plan is not None
                self.assertIn("Task: task.md", plan.body)
                self.assertIn("Outcome: completed", plan.body)
                self.assertIn("- finish review", plan.body)
                self.assertIn("Evidence: review passed", plan.body)
                self.assertTrue(send_completion_email(plan))
                self.assertFalse(send_completion_email(plan))
            run.assert_called_once()
            self.assertNotIn("--tmux-target", run.call_args.args[0])
            self.assertEqual(plan.key, run.call_args.args[0][run.call_args.args[0].index("--completion-authorization") + 1])
            authorization = state / "completion-email-authorizations" / plan.key
            self.assertTrue(authorization.is_file())

    def test_pending_notice_rejects_combined_answer_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), self.assertRaisesRegex(ValueError, "exact thread or body"):
                plan_completion_email(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=("🧑 answer question",),
                    evidence="answered",
                    human_subject="Re: Original question",
                    human_body="The concise answer.\n",
                )

    def test_combined_answer_claim_distinguishes_subject_and_resolved_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                first = plan_completion_email(root, task, text, "completed", items=("first",), human_subject="Re: First", human_body="Done.\n")
                second = plan_completion_email(root, task, text, "completed", items=("second",), human_subject="Re: Second", human_body="Done.\n")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first.key, second.key)

    def test_failed_entrypoint_can_retry_unconsumed_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")
            ) as run:
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                self.assertFalse(send_completion_email(plan))
                self.assertFalse(send_completion_email(plan))
            self.assertEqual(2, run.call_count)

    def test_surviving_notice_marker_repairs_exact_receipt_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as run:
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert plan is not None
                self.assertTrue(send_completion_email(plan))
                receipt = state / "completion-email-delivered" / plan.key
                receipt.unlink()

                self.assertTrue(completion_email_is_delivered(plan))
                self.assertEqual(f"{plan.target}\t{plan.task.name}\t{plan.task_sha256}\n", receipt.read_text(encoding="utf-8"))

            run.assert_called_once()

    def test_notice_marker_with_wrong_valid_receipt_key_cannot_fabricate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run"):
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert plan is not None
                self.assertTrue(send_completion_email(plan))
                notice = state / "completion-notice-delivered" / plan.notice_key
                wrong_key = "0" * 64
                notice.write_text(f"{wrong_key}\t{plan.target}\t{plan.task.name}\t{plan.task_sha256}\n", encoding="utf-8")
                (state / "completion-email-delivered" / plan.key).unlink()

                with self.assertRaisesRegex(OSError, "atomic claim"):
                    completion_email_is_delivered(plan)

            self.assertFalse((state / "completion-email-delivered" / wrong_key).exists())

    def test_surviving_exact_receipt_repairs_notice_marker_after_churn_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as run:
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert plan is not None
                self.assertTrue(send_completion_email(plan))
                notice = state / "completion-notice-delivered" / plan.notice_key
                notice.unlink()

                changed = text + "(verified removed pending item: report sent.)\n"
                task.write_text(changed, encoding="utf-8")
                changed_plan = plan_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                assert changed_plan is not None
                self.assertNotEqual(plan.key, changed_plan.key)
                self.assertEqual(plan.notice_key, changed_plan.notice_key)
                self.assertTrue(completion_email_is_delivered(changed_plan))
                self.assertFalse(notice.exists())

            run.assert_called_once()

    def test_task_record_churn_cannot_duplicate_the_same_delivered_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as run:
                first = plan_completion_email(root, task, initial, "task done", semantic_key="a" * 64)
                assert first is not None
                self.assertTrue(send_completion_email(first))
                changed = initial + "(verified removed pending item: private manager report sent.)\n"
                task.write_text(changed, encoding="utf-8")
                second = plan_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                assert second is not None

                self.assertNotEqual(first.key, second.key)
                self.assertEqual(first.notice_key, second.notice_key)
                self.assertTrue(completion_email_is_delivered(second))

            run.assert_called_once()

    def test_canonical_target_spelling_churn_cannot_duplicate_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as run:
                first = plan_completion_email(root, task, initial, "task done", semantic_key="a" * 64)
                assert first is not None
                self.assertTrue(send_completion_email(first))
                changed = initial.replace("runat: cfg:2", "runat: cfg:2.0").replace("managerat: cfg:1", "managerat: cfg:1.0")
                task.write_text(changed, encoding="utf-8")
                second = plan_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                assert second is not None

                self.assertNotEqual(first.key, second.key)
                self.assertEqual(first.notice_key, second.notice_key)
                self.assertTrue(completion_email_is_delivered(second))

            run.assert_called_once()

    def test_owner_auth_accepts_implicit_pane_zero_for_exact_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "data_gen_mgr.md"
            text = (
                task_text()
                .replace("status: running", "status: blocked\nblocked_on: done_close_failed")
                .replace("runat: cfg:2", "runat: dw:29")
                .replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            )
            task.write_text(text, encoding="utf-8")
            (root / "TODO.md").write_text("current:\ndata_gen_mgr.md dw:29\n", encoding="utf-8")
            entrypoint = root / "omo_completion_email.py"
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            entrypoint.chmod(0o700)

            with patch("omo_manager.omo_task_context.current_tmux_target", return_value="dw:29.0"), patch(
                "omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint
            ):
                plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)

            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual("dw:29", plan.target)

    def test_task_record_churn_cannot_replay_an_uncertain_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")) as run:
                first = plan_completion_email(root, task, initial, "task done", semantic_key="a" * 64)
                assert first is not None
                self.assertFalse(send_completion_email(first))
                changed = initial + "(verified removed pending item: private manager report sent.)\n"
                task.write_text(changed, encoding="utf-8")
                second = plan_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                assert second is not None

                self.assertNotEqual(first.key, second.key)
                self.assertEqual(first.notice_key, second.notice_key)
                self.assertFalse(send_completion_email(second))

            run.assert_called_once()

    def test_exact_unattempted_claim_can_refresh_after_task_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            semantic_key = "a" * 64
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ):
                old = plan_completion_email(root, task, initial, "completed", semantic_key=semantic_key)
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                changed = initial + "(reviewed result)\n"
                task.write_text(changed, encoding="utf-8")
                current = plan_completion_email(
                    root,
                    task,
                    changed,
                    "completed",
                    human_subject="Task result",
                    human_body="The task is complete.",
                    semantic_key=semantic_key,
                )
                assert current is not None

                refresh_unattempted_completion_claim(current, old.key)

                rows = (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines()
                self.assertEqual(1, len(rows))
                self.assertEqual(current.key, rows[0].split("\t")[0])
                self.assertTrue((state / "completion-email-authorizations" / old.key).is_file())
                self.assertTrue((state / "completion-email-authorizations" / current.key).is_file())
                self.assertTrue(claim_completion_email(current, recover_existing=True))

    def test_changed_unattempted_close_can_refresh_a_lost_semantic_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                old = build_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                changed = text + "(manager requested a safe retry)\n"
                task.write_text(changed, encoding="utf-8")
                current = build_completion_email(root, task, changed, "task done", semantic_key="b" * 64)
                assert current is not None

                refresh_unattempted_completion_claim(current, old.key)

                rows = (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines()
                self.assertEqual([current.key], [row.split("\t")[0] for row in rows])
                self.assertTrue(claim_completion_email(current, recover_existing=True))

    def test_semantic_key_refresh_is_close_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
            task.write_text(text, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                old = build_completion_email(root, task, text, "completed", semantic_key="a" * 64)
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                changed = text + "(manager requested a safe retry)\n"
                task.write_text(changed, encoding="utf-8")
                current = build_completion_email(root, task, changed, "completed", semantic_key="b" * 64)
                assert current is not None

                with self.assertRaisesRegex(OSError, "different semantic notice"):
                    refresh_unattempted_completion_claim(current, old.key)

    def test_lost_semantic_key_refresh_rejects_old_notice_delivery_evidence(self) -> None:
        for directory in ("completion-notice-delivered", "ordinary-completion-by-notice"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                task = root / "task.md"
                text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
                task.write_text(text, encoding="utf-8")
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    old = build_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                    assert old is not None
                    self.assertTrue(claim_completion_email(old))
                    changed = text + "(manager requested a safe retry)\n"
                    task.write_text(changed, encoding="utf-8")
                    current = build_completion_email(root, task, changed, "task done", semantic_key="b" * 64)
                    assert current is not None
                    evidence = state / directory / old.notice_key
                    evidence.parent.mkdir(mode=0o700)
                    evidence.write_text("evidence\n", encoding="utf-8")
                    evidence.chmod(0o600)

                    with self.assertRaisesRegex(OSError, "may have been used"):
                        refresh_unattempted_completion_claim(current, old.key)

    def test_lost_semantic_key_refresh_rejects_current_outbound_evidence(self) -> None:
        for directory, identity in (
            ("completion-email-authorization-used", "key"),
            ("completion-email-delivered", "key"),
            ("completion-email-reconciled", "key"),
            ("completion-email-requests", "key"),
            ("completion-notice-delivered", "notice_key"),
            ("ordinary-completion-by-notice", "notice_key"),
        ):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                task = root / "task.md"
                text = task_text().replace("pending_task_items:\n  - finish review", "pending_task_items: []")
                task.write_text(text, encoding="utf-8")
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    old = build_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                    assert old is not None
                    self.assertTrue(claim_completion_email(old))
                    changed = text + "(manager requested a safe retry)\n"
                    task.write_text(changed, encoding="utf-8")
                    current = build_completion_email(root, task, changed, "task done", semantic_key="b" * 64)
                    assert current is not None
                    evidence = state / directory / getattr(current, identity)
                    evidence.parent.mkdir(mode=0o700)
                    evidence.write_text("evidence\n", encoding="utf-8")
                    evidence.chmod(0o600)

                    with self.assertRaisesRegex(OSError, "may have been used"):
                        refresh_unattempted_completion_claim(current, old.key)

    def test_claim_refresh_rejects_any_outbound_boundary_evidence(self) -> None:
        for relative in (
            "completion-email-authorization-used/{key}",
            "completion-email-delivered/{key}",
            "completion-email-reconciled/{key}",
            "completion-email-requests/{key}",
            "completion-notice-delivered/{notice}",
            "ordinary-completion-by-notice/{notice}",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                task = root / "task.md"
                initial = task_text()
                task.write_text(initial, encoding="utf-8")
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    old = build_completion_email(root, task, initial, "task done", semantic_key="a" * 64)
                    assert old is not None
                    self.assertTrue(claim_completion_email(old))
                    changed = initial + "(reviewed result)\n"
                    task.write_text(changed, encoding="utf-8")
                    current = build_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                    assert current is not None
                    evidence = state / relative.format(key=old.key, notice=old.notice_key)
                    evidence.parent.mkdir(mode=0o700, exist_ok=True)
                    evidence.write_text("evidence\n", encoding="utf-8")
                    evidence.chmod(0o600)

                    with self.assertRaisesRegex(OSError, "may have been used"):
                        refresh_unattempted_completion_claim(current, old.key)

                self.assertFalse((state / "completion-email-authorizations" / current.key).exists())

    def test_claim_refresh_rejects_a_different_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                old = build_completion_email(root, task, initial, "task done", semantic_key="a" * 64)
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                changed = initial.replace("runat: cfg:2", "runat: cfg:3")
                task.write_text(changed, encoding="utf-8")
                current = build_completion_email(root, task, changed, "task done", semantic_key="a" * 64)
                assert current is not None

                with self.assertRaisesRegex(OSError, "missing or ambiguous"):
                    refresh_unattempted_completion_claim(current, old.key)

    def test_non_close_entrypoint_refreshes_then_sends_combined_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.md"
            initial = task_text()
            task.write_text(initial, encoding="utf-8")
            subject.write_text("Task result\n", encoding="utf-8")
            message.write_text("The task is complete.\n", encoding="utf-8")
            semantic_key = "a" * 64
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ), patch("omo_manager.omo_completion_email.subprocess.run") as send:
                old = plan_completion_email(root, task, initial, "completed", semantic_key=semantic_key)
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                task.write_text(initial + "(reviewed result)\n", encoding="utf-8")

                result = main(
                    [
                        "--root",
                        str(root),
                        "--task",
                        str(task),
                        "--outcome",
                        "completed",
                        "--semantic-key",
                        semantic_key,
                        "--answer-subject-file",
                        str(subject),
                        "--answer-message-file",
                        str(message),
                        "--refresh-unattempted-claim",
                        old.key,
                    ]
                )

            self.assertEqual(0, result)
            send.assert_called_once()

    def test_exact_and_notice_claims_are_one_atomic_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
            ):
                plan = plan_completion_email(root, task, text, "task done")
                assert plan is not None
                with patch("omo_manager.omo_completion_email.os.replace", side_effect=OSError("injected claim failure")), self.assertRaisesRegex(
                    OSError, "injected claim failure"
                ):
                    claim_completion_email(plan)

            self.assertFalse((state / "completion-email-claims.tsv").exists())
            self.assertFalse((state / "completion-notice-claims.tsv").exists())
            self.assertTrue((state / "completion-email-authorizations" / plan.key).is_file())
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                self.assertTrue(claim_completion_email(plan))

    def test_direct_manager_cannot_fallback_for_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            manager = root / "manager.md"
            text = task_text()
            manager.write_text(task_text().replace("runat: cfg:2", "runat: cfg:1").replace("managerat: cfg:1", "managerat: main:0").replace("is_manager: false", "is_manager: true"), encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=manager):
                self.assertIsNone(plan_completion_email(root, task, text, "task done"))

    def test_manager_or_human_owned_task_cannot_impersonate_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            other = root / "manager.md"
            text = task_text()
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=other):
                self.assertIsNone(plan_completion_email(root, task, text, "task done"))
            human_text = text.replace("runat: cfg:2", "runat: hcfg:2")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                self.assertIsNone(plan_completion_email(root, task, human_text, "task done"))

    def test_explicit_no_contact_rules_suppress_mail(self) -> None:
        rules = (
            "Source-985 suppresses human reporting.",
            "This task is no-contact.",
            "Do not email the human.",
            "Do not send human email.",
            "Never send human-facing message.",
            "Never contact the human.",
            "No human-facing reports until lifted.",
            "Report only privately with omo_report.sh.",
        )
        manager_only_rules = (
            "Report only compact high-level status to this submanager through omo_report.sh.",
            "Report only to the manager.",
            "Return only a concise report to your manager.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                for rule in rules:
                    with self.subTest(rule=rule):
                        self.assertIsNone(plan_completion_email(root, task, task_text(rule), "task done"))
                for rule in manager_only_rules:
                    with self.subTest(rule=rule):
                        self.assertIsNone(plan_completion_email(root, task, task_text(rule, human_report=False), "task done"))

    def test_source1241_exact_meta_reference_does_not_suppress_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text = source1241_task(root)
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                plan = plan_completion_email(root, task, text, "task done")
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertIsNotNone(plan.contact_policy)

    def test_source1241_clarification_rechecks_every_other_suppression(self) -> None:
        for suffix in ("\nDo not email the human.", "\nReturn only a concise report to your manager."):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text = source1241_task(root, body_suffix=suffix, human_report=False)
                with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                    self.assertIsNone(plan_completion_email(root, task, text, "task done"))

    def test_source1241_clarification_rejects_missing_wrong_and_ambiguous_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text = source1241_task(root)
            cases = (
                text.replace(SOURCE1241_ENVELOPE, ""),
                text.replace(SOURCE1241_META_LINE, f"{SOURCE1241_META_LINE}\n{SOURCE1241_META_LINE}"),
                text.replace(
                    SOURCE1241_CONTEXT,
                    '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1241.txt:1-7">\n'
                    f"conflicting body\n</human_instruction>\n{SOURCE1241_CONTEXT}",
                ),
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    task.write_text(changed, encoding="utf-8")
                    with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                        self.assertIsNone(plan_completion_email(root, task, changed, "task done"))
            wrong_source = SOURCE1241_HUMAN.replace("go\nahead", "do not go\nahead")
            task, text = source1241_task(root, source_text=wrong_source)
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                self.assertIsNone(plan_completion_email(root, task, text, "task done"))

    def test_source1241_clarification_rejects_other_task_and_quoted_meta_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text = source1241_task(root)
            other = root / "other.md"
            other.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=other):
                self.assertIsNone(plan_completion_email(root, other, text, "task done"))
            for wrapper in ("<agent_message>\n{}\n</agent_message>", "```text\n{}\n```"):
                with self.subTest(wrapper=wrapper):
                    quoted = text.replace(SOURCE1241_CONTEXT, wrapper.format(SOURCE1241_CONTEXT))
                    task.write_text(quoted, encoding="utf-8")
                    with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                        self.assertIsNone(plan_completion_email(root, task, quoted, "task done"))
            for quoted in (
                text.replace(SOURCE1241_CONTEXT, f"<agent_message>prefix\n{SOURCE1241_CONTEXT}\n</agent_message>"),
                text.replace(SOURCE1241_CONTEXT, f"prefix <agent_message>\n{SOURCE1241_CONTEXT}\n</agent_message>"),
                text.replace(SOURCE1241_CONTEXT, f"````text\n```\n{SOURCE1241_CONTEXT}\n````"),
                text.replace(SOURCE1241_CONTEXT, f"<!--\n{SOURCE1241_CONTEXT}\n-->"),
                text.replace(SOURCE1241_CONTEXT, f"<!--\n<!-- nested -->\n-->\n{SOURCE1241_CONTEXT}"),
                text.replace(SOURCE1241_CONTEXT, f"<outer>\n</outer><agent_message>\n{SOURCE1241_CONTEXT}"),
                text.replace(SOURCE1241_CONTEXT, f"Narrative quote: {SOURCE1241_CONTEXT} extra"),
            ):
                with self.subTest(quoted=quoted):
                    task.write_text(quoted, encoding="utf-8")
                    with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                        self.assertIsNone(plan_completion_email(root, task, quoted, "task done"))

    def test_source1241_clarification_preserves_owner_and_human_target_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text = source1241_task(root)
            other = root / "other.md"
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=other):
                self.assertIsNone(plan_completion_email(root, task, text, "task done"))
            human_text = text.replace("runat: cfg:2", "runat: hcfg:2")
            task.write_text(human_text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                self.assertIsNone(plan_completion_email(root, task, human_text, "task done"))

    def test_source1241_clarification_fails_closed_on_task_or_source_drift_before_claim(self) -> None:
        for drift in ("task", "source"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                task, text = source1241_task(root)
                with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch.dict(
                    "os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}
                ):
                    plan = plan_completion_email(root, task, text, "task done", semantic_key="a" * 64)
                    assert plan is not None
                    changed = task if drift == "task" else root / "manager_mail/85c5dff58359-1241.txt"
                    changed.write_text(changed.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
                    with patch("omo_manager.omo_completion_email.subprocess.run") as email:
                        self.assertFalse(send_completion_email(plan))
                    email.assert_not_called()
                    self.assertFalse((state / "completion-email-claims.tsv").exists())

    def watcher_pangram_fixture(self, root: Path) -> tuple[Path, str, tuple[str, ...]]:
        items = omo_completion_email.WATCHER_PANGRAM_ITEMS
        queue = tuple(f"unrelated item {index}" for index in range(7)) + items + ("unrelated item after",)
        rendered = "".join(f"  - '{item.replace(chr(39), chr(39) * 2)}'\n" for item in queue)
        text = (
            "---\n"
            "version: v1.0.0\n"
            "status: running\n"
            "runat: config:35\n"
            "tool: codex\n"
            "managerat: config:39\n"
            "is_manager: false\n"
            "pending_task_items:\n"
            f"{rendered}"
            "---\n"
            "do not send another Human email\n"
        )
        task = root / "watcher_repair.md"
        task.write_text(text, encoding="utf-8")
        return task, text, queue

    def test_watcher_pangram_reviewed_sent_removes_only_exact_items_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            (state / "completion-email-claims.tsv").write_text("", encoding="utf-8")
            (state / "completion-email-claims.tsv").chmod(0o600)
            task, text, queue = self.watcher_pangram_fixture(root)
            constants = {
                "WATCHER_PANGRAM_ROOT": str(root.resolve()),
                "WATCHER_PANGRAM_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
                "WATCHER_PANGRAM_QUEUE_SHA256": omo_pending.pending_queue_sha256(queue),
                "WATCHER_PANGRAM_PURPOSE_SHA256": ordinary_pending_purpose(
                    "pending item removed after verification",
                    omo_completion_email.WATCHER_PANGRAM_ITEMS,
                    omo_completion_email.WATCHER_PANGRAM_EVIDENCE,
                ),
            }
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch.multiple(
                omo_completion_email, **constants
            ), patch("omo_manager.omo_pending.current_pending_task", return_value=task), patch(
                "omo_manager.omo_completion_email.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True), patch(
                "omo_manager.omo_completion_email.subprocess.run"
            ) as email_process:
                normal = build_completion_email(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=omo_completion_email.WATCHER_PANGRAM_ITEMS,
                    evidence=omo_completion_email.WATCHER_PANGRAM_EVIDENCE,
                    semantic_key=constants["WATCHER_PANGRAM_PURPOSE_SHA256"],
                )
                self.assertIsNone(normal)
                plan = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=omo_completion_email.WATCHER_PANGRAM_ITEMS,
                    evidence=omo_completion_email.WATCHER_PANGRAM_EVIDENCE,
                    semantic_key=constants["WATCHER_PANGRAM_PURPOSE_SHA256"],
                )
                assert plan is not None
                self.assertFalse(plan.send_allowed)
                with self.assertRaisesRegex(OSError, "cannot send"):
                    send_completion_email(plan)
                self.assertEqual(0, omo_pending.run(omo_pending.parse_args(["recover-watcher-pangram-reviewed-sent"]), root))
                email_process.assert_not_called()
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert metadata is not None
            self.assertEqual((*queue[:7], queue[-1]), metadata.pending_task_items)

    def test_watcher_pangram_reviewed_sent_rejects_drift_and_message_reuse_before_task_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            (state / "completion-email-claims.tsv").write_text("", encoding="utf-8")
            (state / "completion-email-claims.tsv").chmod(0o600)
            task, text, queue = self.watcher_pangram_fixture(root)
            constants = {
                "WATCHER_PANGRAM_ROOT": str(root.resolve()),
                "WATCHER_PANGRAM_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
                "WATCHER_PANGRAM_QUEUE_SHA256": omo_pending.pending_queue_sha256(queue),
                "WATCHER_PANGRAM_PURPOSE_SHA256": ordinary_pending_purpose(
                    "pending item removed after verification",
                    omo_completion_email.WATCHER_PANGRAM_ITEMS,
                    omo_completion_email.WATCHER_PANGRAM_EVIDENCE,
                ),
            }
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch.multiple(
                omo_completion_email, **constants
            ), patch("omo_manager.omo_pending.current_pending_task", return_value=task), patch(
                "omo_manager.omo_completion_email.current_pending_task", return_value=task
            ), patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True):
                args = omo_pending.parse_args(["recover-watcher-pangram-reviewed-sent"])
                request = omo_pending.sent_recovery_request(args)
                plan = plan_sent_recovery_completion(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=args.items,
                    evidence=args.evidence,
                    semantic_key=request.semantic_key,
                )
                assert plan is not None
                with self.assertRaisesRegex(OSError, "does not bind"):
                    omo_completion_email.validate_watcher_pangram_reviewed_sent_authority(
                        root, plan, args.items, args.evidence, replace(request, sent_body_sha256="0" * 64), text
                    )
                task.write_text(text + "task drift\n", encoding="utf-8")
                with self.assertRaisesRegex(omo_pending.BlockingError, "task bytes changed"):
                    omo_pending.run(args, root)
                self.assertEqual(text + "task drift\n", task.read_text(encoding="utf-8"))
                task.write_text(text, encoding="utf-8")
                message_dir = state / "ordinary-completion-by-message"
                message_dir.mkdir(mode=0o700, parents=True)
                marker = message_dir / hashlib.sha256(request.message_id.encode()).hexdigest()
                marker.write_text("different transition\n", encoding="utf-8")
                marker.chmod(0o600)
                with self.assertRaisesRegex(OSError, "Message-ID is already bound"):
                    omo_pending.run(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))

    def mail_compress_fixture(
        self,
        root: Path,
        state: Path,
    ) -> tuple[Path, str, dict[str, object], tuple[str, ...], tuple[str, ...]]:
        items = omo_completion_email.MAIL_COMPRESS_ITEMS

        def task_payload(queue: tuple[str, ...], body: str) -> str:
            rendered = "".join(f"  - '{item.replace(chr(39), chr(39) * 2)}'\n" for item in queue)
            return (
                "---\n"
                "version: v1.0.0\n"
                "status: blocked\n"
                f"blocked_on: {omo_completion_email.MAIL_COMPRESS_BLOCKED_ON}\n"
                "runat: config:44\n"
                "tool: codex\n"
                "managerat: config:27\n"
                "is_manager: false\n"
                "pending_task_items:\n"
                f"{rendered}"
                "---\n"
                f"{body}"
            )

        task = root / omo_completion_email.MAIL_COMPRESS_TASK
        initial = task_payload((omo_completion_email.MAIL_COMPRESS_REMOVED_ITEM, *items), "mailbox work complete\n")
        task.write_text(initial, encoding="utf-8")

        def stale_binding(item: str, label: str) -> tuple[str, str, str, str, str]:
            semantic_key = hashlib.sha256(f"mail-compress-failed-{label}".encode()).hexdigest()
            plan = build_completion_email(
                root,
                task,
                task.read_text(encoding="utf-8"),
                "pending item removed after verification",
                items=(item,),
                evidence=f"failed {label}",
                semantic_key=semantic_key,
            )
            assert plan is not None
            self.assertTrue(claim_completion_email(plan))
            authorization = state / "completion-email-authorizations" / plan.key
            return (
                plan.key,
                plan.task_sha256,
                plan.manager_target,
                plan.notice_semantic_key,
                hashlib.sha256(authorization.read_bytes()).hexdigest(),
            )

        failed_main = stale_binding(omo_completion_email.MAIL_COMPRESS_REMOVED_ITEM, "main")
        failed_streaming = stale_binding(items[0], "streaming")
        failed_diagnosis = stale_binding(items[1], "diagnosis")

        def delivered_binding(item: str, label: str, message_id: str) -> tuple[str, ...]:
            semantic_key = hashlib.sha256(f"mail-compress-delivered-{label}".encode()).hexdigest()
            plan = build_completion_email(
                root,
                task,
                task.read_text(encoding="utf-8"),
                "pending item removed after verification",
                items=(item,),
                evidence=f"delivered {label}",
                semantic_key=semantic_key,
            )
            assert plan is not None
            self.assertTrue(claim_completion_email(plan))
            used_dir = state / "completion-email-authorization-used"
            used_dir.mkdir(mode=0o700, exist_ok=True)
            used = used_dir / plan.key
            used.write_text(f"{plan.target}\t{task.name}\n", encoding="utf-8")
            used.chmod(0o600)
            mark_completion_email_delivered(plan)
            authorization = state / "completion-email-authorizations" / plan.key
            return (
                item,
                plan.key,
                plan.task_sha256,
                plan.manager_target,
                plan.notice_key,
                plan.notice_semantic_key,
                hashlib.sha256(authorization.read_bytes()).hexdigest(),
                message_id,
                hashlib.sha256(f"{label} subject".encode()).hexdigest(),
                hashlib.sha256(f"{label} body".encode()).hexdigest(),
            )

        delivered_main = delivered_binding(
            omo_completion_email.MAIL_COMPRESS_REMOVED_ITEM,
            "main",
            "<mail-compress-main@example.test>",
        )
        task.write_text(task_payload(items, "mailbox work complete\nfirst item removed\n"), encoding="utf-8")
        delivered_streaming = delivered_binding(
            items[0],
            "streaming",
            "<mail-compress-streaming@example.test>",
        )
        text = task_payload(
            (*items, *omo_completion_email.MAIL_COMPRESS_PRESERVED_ITEMS),
            f"mailbox work complete\nfirst item removed\n{omo_completion_email.MAIL_COMPRESS_STOP_LINE}\n",
        )
        task.write_text(text, encoding="utf-8")
        failed = (failed_streaming, failed_diagnosis, failed_main)
        delivered = (delivered_main, delivered_streaming)
        constants: dict[str, object] = {
            "MAIL_COMPRESS_ROOT": str(root.resolve()),
            "MAIL_COMPRESS_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
            "MAIL_COMPRESS_QUEUE_SHA256": digest_fields(
                "pending-queue-v1",
                *items,
                *omo_completion_email.MAIL_COMPRESS_PRESERVED_ITEMS,
            ),
            "MAIL_COMPRESS_PURPOSE_SHA256": ordinary_pending_purpose(
                "pending item removed after verification",
                items,
                omo_completion_email.MAIL_COMPRESS_EVIDENCE,
            ),
            "MAIL_COMPRESS_FAILED_CLAIMS": failed,
            "MAIL_COMPRESS_DELIVERED_CLAIMS": delivered,
            "MAIL_COMPRESS_MESSAGE_ID": "<mail-compress-final@example.test>",
            "MAIL_COMPRESS_SUBJECT_SHA256": hashlib.sha256(b"final subject").hexdigest(),
            "MAIL_COMPRESS_BODY_SHA256": hashlib.sha256(b"final body").hexdigest(),
        }
        return task, text, constants, tuple(binding[0] for binding in failed), tuple(binding[1] for binding in delivered)

    def test_mail_compress_reviewed_sent_reconciles_two_items_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, failed_keys, delivered_keys = self.mail_compress_fixture(root, state)
                delivered_before = {
                    path.relative_to(state): path.read_bytes()
                    for key in delivered_keys
                    for directory in (
                        "completion-email-authorizations",
                        "completion-email-authorization-used",
                        "completion-email-delivered",
                    )
                    if (path := state / directory / key).is_file()
                }
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
                ) as sent, patch("omo_manager.omo_pending.require_owner_completion") as owner_sender, patch(
                    "omo_manager.omo_completion_email.send_completion_email"
                ) as sender, patch("omo_manager.omo_completion_email.subprocess.run") as email_process:
                    args = omo_pending.parse_args(["recover-mail-compress-reviewed-sent"])
                    request = omo_pending.sent_recovery_request(args)
                    plan = plan_sent_recovery_completion(
                        root,
                        task,
                        text,
                        "pending item removed after verification",
                        items=args.items,
                        evidence=args.evidence,
                        semantic_key=request.semantic_key,
                    )
                    assert plan is not None
                    self.assertFalse(plan.send_allowed)
                    self.assertEqual(0, omo_pending.run(args, root))
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual(omo_completion_email.MAIL_COMPRESS_PRESERVED_ITEMS, metadata.pending_task_items)
                    rows = [
                        line.split("\t")
                        for line in (state / "completion-email-claims.tsv").read_text(encoding="utf-8").splitlines()
                    ]
                    for key in failed_keys:
                        selected = [row for row in rows if row[0] == key]
                        self.assertEqual(1, len(selected))
                        self.assertTrue(selected[0][1].startswith("retired:"))
                        self.assertTrue((state / "completion-email-retired-authorizations" / key).is_file())
                    self.assertEqual(
                        delivered_before,
                        {
                            path.relative_to(state): path.read_bytes()
                            for key in delivered_keys
                            for directory in (
                                "completion-email-authorizations",
                                "completion-email-authorization-used",
                                "completion-email-delivered",
                            )
                            if (path := state / directory / key).is_file()
                        },
                    )
                    first_snapshot = {
                        path.relative_to(root): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(0, omo_pending.run(args, root))
                    self.assertEqual(
                        first_snapshot,
                        {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()},
                    )
                self.assertEqual(3, sent.call_count)
                owner_sender.assert_not_called()
                sender.assert_not_called()
                email_process.assert_not_called()

    def test_mail_compress_reviewed_sent_rejects_drift_before_task_or_claim_mutation(self) -> None:
        for drift in ("task", "sent", "delivered-claim"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    task, text, constants, _failed_keys, delivered_keys = self.mail_compress_fixture(root, state)
                    ledger = state / "completion-email-claims.tsv"
                    if drift == "task":
                        task.write_text(text + "drift\n", encoding="utf-8")
                    elif drift == "delivered-claim":
                        marker = state / "completion-email-delivered" / delivered_keys[1]
                        marker.write_text("changed\n", encoding="utf-8")
                    before_task = task.read_bytes()
                    before_claims = ledger.read_bytes()
                    sent_patch = patch(
                        "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent",
                        side_effect=[True, False] if drift == "sent" else None,
                        return_value=drift != "sent",
                    )
                    with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                        "omo_manager.omo_pending.current_pending_task", return_value=task
                    ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), sent_patch, patch(
                        "omo_manager.omo_completion_email.subprocess.run"
                    ) as email_process:
                        args = omo_pending.parse_args(["recover-mail-compress-reviewed-sent"])
                        error = "task bytes changed|Sent-Mail evidence|delivered claim evidence changed"
                        with self.assertRaisesRegex((OSError, omo_pending.BlockingError), error):
                            omo_pending.run(args, root)
                    self.assertEqual(before_task, task.read_bytes())
                    self.assertEqual(before_claims, ledger.read_bytes())
                    email_process.assert_not_called()

    def test_pending_remove_reports_adopted_notice_without_claiming_a_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_text().replace("  - finish review", "  - 🧑 finish review"), encoding="utf-8")
            output = StringIO()
            with patch("omo_manager.omo_pending.current_pending_task", return_value=task), patch(
                "omo_manager.omo_pending.plan_completion_email", return_value=object()
            ), patch("omo_manager.omo_pending.completion_email_is_delivered", return_value=True), patch(
                "omo_manager.omo_pending.require_owner_completion", return_value=True
            ), redirect_stdout(output):
                self.assertEqual(
                    0,
                    omo_pending.run(
                        omo_pending.Args(
                            "remove",
                            ("🧑 finish review",),
                            evidence="verified existing Sent-Mail evidence",
                            completion_key="a" * 64,
                        ),
                        root,
                    ),
                )
            self.assertIn("Verified the Human completion notice.", output.getvalue())
            self.assertNotIn("Emailed the human", output.getvalue())

    def test_manager_summary_rule_does_not_override_explicit_direct_human_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            body = "Report substantive results directly to the human. Return only a concise report to your manager."
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                self.assertIsNotNone(plan_completion_email(root, task, task_text(body), "task done"))


if __name__ == "__main__":
    unittest.main()
