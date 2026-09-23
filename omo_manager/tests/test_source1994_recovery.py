from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_completion_email
from omo_manager import omo_pending
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_completion_email import build_completion_email
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import digest_fields
from omo_manager.omo_completion_email import plan_sent_recovery_completion
from omo_manager.omo_task_edit import pending_remove_evidence_comment
from omo_manager.omo_task_metadata import parse_task_metadata


PARTICIPANT_EVIDENCE = (hashlib.sha256(b"agent@example.test").hexdigest(), hashlib.sha256(b"human@example.test").hexdigest())


class Source1994RecoveryTest(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        state: Path,
    ) -> tuple[Path, str, dict[str, object], str, str]:
        text = (
            "---\n"
            "version: v1.0.0\n"
            "status: blocked\n"
            f"blocked_on: {omo_completion_email.SOURCE1994_BLOCKED_ON}\n"
            "runat: dw3:0\n"
            "tool: codex\n"
            "managerat: dw:61\n"
            "is_manager: false\n"
            "pending_task_items: []\n"
            "---\n"
            f"{omo_completion_email.SOURCE1994_SOURCE_POINTER}\n"
            f"{omo_completion_email.SOURCE1994_SOURCE1977_COMPLETION_LINE}\n"
        )
        task = root / omo_completion_email.SOURCE1994_TASK
        task.write_text(text, encoding="utf-8")
        authority = root / omo_completion_email.SOURCE1994_PATH
        authority.parent.mkdir(parents=True)
        authority.write_text(
            "Subject: Re: Correcting sample-size evaluation plots\n\n"
            + "\n".join(omo_completion_email.SOURCE1994_AUTHORITY_LINES)
            + "\n",
            encoding="utf-8",
        )
        authority.chmod(0o600)

        def unused_binding(item: str, label: str) -> tuple[str, str, str, str, str]:
            plan = build_completion_email(
                root,
                task,
                text,
                "pending item created",
                items=(item,),
                semantic_key=hashlib.sha256(label.encode()).hexdigest(),
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

        stale = unused_binding(omo_completion_email.SOURCE1994_ITEMS[0], "source1994-stale-add")
        unrelated = unused_binding("🧑 unrelated Human request", "source1994-unrelated")
        constants: dict[str, object] = {
            "SOURCE1994_ROOT": str(root.resolve()),
            "SOURCE1994_SHA256": hashlib.sha256(authority.read_bytes()).hexdigest(),
            "SOURCE1994_TASK_SHA256": hashlib.sha256(text.encode()).hexdigest(),
            "SOURCE1994_QUEUE_SHA256": digest_fields("pending-queue-v1"),
            "SOURCE1994_AFTER_QUEUE_SHA256": digest_fields(
                "pending-queue-v1",
                omo_completion_email.SOURCE1994_ITEMS[-1],
            ),
            "SOURCE1994_STALE_ADD_CLAIM": stale,
        }
        return task, text, constants, stale[0], unrelated[0]

    def test_authority_binds_source_owner_ack_result_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, _stale, _unrelated = self.fixture(root, state)
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_completion_email.current_pending_task", return_value=task
                ), patch(
                    "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE
                ) as sent, patch(
                    "omo_manager.omo_completion_email.git_output",
                    side_effect=[f"{omo_completion_email.SOURCE1994_RESULT_COMMIT}\n".encode(), b""],
                ) as git:
                    request = omo_completion_email.source1994_plot_recovery_request()
                    plan = plan_sent_recovery_completion(
                        root,
                        task,
                        text,
                        "pending item removed after verification",
                        items=omo_completion_email.SOURCE1994_COMPLETED_ITEMS,
                        evidence=omo_completion_email.SOURCE1994_EVIDENCE,
                        semantic_key=request.semantic_key,
                    )
                    assert plan is not None
                    omo_completion_email.validate_source1994_plot_authority(
                        root,
                        plan,
                        omo_completion_email.SOURCE1994_COMPLETED_ITEMS,
                        omo_completion_email.SOURCE1994_EVIDENCE,
                        request,
                        text,
                    )
                    sent.assert_called_once_with(
                        omo_completion_email.SOURCE1994_ACK_MESSAGE_ID,
                        omo_completion_email.SOURCE1994_ACK_SUBJECT_SHA256,
                        omo_completion_email.SOURCE1994_ACK_BODY_SHA256,
                    )
                    self.assertEqual(2, git.call_count)
                    with self.assertRaisesRegex(OSError, "does not bind"):
                        omo_completion_email.validate_source1994_plot_authority(
                            root,
                            plan,
                            omo_completion_email.SOURCE1994_COMPLETED_ITEMS,
                            omo_completion_email.SOURCE1994_EVIDENCE,
                            replace(request, sent_body_sha256="0" * 64),
                            text,
                        )
                    source = root / omo_completion_email.SOURCE1994_PATH
                    source.write_text(source.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
                    with self.assertRaisesRegex(OSError, "does not bind"):
                        omo_completion_email.validate_source1994_plot_authority(
                            root,
                            plan,
                            omo_completion_email.SOURCE1994_COMPLETED_ITEMS,
                            omo_completion_email.SOURCE1994_EVIDENCE,
                            request,
                            text,
                        )

    def test_cli_records_six_resolves_five_replays_and_never_sends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, text, constants, stale, unrelated = self.fixture(root, state)
                ledger = state / "completion-email-claims.tsv"
                unrelated_row = next(
                    line
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                    if line.startswith(unrelated + "\t")
                )
                unrelated_authorization = (state / "completion-email-authorizations" / unrelated).read_bytes()
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1994_plot_authority"
                ) as authority, patch(
                    "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE
                ) as sent, patch(
                    "omo_manager.omo_pending.require_owner_completion"
                ) as owner_sender, patch(
                    "omo_manager.omo_completion_email.send_completion_email"
                ) as sender, patch(
                    "omo_manager.omo_completion_email.subprocess.run"
                ) as email_process, redirect_stdout(StringIO()):
                    args = omo_pending.parse_args(["recover-source1994-plot"])
                    self.assertEqual(omo_completion_email.SOURCE1994_ITEMS, args.items)
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    updated = task.read_text(encoding="utf-8")
                    metadata = parse_task_metadata(updated, root)
                    assert metadata is not None
                    self.assertEqual("blocked", metadata.status)
                    self.assertEqual(omo_completion_email.SOURCE1994_BLOCKED_ON, metadata.blocked_on)
                    self.assertEqual((omo_completion_email.SOURCE1994_ITEMS[-1],), metadata.pending_task_items)
                    self.assertEqual(
                        1,
                        updated.splitlines().count(omo_completion_email.SOURCE1994_SOURCE1977_COMPLETION_LINE),
                    )
                    for item in omo_completion_email.SOURCE1994_ITEMS:
                        record = f"({omo_pending.source1994_pending_record_comment(item)})"
                        self.assertEqual(1, updated.splitlines().count(record))
                    evidence = f"({pending_remove_evidence_comment(5, omo_completion_email.SOURCE1994_EVIDENCE)})"
                    self.assertEqual(1, updated.splitlines().count(evidence))
                    rows = ledger.read_text(encoding="utf-8").splitlines()
                    stale_row = next(line for line in rows if line.startswith(stale + "\t"))
                    self.assertIn("\tretired:", stale_row)
                    self.assertIn(unrelated_row, rows)
                    self.assertEqual(
                        unrelated_authorization,
                        (state / "completion-email-authorizations" / unrelated).read_bytes(),
                    )
                    self.assertTrue((state / "completion-email-retired-authorizations" / stale).is_file())
                    transitions = list((state / "ordinary-pending-transitions").iterdir())
                    self.assertEqual(1, len(transitions))
                    self.assertIn('"status":"committed"', transitions[0].read_text(encoding="utf-8"))
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
                authority.assert_called_once()
                sent.assert_called_once_with(
                    omo_completion_email.SOURCE1994_RESULT_MESSAGE_ID,
                    omo_completion_email.SOURCE1994_RESULT_SUBJECT_SHA256,
                    omo_completion_email.SOURCE1994_RESULT_BODY_SHA256,
                )
                owner_sender.assert_not_called()
                sender.assert_not_called()
                email_process.assert_not_called()
                self.assertNotEqual(text, task.read_text(encoding="utf-8"))

    def test_replays_prepared_transition_after_task_replace_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                task, _text, constants, _stale, _unrelated = self.fixture(root, state)
                with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                    "omo_manager.omo_pending.current_pending_task", return_value=task
                ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.validate_source1994_plot_authority"
                ) as authority, patch(
                    "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE
                ) as sent, patch(
                    "omo_manager.omo_completion_email.send_completion_email"
                ) as sender, redirect_stdout(StringIO()):
                    args = omo_pending.parse_args(["recover-source1994-plot"])
                    with patch("omo_manager.omo_pending.fsync_task_parent", side_effect=OSError("crash after replace")):
                        with self.assertRaisesRegex(OSError, "crash after replace"):
                            omo_pending.run(args, root=root)
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertEqual((omo_completion_email.SOURCE1994_ITEMS[-1],), metadata.pending_task_items)
                    transition = next((state / "ordinary-pending-transitions").iterdir())
                    self.assertIn('"status":"prepared"', transition.read_text(encoding="utf-8"))
                    self.assertEqual(0, omo_pending.run(args, root=root))
                    self.assertIn('"status":"committed"', transition.read_text(encoding="utf-8"))
                authority.assert_called_once()
                sent.assert_called_once()
                sender.assert_not_called()

    def test_rejects_task_result_and_claim_drift_before_task_mutation(self) -> None:
        for drift in ("task", "result", "claim"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}):
                    task, text, constants, stale, _unrelated = self.fixture(root, state)
                    ledger = state / "completion-email-claims.tsv"
                    if drift == "task":
                        task.write_text(text + "drift\n", encoding="utf-8")
                    elif drift == "claim":
                        authorization = state / "completion-email-authorizations" / stale
                        authorization.write_text(
                            authorization.read_text(encoding="utf-8") + "drift=1\n",
                            encoding="utf-8",
                        )
                    task_before = task.read_bytes()
                    claims_before = ledger.read_bytes()
                    with patch.multiple(omo_completion_email, **constants), patch(  # pyright: ignore[reportCallIssue, reportArgumentType]
                        "omo_manager.omo_pending.current_pending_task", return_value=task
                    ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=task), patch(
                        "omo_manager.omo_completion_email.validate_source1994_plot_authority"
                    ), patch(
                        "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent",
                        return_value=None if drift == "result" else PARTICIPANT_EVIDENCE,
                    ), redirect_stdout(StringIO()):
                        error = "task bytes changed|Sent-Mail evidence|authorization digest"
                        with self.assertRaisesRegex((OSError, BlockingError), error):
                            omo_pending.run(omo_pending.parse_args(["recover-source1994-plot"]), root=root)
                    self.assertEqual(task_before, task.read_bytes())
                    self.assertEqual(claims_before, ledger.read_bytes())


if __name__ == "__main__":
    unittest.main()
