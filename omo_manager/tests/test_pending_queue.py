from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_pending import Args
from omo_manager.omo_pending import parse_args
from omo_manager.omo_pending import run
from omo_manager.omo_task_context import infer_active_task
from omo_manager.omo_task_context import infer_pending_task
from omo_manager.omo_task_metadata import frontmatter_parts


def task_text(status: str = "running", items: tuple[str, ...] = ()) -> str:
    pending = "pending_task_items: []" if not items else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in items)
    blocked_on = "blocked_on: persistent role\n" if status in {"long_running", "blocked"} else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked_on}"
        "runat: cfg:2\n"
        "tool: codex\n"
        "managerat: cfg:1\n"
        "is_manager: false\n"
        f"{pending}\n"
        "---\n"
        "work\n"
    )


def v2_task_text() -> str:
    return """---
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


class PendingQueueTests(unittest.TestCase):
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

    def test_failed_owner_email_keeps_item_and_retry_cannot_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text(items=("finish review",))
            path.write_text(original, encoding="utf-8")
            args = Args("remove", ("finish review",), evidence="review passed", completion_key="a" * 64)
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_pending.current_pending_task", return_value=path
            ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=path), patch(
                "omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")
            ) as email:
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                        run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(2, email.call_count)

    def test_remove_with_missing_completion_entrypoint_does_not_mutate_or_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            entrypoint = root / "omo_completion_email.py"
            original = task_text(items=("finish review",))
            path.write_text(original, encoding="utf-8")
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            entrypoint.chmod(0o600)
            state = root / "state"
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint
            ), patch("omo_manager.omo_completion_email.current_pending_task", return_value=path), patch(
                "omo_manager.omo_completion_email.EMAIL_HELPER", root / "must-not-run-email-helper"
            ), patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")):
                with self.assertRaisesRegex(OSError, "not safely executable"):
                    run(Args("remove", ("finish review",), evidence="review passed", completion_key="a" * 64), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_remove_help_explains_single_email_answer_workflow(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            parse_args(["remove", "--help"])
        self.assertIn("answer a human question and remove its pending item with one email", " ".join(output.getvalue().split()))

    def test_emailing_remove_requires_lowercase_sha256_completion_key(self) -> None:
        base = ["remove", "--item", "finish review", "--evidence", "review passed"]
        for value in ("", "not-a-digest", "A" * 64):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_args([*base, "--completion-key", value])
        self.assertEqual("a" * 64, parse_args([*base, "--completion-key", "a" * 64]).completion_key)

    def test_legacy_remove_no_email_preserves_evidence_without_mail_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(task_text(items=("finish review",)), encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_pending.plan_completion_email"
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion") as require:
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
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=path
            ), patch(
                "omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=True
            ), patch(
                "omo_manager.omo_task_status.require_owner_completion", side_effect=actual_require
            ), patch(
                "omo_manager.omo_task_status.stop_done_agent",
                return_value=(StopArgs("cfg:2", 10.0, 2000, False, False, root, "task.md", True, 0.0), "session-1"),
            ), patch("omo_manager.omo_task_status.record_close"), patch(
                "omo_manager.omo_tmux_send.send_system_to_codex"
            ) as queue, patch(
                "omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not send email")
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
                self.assertEqual(0, status_run(StatusArgs(root, Path("task.md"), "done", "", completion_key=semantic_key)))
            queue.assert_not_called()
            self.assertIn("status: done\n", path.read_text(encoding="utf-8"))

    def test_legacy_remove_matches_displayed_text_from_quoted_colon_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            item = "fresh residual review: exact bookkeeping candidate"
            path.write_text(task_text(items=(f"'{item}'", "keep this")), encoding="utf-8")

            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_pending.plan_completion_email"
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion") as require:
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
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_pending.plan_completion_email"
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion") as require:
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

    def test_remove_with_answer_files_sends_only_combined_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            path.write_text(task_text(items=("answer question",)), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            email = object()
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_pending.plan_completion_email", return_value=email
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require:
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            ("answer question",),
                            evidence="answered",
                            answer_subject_file=subject,
                            answer_message_file=message,
                            completion_key="c" * 64,
                        ),
                        root,
                    ),
                )
            self.assertEqual("Re: Original question", plan.call_args.kwargs["human_subject"])
            self.assertEqual("The concise answer.\n", plan.call_args.kwargs["human_body"])
            self.assertEqual("c" * 64, plan.call_args.kwargs["semantic_key"])
            self.assertEqual("c" * 64, require.call_args.kwargs["semantic_key"])
            require.assert_called_once()
            self.assertNotIn("answer question", path.read_text(encoding="utf-8").split("---", 2)[1])

    def test_remove_with_answer_refuses_before_mutation_when_reporting_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            original = task_text(items=("answer question",))
            path.write_text(original, encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_pending.plan_completion_email", return_value=None
            ):
                with self.assertRaisesRegex(BlockingError, "reporting policy"):
                    run(
                        Args(
                            "remove",
                            ("answer question",),
                            evidence="answered",
                            answer_subject_file=subject,
                            answer_message_file=message,
                        ),
                        root,
                    )
            self.assertEqual(original, path.read_text(encoding="utf-8"))
    def test_v2_remove_sends_exact_owner_completion_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            email = object()
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.v2_enabled", return_value=True), patch(
                "omo_manager.omo_pending.load_task", return_value=document
            ), patch("omo_manager.omo_pending.resolve_item") as resolve, patch("omo_manager.omo_pending.blocking_request"), patch(
                "omo_manager.omo_pending.plan_completion_email", return_value=email
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require:
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
            self.assertEqual(("finish review",), plan.call_args.kwargs["items"])
            self.assertEqual("review passed", plan.call_args.kwargs["evidence"])
            self.assertEqual("d" * 64, plan.call_args.kwargs["semantic_key"])
            self.assertEqual("d" * 64, require.call_args.kwargs["semantic_key"])
            require.assert_called_once()

    def test_v2_remove_with_answer_files_sends_only_combined_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            path.write_text(v2_task_text(), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            email = object()
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.v2_enabled", return_value=True), patch(
                "omo_manager.omo_pending.load_task", return_value=document
            ), patch("omo_manager.omo_pending.resolve_item"), patch("omo_manager.omo_pending.blocking_request"), patch(
                "omo_manager.omo_pending.plan_completion_email", return_value=email
            ) as plan, patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require:
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            evidence="answered",
                            item_id="pi_019f0000-0000-7000-8000-000000000002",
                            outcome="completed",
                            answer_subject_file=subject,
                            answer_message_file=message,
                        ),
                        root,
                    ),
                )
            self.assertEqual("Re: Original question", plan.call_args.kwargs["human_subject"])
            self.assertEqual("The concise answer.\n", plan.call_args.kwargs["human_body"])
            require.assert_called_once()

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

            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2.0"
            ), patch("omo_manager.omo_completion_email.subprocess.run") as email:
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
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch(
                "omo_manager.omo_task_edit.frontmatter_parts", wraps=frontmatter_parts
            ) as parse_parts, patch("omo_manager.omo_pending.plan_completion_email", return_value=None), patch(
                "omo_manager.omo_pending.require_owner_completion", return_value=True
            ) as require, redirect_stdout(output):
                self.assertEqual(0, run(Args("add", ("inspect failure",)), root))
                self.assertEqual(0, run(Args("list"), root))
                self.assertEqual(0, run(Args("replace", old_item="inspect failure", new_item="repair failure"), root))
                self.assertEqual(0, run(Args("remove", ("repair failure",), evidence="verified fixed"), root))
            require.assert_called_once()
            self.assertGreaterEqual(parse_parts.call_count, 3)
            self.assertNotIn("secret-task", output.getvalue())
            text = path.read_text(encoding="utf-8")
            self.assertIn("verified removed pending item: verified fixed", text)
            self.assertIn("pending_task_items: []", text)


if __name__ == "__main__":
    unittest.main()
