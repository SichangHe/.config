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
from omo_manager.omo_task_metadata import frontmatter_parts


def task_text(status: str = "running", items: tuple[str, ...] = ()) -> str:
    pending = "pending_task_items: []" if not items else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in items)
    blocked_on = "blocked_on: persistent role\n" if status == "long_running" else ""
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
    def test_failed_owner_email_keeps_item_and_retry_cannot_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text(items=("finish review",))
            path.write_text(original, encoding="utf-8")
            args = Args("remove", ("finish review",), evidence="review passed")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_pending.current_active_task", return_value=path
            ), patch("omo_manager.omo_completion_email.current_active_task", return_value=path), patch(
                "omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")
            ) as email:
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                        run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            email.assert_called_once()

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
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch("omo_manager.omo_pending.current_active_task", return_value=path), patch(
                "omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint
            ), patch("omo_manager.omo_completion_email.current_active_task", return_value=path), patch(
                "omo_manager.omo_completion_email.EMAIL_HELPER", root / "must-not-run-email-helper"
            ), patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")):
                with self.assertRaisesRegex(OSError, "not safely executable"):
                    run(Args("remove", ("finish review",), evidence="review passed"), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_remove_help_explains_single_email_answer_workflow(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            parse_args(["remove", "--help"])
        self.assertIn("answer a human question and remove its pending item with one email", " ".join(output.getvalue().split()))

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
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), patch(
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
                        ),
                        root,
                    ),
                )
            self.assertEqual("Re: Original question", plan.call_args.kwargs["human_subject"])
            self.assertEqual("The concise answer.\n", plan.call_args.kwargs["human_body"])
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
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), patch(
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
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), patch("omo_manager.omo_pending.v2_enabled", return_value=True), patch(
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
                        ),
                        root,
                    ),
                )
            resolve.assert_called_once_with(document, "pi_019f0000-0000-7000-8000-000000000002", "completed", "review passed")
            self.assertEqual(("finish review",), plan.call_args.kwargs["items"])
            self.assertEqual("review passed", plan.call_args.kwargs["evidence"])
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
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), patch("omo_manager.omo_pending.v2_enabled", return_value=True), patch(
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
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), patch(
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
