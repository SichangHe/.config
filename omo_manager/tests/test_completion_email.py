from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_completion_email import build_completion_email
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import reconcile_delivered_completion
from omo_manager.omo_completion_email import require_completion_entrypoint
from omo_manager.omo_completion_email import require_owner_completion
from omo_manager.omo_completion_email import main
from omo_manager.omo_completion_email import mark_completion_email_delivered
from omo_manager.omo_completion_email import mark_completion_email_request_queued
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_completion_email import SOURCE1241_ENVELOPE
from omo_manager.omo_completion_email import SOURCE1241_CONTEXT
from omo_manager.omo_completion_email import SOURCE1241_HUMAN
from omo_manager.omo_completion_email import SOURCE1241_META_LINE


def task_text(body: str = "") -> str:
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
        f"{body}\n"
    )


def source1241_task(root: Path, *, body_suffix: str = "", source_text: str = SOURCE1241_HUMAN) -> tuple[Path, str]:
    source = root / "manager_mail/85c5dff58359-1241.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text(source_text + "\n", encoding="utf-8")
    source.chmod(0o600)
    task = root / "hmanager_replace_fix.md"
    text = task_text(f"{SOURCE1241_CONTEXT}{body_suffix}")
    task.write_text(text, encoding="utf-8")
    return task, text


class CompletionEmailTest(unittest.TestCase):
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

    def test_v1_pending_to_done_transition_reuses_one_semantic_delivery(self) -> None:
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
                    items=("finish review",),
                    evidence="review passed",
                    human_subject="Reviewed work complete",
                    human_body="The pending item is complete.",
                    semantic_key=semantic_key,
                )
                assert pending_plan is not None
                self.assertTrue(send_completion_email(pending_plan))
                finished = initial.replace("pending_task_items:\n  - finish review", "pending_task_items: []")
                task.write_text(finished, encoding="utf-8")
                done_plan = plan_completion_email(root, task, finished, "task done", semantic_key=semantic_key)
                assert done_plan is not None
                self.assertNotEqual(pending_plan.key, done_plan.key)
                self.assertEqual(pending_plan.notice_key, done_plan.notice_key)
                self.assertTrue(completion_email_is_delivered(done_plan))
                self.assertFalse(send_completion_email(done_plan))
            send.assert_called_once()

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

    def test_v2_pending_to_done_transition_reuses_one_semantic_notice(self) -> None:
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
    text: finish review
    blocked_on: []
    notices: []
resolved_task_items: []
---
work
"""
            task.write_text(initial, encoding="utf-8")
            first = build_completion_email(
                root,
                task,
                initial,
                "pending item completed",
                items=("finish review",),
                evidence="review passed",
                semantic_key="a" * 64,
            )
            finished = initial.replace(
                "pending_task_items:\n  - id: pi_019f0000-0000-7000-8000-000000000002\n"
                "    text: finish review\n    blocked_on: []\n    notices: []",
                "pending_task_items: []",
            )
            task.write_text(finished, encoding="utf-8")
            second = build_completion_email(root, task, finished, "task done", semantic_key="a" * 64)
            assert first is not None and second is not None
            self.assertNotEqual(first.key, second.key)
            self.assertEqual(first.notice_key, second.notice_key)

    def test_guest_lifecycle_notice_is_suppressed_without_changing_primary_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "primary.md"
            primary_text = task_text()
            primary.write_text(primary_text, encoding="utf-8")
            primary_plan = build_completion_email(root, primary, primary_text, "task done")
            assert primary_plan is not None
            self.assertEqual("primary.md: task done", primary_plan.subject)
            self.assertEqual("Task: primary.md\nOutcome: task done\n", primary_plan.body)

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
                        root, task, text, "pending item completed", items=("finish review",), evidence="passed", semantic_key="d" * 64
                    )
                )
                self.assertFalse(
                    require_owner_completion(
                        root, task, text, "pending item completed", items=("finish review",), evidence="passed", semantic_key="d" * 64
                    )
                )
            queue.assert_called_once()
            target, message = queue.call_args.args
            self.assertEqual("cfg:2", target)
            self.assertIn(str(Path(__file__).parents[1] / "omo_completion_email.py"), message)
            self.assertIn("--item 'finish review'", message)
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
                f"{receipt.name}\tcfg:2\t{task.name}\tcfg:1\t{original_sha256}\t{original_plan.notice_key}\t{'c' * 64}\n",
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

    def test_combined_answer_is_one_email_with_exact_completion_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                plan = plan_completion_email(
                    root,
                    task,
                    text,
                    "pending item removed after verification",
                    items=("answer question",),
                    evidence="answered",
                    human_subject="Re: Original question",
                    human_body="The concise answer.\n",
                )
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual("Re: Original question", plan.subject)
            self.assertTrue(plan.body.startswith("The concise answer.\n\nCompletion record:\n"))
            self.assertIn("Task: task.md", plan.body)
            self.assertIn("Outcome: pending item removed after verification", plan.body)
            self.assertIn("- answer question", plan.body)
            self.assertIn("Evidence: answered", plan.body)

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
                task, text = source1241_task(root, body_suffix=suffix)
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

    def test_manager_summary_rule_does_not_override_explicit_direct_human_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            body = "Report substantive results directly to the human. Return only a concise report to your manager."
            with patch("omo_manager.omo_completion_email.current_active_task", return_value=task):
                self.assertIsNotNone(plan_completion_email(root, task, task_text(body), "task done"))


if __name__ == "__main__":
    unittest.main()
