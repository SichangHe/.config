from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_pending import Args
from omo_manager.omo_pending import REMOVAL_NOTICE_RECOVERIES
from omo_manager.omo_pending import SOURCE1929_RECOVERY_ID
from omo_manager.omo_pending import SOURCE2048_BATCH_PATH_EVIDENCE
from omo_manager.omo_pending import SOURCE2048_BATCH_PATH_ITEM
from omo_manager.omo_pending import SOURCE2048_DELIVERY_MESSAGE_SHA256
from omo_manager.omo_pending import SOURCE2048_PAPER_CWD
from omo_manager.omo_pending import SOURCE2048_PAPER_SESSION
from omo_manager.omo_pending import SOURCE2048_PAPER_TRANSCRIPT
from omo_manager.omo_pending import SOURCE2048_BROADER_ITEM
from omo_manager.omo_pending import SOURCE2048_DELIVERY_EVIDENCE
from omo_manager.omo_pending import SOURCE2048_DELIVERY_MESSAGE_ID
from omo_manager.omo_pending import SOURCE2048_DELIVERY_SUPPORT_ITEM
from omo_manager.omo_pending import SOURCE2048_SENT_BODY_SHA256
from omo_manager.omo_pending import SOURCE2048_SENT_SUBJECT_SHA256
from omo_manager.omo_pending import RemovalNoticeRecovery
from omo_manager.omo_pending import parse_args
from omo_manager.omo_pending import run
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import refresh_unattempted_completion_claim
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_task_context import infer_active_task
from omo_manager.omo_task_context import infer_pending_task
from omo_manager.omo_task_metadata import frontmatter_parts


PARTICIPANT_EVIDENCE = (hashlib.sha256(b"agent@example.test").hexdigest(), hashlib.sha256(b"human@example.test").hexdigest())
SOURCE2048_REVIEWED_DELIVERY_MESSAGE = """The reviewed normal owner-local batch path is ready. It sends one owner-authenticated final answer, then removes exactly the four listed Source-2048 Human items in one guarded mutation. It does not list or remove the distinct broader review item.

After the final integrated wording review passes, write the final email subject as one line to `/tmp/source2048-final-subject.txt` and the final reviewed email body to `/tmp/source2048-final-message.txt`. Then run this exact command once from `DeGenTWeb_writeup:0`:

```sh
completion_key=$(python3 -c 'import hashlib, secrets; print(hashlib.sha256(secrets.token_bytes(32)).hexdigest())')
omo_pending.py remove \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Draft the supplied 605-page Pangram-versus-Binoculars comparison into the paper, preserving that the observed cohort was incomplete and non-random, lacked Pangram-scored matched human texts, and used detector rules not matched by false-positive rate.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Include the existing GitHub Issue plot in the paper after cleaning it up.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Make a good, evidence-supported paper argument from the Pangram result without claiming higher overall accuracy.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Use ChatGPT CLI to decide and review the wording.' \\
  --outcome completed \\
  --evidence 'Final Source-2048 result, ChatGPT answer e3c999663e9efbf5b57562639aeb230133884159bba4f205701c07f64a09d596, integrated wording, and Human-facing email were independently reviewed.' \\
  --completion-key "$completion_key" \\
  --answer-subject-file /tmp/source2048-final-subject.txt \\
  --answer-message-file /tmp/source2048-final-message.txt
```

Do not use `--no-email` or `reconcile-sent-remove`. Confirm afterward that `omo_pending.py list` retains the broader review item and no Source-2048 item.
"""
SOURCE2048_FINAL_MESSAGE = """The Pangram paragraph and cleaned plot are in Section 3.1, “Evaluation on Newer Generators.” The body remains eight pages.

Open the paper in Overleaf to review these two items:
https://www.overleaf.com/project/67b5138d6bdbe59818a13d48

1. PDF page 5, the paragraph beginning “Pangram”: is its argument clear?

“Pangram [56] marks more of the 605 observed Claude Sonnet-generated replacement texts as AI than Binoculars (Figure 5). Both detectors scored all 605 texts, a nonrandom subset of 780 Sonnet 4/4.6 replacements. Pangram labels 594 (98.2%) AI, including all 470 (77.7%) marked by Binoculars’ pre-existing max-F1 rule and 124 additional texts. Because we did not test Pangram on comparable human-written texts or set the detectors to the same false-positive rate, this result does not establish higher overall accuracy.”

2. PDF page 6, Figure 5 and its caption: are they readable at paper size?

“Pangram AI percentage (y-axis) versus Binoculars score (x-axis) for 605 observed Claude Sonnet 4/4.6 generated replacement texts. Each cross is one text. Binoculars calls texts left of the blue max-F1 cutoff AI; this gives the reported 470 calls. Orange shows its stricter alternative, calibrated for a 0.01% false-positive rate outside this sample. This generated-only sample does not measure that rate. The scatter shows the score relationship; Pangram’s count and overlap use its returned AI labels.”

Please reply with comments or edit the passages directly in Overleaf.
"""
SOURCE2048_ITEMS = (
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Draft the supplied 605-page Pangram-versus-Binoculars comparison into the paper, preserving that the observed cohort was incomplete and non-random, lacked Pangram-scored matched human texts, and used detector rules not matched by false-positive rate.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Include the existing GitHub Issue plot in the paper after cleaning it up.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Make a good, evidence-supported paper argument from the Pangram result without claiming higher overall accuracy.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Use ChatGPT CLI to decide and review the wording.",
)
SOURCE2048_EVIDENCE = (
    "Final Source-2048 result, ChatGPT answer e3c999663e9efbf5b57562639aeb230133884159bba4f205701c07f64a09d596, integrated wording, and Human-facing email were independently reviewed."
)
SOURCE2048_KEY = "803ba79e684265b9d33fc98159bc0c94366cd93206916a36c5ae5825771dda2e"


def task_text(status: str = "running", items: tuple[str, ...] = ()) -> str:
    pending = "pending_task_items: []" if not items else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in items)
    blocked_on = "blocked_on: persistent role\n" if status in {"long_running", "blocked"} else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocked_on}runat: cfg:2\ntool: codex\nmanagerat: cfg:1\nis_manager: false\n{pending}\n---\nwork\n"


def v2_task_text(item: str = "finish review") -> str:
    return f"""---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000001
status: running
runat: cfg:2
tool: codex
managerat: cfg:1
is_manager: false
pending_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000002
    text: {item}
    blocked_on: []
    notices: []
resolved_task_items: []
---
work
"""


def removal_recovery(path: Path, text: str, items: tuple[str, ...], evidence: str) -> RemovalNoticeRecovery:
    return RemovalNoticeRecovery(path.name, hashlib.sha256(text.encode()).hexdigest(), items, evidence, "b" * 64)


class PendingQueueTests(unittest.TestCase):
    def test_source2048_delivery_support_recovery_rejects_same_name_nested_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "nested" / "watch_err_email.md"
            path.parent.mkdir()
            path.write_text(task_text(items=(SOURCE2048_DELIVERY_SUPPORT_ITEM,)), encoding="utf-8")
            original = path.read_text(encoding="utf-8")
            args = Args(
                "recover-source2048-batch-delivery",
                (SOURCE2048_DELIVERY_SUPPORT_ITEM,),
                evidence=SOURCE2048_DELIVERY_EVIDENCE,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ) as sent,
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            sent.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_source2048_delivery_support_recovery_requires_sent_and_exact_paper_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            paper = root / "paper_finish.md"
            path.write_text(task_text(items=(SOURCE2048_DELIVERY_SUPPORT_ITEM,)), encoding="utf-8")
            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM,)), encoding="utf-8")
            args = Args(
                "recover-source2048-batch-delivery",
                (SOURCE2048_DELIVERY_SUPPORT_ITEM,),
                evidence=SOURCE2048_DELIVERY_EVIDENCE,
            )
            original = path.read_text(encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.ordinary_completion_participant_evidence", return_value=None),
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM, "unexpected item")), encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ),
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM,)), encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ) as sent,
            ):
                self.assertEqual(0, run(args, root))
            sent.assert_called_once_with(
                SOURCE2048_DELIVERY_MESSAGE_ID,
                SOURCE2048_SENT_SUBJECT_SHA256,
                SOURCE2048_SENT_BODY_SHA256,
            )
            self.assertNotIn(SOURCE2048_DELIVERY_SUPPORT_ITEM, path.read_text(encoding="utf-8"))
            self.assertEqual((SOURCE2048_BROADER_ITEM,), parse_task_metadata(paper.read_text(), root).pending_task_items)

    def source2048_delivery(self, root: Path, message: str = SOURCE2048_REVIEWED_DELIVERY_MESSAGE) -> Args:
        message_path = root / "delivery-message.txt"
        message_path.write_text(message, encoding="utf-8")
        transcript = root / "sessions" / SOURCE2048_PAPER_TRANSCRIPT
        transcript.parent.mkdir(parents=True, exist_ok=True)
        wrapper = f'<agent_message from="config:4">\n{message.rstrip()}\n</agent_message>'
        records = (
            {"type": "session_meta", "payload": {"id": SOURCE2048_PAPER_SESSION, "cwd": SOURCE2048_PAPER_CWD}},
            {
                "type": "response_item",
                "payload": {"role": "user", "content": [{"type": "input_text", "text": wrapper}]},
            },
        )
        transcript.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
        if message == SOURCE2048_REVIEWED_DELIVERY_MESSAGE:
            self.assertEqual(SOURCE2048_DELIVERY_MESSAGE_SHA256, hashlib.sha256(message.rstrip().encode()).hexdigest())
        return Args(
            "recover-source2048-batch-path",
            (SOURCE2048_BATCH_PATH_ITEM,),
            evidence=SOURCE2048_BATCH_PATH_EVIDENCE,
            delivery_transcript=transcript,
            delivery_message_file=message_path,
            delivery_message_sha256=hashlib.sha256(message.rstrip().encode()).hexdigest(),
        )

    def test_source2048_recovery_removes_only_exact_item_after_verified_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            other = "retain unrelated pending work"
            original = task_text(items=(SOURCE2048_BATCH_PATH_ITEM, other))
            path.write_text(original, encoding="utf-8")
            args = self.source2048_delivery(root)

            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                patch("omo_manager.omo_pending.require_owner_completion") as email,
            ):
                self.assertEqual(0, run(args, root))

            email.assert_not_called()
            updated = path.read_text(encoding="utf-8")
            self.assertNotIn(SOURCE2048_BATCH_PATH_ITEM, updated)
            self.assertIn(f"  - {other}\n", updated)
            self.assertIn(SOURCE2048_BATCH_PATH_EVIDENCE, updated)

    def test_source2048_recovery_rejects_unbound_or_missing_evidence_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            original = task_text(items=(SOURCE2048_BATCH_PATH_ITEM, "retain unrelated pending work"))
            path.write_text(original, encoding="utf-8")
            valid = self.source2048_delivery(root)
            cases = (
                replace(valid, delivery_message_sha256="0" * 64),
                replace(valid, evidence="changed evidence"),
                replace(valid, items=("different item",)),
            )
            for args in cases:
                with (
                    self.subTest(args=args),
                    patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                    patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                    self.assertRaises(BlockingError),
                ):
                    run(args, root)
                self.assertEqual(original, path.read_text(encoding="utf-8"))

            transcript = valid.delivery_transcript
            assert transcript is not None
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": SOURCE2048_PAPER_SESSION, "cwd": SOURCE2048_PAPER_CWD}}) + "\n",
                encoding="utf-8",
            )
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"), self.assertRaises(BlockingError):
                run(valid, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

            unrelated = self.source2048_delivery(root, "unrelated valid config:4 delivery")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                self.assertRaises(BlockingError),
            ):
                run(unrelated, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_source2048_recovery_parser_requires_digest(self) -> None:
        argv = [
            "recover-source2048-batch-path",
            "--delivery-transcript",
            "/tmp/transcript",
            "--delivery-message-file",
            "/tmp/message",
            "--delivery-message-sha256",
            "BAD",
        ]
        with self.assertRaises(SystemExit):
            parse_args(argv)

    def test_source2048_same_key_refresh_sends_once_then_removes_exact_four(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "paper_finish.md"
            broader = "Guide the remaining broader paper review."
            original = task_text(items=(broader, *SOURCE2048_ITEMS)).replace(
                "runat: cfg:2",
                "runat: DeGenTWeb_writeup:0\nsession_id: 01a0bb49-92ae-70e2-a0eb-0ea7373862e7",
            )
            path.write_text(original, encoding="utf-8")
            subject = root / "subject.txt"
            message = root / "message.txt"
            subject.write_text("Re: Try Pangram for hard data\n", encoding="utf-8")
            message.write_text(SOURCE2048_FINAL_MESSAGE, encoding="utf-8")

            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch("omo_manager.omo_completion_email.current_pending_task", return_value=path):
                old = plan_completion_email(
                    root,
                    path,
                    original,
                    "pending item removed after verification",
                    items=SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    human_subject="Re: Try Pangram for hard data",
                    human_body=SOURCE2048_FINAL_MESSAGE,
                    semantic_key=SOURCE2048_KEY,
                    pending_item_owner=True,
                )
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                with self.assertRaisesRegex(OSError, "requires changed task bytes"):
                    refresh_unattempted_completion_claim(replace(old, key="f" * 64), old.key)

                current = original.replace("status: running", "status: long_running\nblocked_on: exact same-key recovery")
                path.write_text(current, encoding="utf-8")
                plan = plan_completion_email(
                    root,
                    path,
                    current,
                    "pending item removed after verification",
                    items=SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    human_subject="Re: Try Pangram for hard data",
                    human_body=SOURCE2048_FINAL_MESSAGE,
                    semantic_key=SOURCE2048_KEY,
                    pending_item_owner=True,
                )
                assert plan is not None
                tampered = replace(plan, body="tampered reviewed answer\n")
                for operation in (
                    completion_email_is_delivered,
                    claim_completion_email,
                    send_completion_email,
                ):
                    with (
                        self.subTest(operation=operation.__name__),
                        self.assertRaisesRegex(OSError, "reviewed recovery bindings"),
                    ):
                        operation(tampered)
                with self.assertRaisesRegex(OSError, "changed its reviewed answer"):
                    refresh_unattempted_completion_claim(
                        replace(
                            plan,
                            key="d" * 64,
                            notice_key="e" * 64,
                            semantic_key="0" * 64,
                        ),
                        old.key,
                    )
                with patch("omo_manager.omo_completion_email.subprocess.run") as bypass_sender, self.assertRaisesRegex(OSError, "reviewed recovery bindings"):
                    send_completion_email(tampered)
                bypass_sender.assert_not_called()
                refresh_unattempted_completion_claim(plan, old.key)
                with patch("omo_manager.omo_completion_email.subprocess.run") as sender:
                    self.assertTrue(send_completion_email(plan))
                    self.assertFalse(send_completion_email(plan))
                command = sender.call_args.args[0]
                self.assertIn("--preserve-source2048-thread", command)
                self.assertIn("--require-human-recipient", command)

                args = Args(
                    "remove",
                    SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    answer_subject_file=subject,
                    answer_message_file=message,
                    completion_key=SOURCE2048_KEY,
                )
                with patch("omo_manager.omo_pending.current_pending_task", return_value=path):
                    self.assertEqual(0, run(args, root))

            updated = path.read_text(encoding="utf-8")
            self.assertIn(f"  - {broader}\n", updated)
            for item in SOURCE2048_ITEMS:
                self.assertNotIn(item, updated)

    def test_add_requires_and_encodes_explicit_provenance(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["add", "--item", "review request"])
        self.assertEqual(("🧑 review request",), parse_args(["add", "--human-authored", "--item", "review request"]).items)
        self.assertEqual(("review request",), parse_args(["add", "--agent-authored", "--item", "review request"]).items)
        self.assertEqual(("review request",), parse_args(["add", "--agent-authored", "--item", "🧑 🧑 review request"]).items)
        for old_flag in ("--human", "--agent"):
            with self.subTest(old_flag=old_flag), self.assertRaises(SystemExit):
                parse_args(["add", old_flag, "--item", "review request"])

    def test_add_help_defines_provenance_by_author(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            parse_args(["add", "--help"])

        help_text = " ".join(output.getvalue().split())
        self.assertIn("trusted caller assertion; helpers do not infer authorship", help_text)
        self.assertIn("who authored the pending item", help_text)
        self.assertIn("even when it asks for or awaits a Human decision", help_text)

    def test_replace_preserves_human_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 old wording",)), encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path):
                self.assertEqual(0, run(Args("replace", old_item="🧑 old wording", new_item="new wording"), root))
            self.assertIn("  - 🧑 new wording\n", path.read_text(encoding="utf-8"))

    def test_add_emails_before_mutation_and_retry_does_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            args = Args("add", ("🧑 inspect failure",))
            from omo_manager.omo_task_edit import replace_if_unchanged as actual_replace

            calls = 0

            def fail_once(target: Path, updated: str, before: os.stat_result) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    target.write_text(target.read_text(encoding="utf-8") + "unrelated note\n", encoding="utf-8")
                    raise OSError("task changed concurrently")
                actual_replace(target, updated, before)

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.replace_if_unchanged", side_effect=fail_once),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(OSError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(original + "unrelated note\n", path.read_text(encoding="utf-8"))
                self.assertEqual(0, run(args, root))

            email.assert_called_once()
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - 🧑 inspect failure\n", text)
            self.assertIn("unrelated note\n", text)
            self.assertRegex(text, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}\)")

    def test_add_mail_failure_keeps_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail unavailable")),
            ):
                with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                    run(Args("add", ("🧑 inspect failure",)), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_mixed_add_under_agent_scoped_no_contact_emails_only_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(
                task_text() + "Agent-authored pending items must not email the Human.\n",
                encoding="utf-8",
            )
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(Args("add", ("🧑 Human work", "agent work")), root))

            self.assertEqual(["pending item created:\n- Human work\n"], bodies)
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - 🧑 Human work\n", text)
            self.assertIn("  - agent work\n", text)

    def test_agent_add_mutates_without_mail_or_notice_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(), encoding="utf-8")
            args = parse_args(["add", "--agent-authored", "--item", "inspect failure"])
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(args, root))
            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - inspect failure\n", text)
            self.assertNotIn("pending item creation notice", text)

    def test_add_race_with_competing_same_item_finalizes_notice_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(task_text(), encoding="utf-8")
            args = Args("add", ("🧑 inspect failure",))
            from omo_manager.omo_task_edit import add_pending_items as actual_add
            from omo_manager.omo_task_edit import remove_pending_items as actual_remove
            from omo_manager.omo_task_edit import replace_if_unchanged as actual_replace

            calls = 0

            def competing_add(target: Path, updated: str, before: os.stat_result) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    race_before = target.stat()
                    raced, _count = actual_add(target.read_text(encoding="utf-8"), args.items)
                    actual_replace(target, raced, race_before)
                    raise OSError("task changed concurrently")
                actual_replace(target, updated, before)

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.replace_if_unchanged", side_effect=competing_add),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(OSError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))
                reconciled = path.read_text(encoding="utf-8")
                self.assertRegex(reconciled, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}\)")

                removed, _count = actual_remove(reconciled, args.items)
                path.write_text(removed, encoding="utf-8")
                self.assertEqual(0, run(args, root))

            self.assertEqual(2, email.call_count)

    def test_duplicate_add_does_not_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text(items=("🧑 inspect failure",))
            path.write_text(original, encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice") as notice:
                self.assertEqual(0, run(Args("add", ("🧑 inspect failure",)), root))
            notice.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_mixed_existing_and_new_add_emails_only_new_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 already tracked",)), encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice", return_value=True) as notice:
                self.assertEqual(0, run(Args("add", ("🧑 already tracked", "🧑 new work")), root))
            self.assertEqual(("🧑 new work",), notice.call_args.args[3])
            text = path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("  - 🧑 already tracked\n"))
            self.assertEqual(1, text.count("  - 🧑 new work\n"))

    def test_repeated_item_in_one_add_is_rejected_before_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice") as notice:
                with self.assertRaisesRegex(BlockingError, "repeated"):
                    run(Args("add", ("same work", "same work")), root)
            notice.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_v2_add_emails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            document = MagicMock()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.require_pending_add_notice", return_value=True) as notice,
                patch("omo_manager.omo_pending.add_items", return_value=("pi_new",)) as add,
            ):
                self.assertEqual(0, run(Args("add", ("🧑 inspect another failure",)), root))
            notice.assert_called_once()
            add.assert_called_once()
            self.assertEqual((document, ("🧑 inspect another failure",)), add.call_args.args)
            self.assertRegex(
                add.call_args.kwargs["body_comment"],
                r"^pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}$",
            )

    def test_v2_add_retry_after_unrelated_write_race_does_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            (root / ".omo-task-v2-enabled.yaml").write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            args = Args("add", ("🧑 inspect another failure",))
            from omo_manager.omo_blocking import write_document as actual_write

            calls = 0

            def fail_once(document: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    path.write_text(path.read_text(encoding="utf-8") + "unrelated note\n", encoding="utf-8")
                    raise BlockingError("task changed concurrently")
                actual_write(document)  # type: ignore[arg-type]

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_blocking.write_document", side_effect=fail_once),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))

            email.assert_called_once()
            text = path.read_text(encoding="utf-8")
            self.assertIn("text: 🧑 inspect another failure\n", text)
            self.assertIn("unrelated note\n", text)
            self.assertRegex(text, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}\)")

    def test_v2_add_race_with_competing_same_item_finalizes_notice_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            (root / ".omo-task-v2-enabled.yaml").write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            args = Args("add", ("🧑 inspect another failure",))
            from omo_manager.omo_blocking import document_with as actual_document_with
            from omo_manager.omo_blocking import load_task as actual_load
            from omo_manager.omo_blocking import resolve_item as actual_resolve
            from omo_manager.omo_blocking import write_document as actual_write

            original_body = actual_load(path, root=root).body
            calls = 0

            def competing_add(document: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    actual_write(actual_document_with(document, document.metadata, original_body))  # type: ignore[arg-type,union-attr]
                    raise BlockingError("task changed concurrently")
                actual_write(document)  # type: ignore[arg-type]

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_blocking.write_document", side_effect=competing_add),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))
                reconciled = path.read_text(encoding="utf-8")
                self.assertRegex(reconciled, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}\)")

                document = actual_load(path, root=root)
                item = next(item for item in document.metadata["pending_task_items"] if item["text"] == args.items[0])
                actual_resolve(document, item["id"], "completed", "race test completed")
                self.assertEqual(0, run(args, root))

            self.assertEqual(2, email.call_count)

    def test_failed_owner_email_keeps_item_and_retry_cannot_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text(items=("🧑 finish review",)) + "Report results directly to the Human.\n"
            path.write_text(original, encoding="utf-8")
            args = Args("remove", ("🧑 finish review",), evidence="review passed", completion_key="a" * 64)
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")) as email,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                        run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(2, email.call_count)

    def test_mixed_remove_under_agent_scoped_no_contact_emails_only_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(
                task_text(items=("🧑 Human work", "agent work")) + "Agent-authored pending items must not email the Human.\n",
                encoding="utf-8",
            )
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            ("🧑 Human work", "agent work"),
                            evidence="both completed",
                            completion_key="a" * 64,
                        ),
                        root,
                    ),
                )

            self.assertEqual(["pending item deleted:\n- Human work\n"], bodies)
            self.assertIn("pending_task_items: []", path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_is_evidence_bound_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            items = ("🧑 first request", "🧑 second request", "🧑 third request")
            original = (
                task_text()
                + "Agent-authored pending items must not email the Human.\n"
                + "Implement the correction without weakening explicit no-contact or delivery safeguards.\n"
                + f"(verified removed pending items: {evidence})\n"
            )
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, items, evidence)
            args = Args("recover-removal-notice", recovery_id="test-recovery")
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(args, root))
                self.assertEqual(0, run(args, root))

            self.assertEqual(["pending item deleted:\n- first request\n- second request\n- third request\n"], bodies)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_failure_keeps_completed_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            original = task_text() + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, ("🧑 request",), evidence)
            args = Args("recover-removal-notice", recovery_id="test-recovery")
            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail unavailable")),
            ):
                with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                    run(args, root)

            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_rejects_changed_or_blanket_no_contact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            original = task_text() + "Never email the Human.\n" + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, ("🧑 request",), evidence)
            base = Args("recover-removal-notice", recovery_id="test-recovery")
            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "removal evidence"):
                    REMOVAL_NOTICE_RECOVERIES["test-recovery"] = removal_recovery(path, original, ("🧑 request",), "different evidence")
                    run(base, root)
                REMOVAL_NOTICE_RECOVERIES["test-recovery"] = recovery
                with self.assertRaisesRegex(BlockingError, "blanket no-contact"):
                    run(base, root)
                with self.assertRaisesRegex(BlockingError, "digest changed"):
                    REMOVAL_NOTICE_RECOVERIES["test-recovery"] = RemovalNoticeRecovery(
                        recovery.task_name,
                        "e" * 64,
                        recovery.items,
                        recovery.evidence,
                        recovery.completion_key,
                    )
                    run(base, root)
            email.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_remove_with_missing_completion_entrypoint_does_not_mutate_or_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            entrypoint = root / "omo_completion_email.py"
            original = task_text(items=("🧑 finish review",))
            path.write_text(original, encoding="utf-8")
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            entrypoint.chmod(0o600)
            state = root / "state"
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.EMAIL_HELPER", root / "must-not-run-email-helper"),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")),
            ):
                with self.assertRaisesRegex(OSError, "not safely executable"):
                    run(Args("remove", ("🧑 finish review",), evidence="review passed", completion_key="a" * 64), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_remove_help_explains_single_email_answer_workflow(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            parse_args(["remove", "--help"])
        self.assertIn(
            "answer a human question and remove one or more exact Human-authored pending items with one email",
            " ".join(output.getvalue().split()),
        )

    def test_emailing_remove_requires_lowercase_sha256_completion_key(self) -> None:
        base = ["remove", "--item", "🧑 finish review", "--evidence", "review passed"]
        for value in ("not-a-digest", "A" * 64):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_args([*base, "--completion-key", value])
        with self.assertRaises(SystemExit):
            parse_args(base)
        self.assertEqual("a" * 64, parse_args([*base, "--completion-key", "a" * 64]).completion_key)
        self.assertEqual("", parse_args(["remove", "--item", "legacy item", "--evidence", "done"]).completion_key)

    def test_combined_answer_remove_accepts_unique_human_items_only(self) -> None:
        base = [
            "remove",
            "--item",
            "🧑 result one",
            "--item",
            "🧑 result two",
            "--evidence",
            "reviewed",
            "--completion-key",
            "a" * 64,
            "--answer-subject-file",
            "/tmp/subject.txt",
            "--answer-message-file",
            "/tmp/message.txt",
        ]
        self.assertEqual(("🧑 result one", "🧑 result two"), parse_args(base).items)
        with self.assertRaises(SystemExit):
            parse_args([*base, "--item", "agent work"])
        with self.assertRaises(SystemExit):
            parse_args([*base, "--item", "🧑 result one"])

    def test_reconcile_sent_remove_requires_exact_human_item_and_sent_evidence(self) -> None:
        base = [
            "reconcile-sent-remove",
            "--item",
            "🧑 finish review",
            "--evidence",
            "review passed",
            "--completion-key",
            "a" * 64,
            "--message-id",
            "<sent@example.test>",
            "--sent-subject-sha256",
            "b" * 64,
            "--sent-body-sha256",
            "c" * 64,
        ]
        args = parse_args(base)
        self.assertEqual("reconcile-sent-remove", args.command)
        self.assertEqual(("🧑 finish review",), args.items)
        for changed in (
            [value if value != "🧑 finish review" else "agent work" for value in base],
            [value if value != "<sent@example.test>" else "bad" for value in base],
            [value if value != "b" * 64 else "B" * 64 for value in base],
            [*base[:3], "--item", "🧑 finish review", *base[3:]],
        ):
            with self.subTest(changed=changed), self.assertRaises(SystemExit):
                parse_args(changed)

    def test_reconcile_sent_remove_verifies_before_removing_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            item = "🧑 finish review"
            path.write_text(task_text(items=(item,)) + "Do not send another Human email.\n", encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                (item,),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE) as verify,
                patch(
                    "omo_manager.omo_completion_email.configured_agent_mail",
                    return_value=MagicMock(agent_address="agent@example.test", human_address="human@example.test"),
                ) as mail_config,
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")),
            ):
                self.assertEqual(0, run(args, root))

            self.assertNotIn(item, path.read_text(encoding="utf-8").split("---", 2)[1])
            verify.assert_called_once_with(
                "<sent@example.test>",
                "b" * 64,
                "c" * 64,
                required_items=("🧑 finish review",),
                required_evidence="review passed",
                require_record=True,
            )
            mail_config.assert_not_called()

    def test_reconcile_sent_remove_validates_items_before_binding_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 finish review",)), encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                ("🧑 misspelled item",),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.reconcile_ordinary_sent_completion") as reconcile,
                self.assertRaisesRegex(TaskFrontmatterError, "not found"),
            ):
                run(args, root)
            reconcile.assert_not_called()

    def test_reconcile_sent_remove_rejects_duplicate_legacy_item_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            item = "🧑 finish review"
            path.write_text(task_text(items=(item, item)), encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                (item,),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.reconcile_ordinary_sent_completion") as reconcile,
                self.assertRaisesRegex(BlockingError, "exactly once"),
            ):
                run(args, root)
            reconcile.assert_not_called()

    def test_recover_removal_notice_requires_human_items_and_exact_digests(self) -> None:
        base = ["recover-removal-notice", "--recovery-id", SOURCE1929_RECOVERY_ID]
        args = parse_args(base)
        self.assertEqual("recover-removal-notice", args.command)
        self.assertEqual(SOURCE1929_RECOVERY_ID, args.recovery_id)
        for changed in (
            ["recover-removal-notice", "--recovery-id", "unknown"],
            [*base, "--item", "🧑 forged item"],
            [*base, "--completion-key", "a" * 64],
        ):
            with self.subTest(changed=changed), self.assertRaises(SystemExit):
                parse_args(changed)

    def test_legacy_remove_no_email_preserves_evidence_without_mail_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(task_text(items=("finish review",)) + "Report results directly to the Human.\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            ("finish review",),
                            evidence="review passed",
                            outcome="completed",
                            no_email=True,
                        ),
                        root,
                    ),
                )
            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertIn("pending_task_items: []", text)
            self.assertIn("verified removed pending item: review passed", text)

            from omo_manager.omo_completion_email import reconcile_ordinary_sent_completion
            from omo_manager.omo_completion_email import require_owner_completion as actual_require
            from omo_manager.omo_task_status import Args as StatusArgs
            from omo_manager.omo_task_status import StopArgs
            from omo_manager.omo_task_status import run as status_run

            message_id = "<legacy-completion@example.test>"
            digest = "b" * 64
            semantic_key = "a" * 64
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_completion_email.current_active_task", return_value=path),
                patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE),
                patch("omo_manager.omo_task_status.require_owner_completion", side_effect=actual_require),
                patch(
                    "omo_manager.omo_task_status.stop_done_agent",
                    return_value=(StopArgs("cfg:2", 10.0, 2000, False, False, root, "task.md", True, 0.0), "session-1"),
                ),
                patch("omo_manager.omo_task_status.record_close"),
                patch("omo_manager.omo_tmux_send.send_system_to_codex") as queue,
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                reconcile_ordinary_sent_completion(
                    root,
                    path,
                    "legacy pending items removed without email",
                    message_id,
                    digest,
                    digest,
                    semantic_key=semantic_key,
                )
                self.assertEqual(2, status_run(StatusArgs(root, Path("task.md"), "done", "", completion_key=semantic_key)))
                self.assertEqual(0, status_run(StatusArgs(root, Path("task.md"), "done", "", completion_key=semantic_key)))
            queue.assert_not_called()
            email.assert_called_once()
            self.assertIn("status: done\n", path.read_text(encoding="utf-8"))

    def test_agent_or_legacy_remove_mutates_without_mail_or_completion_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("agent work",)), encoding="utf-8")
            args = parse_args(["remove", "--item", "agent work", "--evidence", "verified done"])
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(args, root))
            plan.assert_not_called()
            require.assert_not_called()
            self.assertNotIn("agent work", path.read_text(encoding="utf-8").split("---", 2)[1])

    def test_legacy_remove_matches_displayed_text_from_quoted_colon_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            item = "fresh residual review: exact bookkeeping candidate"
            path.write_text(task_text(items=(f"'{item}'", "keep this")), encoding="utf-8")

            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(Args("remove", (item,), evidence="review passed", no_email=True), root))

            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(item, text.split("---", 2)[1])
            self.assertIn("  - keep this\n", text)

    def test_legacy_remove_no_email_failure_does_not_call_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text(items=("finish review",))
            path.write_text(original, encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "pending task item not found"):
                    run(Args("remove", ("different item",), evidence="not applicable", no_email=True), root)
            plan.assert_not_called()
            require.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_legacy_remove_no_email_rejects_email_options(self) -> None:
        for option in ("--answer-subject-file", "--answer-message-file"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parse_args(
                    [
                        "remove",
                        "--item",
                        "finish review",
                        "--evidence",
                        "review passed",
                        "--no-email",
                        option,
                        "answer.txt",
                    ]
                )

    def test_human_remove_no_email_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["remove", "--item", "🧑 answer request", "--evidence", "already sent", "--no-email"])

        with self.assertRaisesRegex(ValueError, "Human-authored"):
            run(Args("remove", ("🧑 answer request",), evidence="already sent", no_email=True), Path("/unused"))

    def test_legacy_remove_accepts_documented_outcome(self) -> None:
        args = parse_args(
            [
                "remove",
                "--item",
                "finish review",
                "--evidence",
                "review passed",
                "--outcome",
                "completed",
                "--completion-key",
                "a" * 64,
            ]
        )
        self.assertEqual("completed", args.outcome)
        self.assertFalse(args.no_email)

    def test_remove_with_answer_files_sends_once_and_removes_exact_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            remove = tuple(f"🧑 Source-2048 result {index}" for index in range(4))
            keep = "broader paper review"
            path.write_text(task_text(items=(*remove, keep)), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            sent: list[tuple[str, str]] = []

            def capture_email(command: list[str], check: bool) -> None:
                sent.append(
                    (
                        Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8"),
                        Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"),
                    )
                )

            args = Args(
                "remove",
                remove,
                evidence="reviewed final result",
                answer_subject_file=subject,
                answer_message_file=message,
                completion_key="c" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(args, root))
                with self.assertRaisesRegex(TaskFrontmatterError, "not found"):
                    run(args, root)

            self.assertEqual(1, len(sent))
            sent_subject, sent_body = sent[0]
            self.assertEqual("Re: Original question\n", sent_subject)
            self.assertIn("The concise answer.\n\nCompletion record:\n", sent_body)
            self.assertIn('"evidence":"reviewed final result"', sent_body)
            for item in remove:
                self.assertIn(item.removeprefix("🧑 "), sent_body)
            self.assertNotIn(keep, sent_body)
            remaining = path.read_text(encoding="utf-8")
            self.assertIn(f"  - {keep}\n", remaining)
            for item in remove:
                self.assertNotIn(item, remaining)

    def test_remove_with_answer_failure_preserves_all_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            items = ("🧑 result one", "🧑 result two", "keep this")
            original = task_text(items=items)
            path.write_text(original, encoding="utf-8")
            subject.write_text("Final result\n", encoding="utf-8")
            message.write_text("Reviewed answer.\n", encoding="utf-8")
            args = Args(
                "remove",
                items[:2],
                evidence="reviewed",
                answer_subject_file=subject,
                answer_message_file=message,
                completion_key="d" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail failed")),
                self.assertRaisesRegex(OSError, "not confirmed delivered"),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_remove_with_answer_refuses_before_mutation_when_reporting_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            original = task_text(items=("🧑 answer question",))
            path.write_text(original, encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.plan_completion_email", return_value=None):
                with self.assertRaisesRegex(BlockingError, "not allowed by this task's reporting policy"):
                    run(
                        Args(
                            "remove",
                            ("🧑 answer question",),
                            evidence="answered",
                            answer_subject_file=subject,
                            answer_message_file=message,
                            completion_key="c" * 64,
                        ),
                        root,
                    )
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_v2_remove_sends_exact_owner_completion_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text("🧑 finish review"), encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            email = object()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.resolve_item") as resolve,
                patch("omo_manager.omo_pending.blocking_request"),
                patch("omo_manager.omo_pending.plan_completion_email", return_value=email) as plan,
                patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            evidence="review passed",
                            item_id="pi_019f0000-0000-7000-8000-000000000002",
                            outcome="completed",
                            completion_key="d" * 64,
                        ),
                        root,
                    ),
                )
            resolve.assert_called_once_with(document, "pi_019f0000-0000-7000-8000-000000000002", "completed", "review passed")
            self.assertEqual(("🧑 finish review",), plan.call_args.kwargs["items"])
            self.assertEqual("review passed", plan.call_args.kwargs["evidence"])
            self.assertEqual("d" * 64, plan.call_args.kwargs["semantic_key"])
            self.assertEqual("d" * 64, require.call_args.kwargs["semantic_key"])
            require.assert_called_once()

    def test_v2_agent_remove_mutates_without_mail_or_completion_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.resolve_item") as resolve,
                patch("omo_manager.omo_pending.blocking_request"),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            evidence="review passed",
                            item_id="pi_019f0000-0000-7000-8000-000000000002",
                            outcome="completed",
                        ),
                        root,
                    ),
                )
            resolve.assert_called_once_with(document, "pi_019f0000-0000-7000-8000-000000000002", "completed", "review passed")
            plan.assert_not_called()
            require.assert_not_called()

    def test_v2_remove_with_answer_files_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            path.write_text(v2_task_text("🧑 finish review"), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires legacy --item"):
                run(
                    Args(
                        "remove",
                        evidence="answered",
                        item_id="pi_019f0000-0000-7000-8000-000000000002",
                        outcome="completed",
                        answer_subject_file=subject,
                        answer_message_file=message,
                        completion_key="e" * 64,
                    ),
                    root,
                )

    def test_inference_rejects_current_previous_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2.0")

    def test_pending_inference_prefers_runnable_owner_over_blocked_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text("blocked"), encoding="utf-8")

            self.assertEqual(root / "current.md", infer_pending_task(root, "cfg:2.0"))

    def test_pending_inference_rejects_two_runnable_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md cfg:2\nb.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text("running"), encoding="utf-8")
            (root / "b.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text("blocked"), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_pending_task(root, "cfg:2")

    def test_pending_inference_rejects_two_blocked_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md cfg:2\n\nprevious:\nb.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text("blocked"), encoding="utf-8")
            (root / "b.md").write_text(task_text("blocked"), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_pending_task(root, "cfg:2")

    def test_human_item_removal_uses_runnable_owner_among_blocked_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "current.md"
            blocked = root / "old.md"
            item = "🧑 answer the human"
            task.write_text(task_text("running", (item,)), encoding="utf-8")
            blocked.write_text(task_text("blocked"), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2.0"),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args("remove", (item,), evidence="answer delivered", completion_key="a" * 64),
                        root,
                    ),
                )

            self.assertNotIn(item, task.read_text(encoding="utf-8"))
            email.assert_called_once()

    def test_inference_accepts_one_long_running_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")

            self.assertEqual(root / "current.md", infer_active_task(root, "cfg:2.0"))

    def test_inference_rejects_ambiguous_noncurrent_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("human pending:\na.md cfg:2\nb.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text(), encoding="utf-8")
            (root / "b.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2")

    def test_agent_add_list_replace_remove_is_path_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "secret-task.md"
            (root / "TODO.md").write_text("current:\nsecret-task.md cfg:2\n", encoding="utf-8")
            path.write_text(task_text("long_running"), encoding="utf-8")
            output = StringIO()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_task_edit.frontmatter_parts", wraps=frontmatter_parts) as parse_parts,
                patch("omo_manager.omo_pending.plan_completion_email", return_value=None),
                patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require,
                redirect_stdout(output),
            ):
                self.assertEqual(0, run(Args("add", ("inspect failure",)), root))
                self.assertEqual(0, run(Args("list"), root))
                self.assertEqual(0, run(Args("replace", old_item="inspect failure", new_item="repair failure"), root))
                self.assertEqual(0, run(Args("remove", ("repair failure",), evidence="verified fixed"), root))
            require.assert_not_called()
            self.assertGreaterEqual(parse_parts.call_count, 3)
            self.assertNotIn("secret-task", output.getvalue())
            text = path.read_text(encoding="utf-8")
            self.assertIn("verified removed pending item: verified fixed", text)
            self.assertIn("pending_task_items: []", text)


if __name__ == "__main__":
    unittest.main()
