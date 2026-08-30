from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task_edit import REMOVE_REMINDER
from omo_manager.omo_task_edit import Args
from omo_manager.omo_task_edit import normalize_duplicate_frontmatter
from omo_manager.omo_task_edit import parse_args
from omo_manager.omo_task_edit import run


def task_frontmatter(*, status: str = "running", pending_items: tuple[str, ...] = (), is_manager: bool = False) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
        *(["blocked_on: persistent role"] if status == "long_running" else []),
        "runat: wl:2",
        "tool: codex",
        "managerat: wl:1",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


class TaskEditTests(unittest.TestCase):
    def test_normalizes_one_empty_later_frontmatter_without_changing_record_or_body(self) -> None:
        authoritative = task_frontmatter(pending_items=("keep broad owner item",))
        duplicate = task_frontmatter()
        original = authoritative + "first human prompt\n" + duplicate + "second human prompt\n(useful history)\n"

        updated = normalize_duplicate_frontmatter(original, 12)

        self.assertEqual(
            authoritative + "first human prompt\n---\nsecond human prompt\n(useful history)\n",
            updated,
        )

    def test_frontmatter_normalize_rejects_unsafe_target_blocks(self) -> None:
        authoritative = task_frontmatter(pending_items=("keep broad owner item",))
        cases = (
            (authoritative + "body\n" + task_frontmatter(pending_items=("do not lose",)), 12, "has pending items"),
            (authoritative + "body\n" + task_frontmatter().replace("runat: wl:2", "runat: other:2"), 12, "does not match"),
            (authoritative + "body\n---\nnot frontmatter\n", 12, "no closing marker"),
        )
        for original, line_number, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_duplicate_frontmatter(original, line_number)

    def test_frontmatter_normalize_cli_uses_explicit_later_line(self) -> None:
        args = parse_args(["frontmatter-normalize", "task.md", "--line", "12"])

        self.assertEqual("frontmatter-normalize", args.command)
        self.assertEqual(12, args.line)

    def test_summary_prints_frontmatter_overview_without_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(pending_items=("finish review",)) + "body should stay private\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(Args(root, Path("task.md"), "summary"))

            self.assertEqual(0, exit_code)
            self.assertEqual(
                "task_file: task.md\nstatus: running\nrunat: wl:2\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n  - finish review\n",
                stdout.getvalue(),
            )
            self.assertNotIn("body should stay private", stdout.getvalue())

    def test_summary_combines_files_sorted_by_manager_then_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zeta.md").write_text(task_frontmatter().replace("managerat: wl:1", "managerat: wl:3"), encoding="utf-8")
            (root / "beta.md").write_text(task_frontmatter().replace("managerat: wl:1", "managerat: wl:1"), encoding="utf-8")
            (root / "alpha.md").write_text(task_frontmatter().replace("managerat: wl:1", "managerat: wl:1"), encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(parse_args(["--root", str(root), "summary", "zeta.md", "beta.md", "alpha.md"]))

            self.assertEqual(0, exit_code)
            self.assertEqual(
                "task_file: alpha.md\nstatus: running\nrunat: wl:2\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n"
                "task_file: beta.md\nstatus: running\nrunat: wl:2\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n"
                "task_file: zeta.md\nstatus: running\nrunat: wl:2\nmanagerat: wl:3\nis_manager: false\npending_task_items: []\n",
                stdout.getvalue(),
            )

    def test_summary_accepts_historical_done_retired_task_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "terminal.md"
            task.write_text(
                task_frontmatter(status="done").replace("runat: wl:2", "runat: retired") + "historical body\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(Args(root, Path("terminal.md"), "summary"))

            self.assertEqual(0, exit_code)
            self.assertEqual(
                "task_file: terminal.md\nstatus: done\nrunat: retired\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n",
                stdout.getvalue(),
            )

    def test_non_summary_command_rejects_historical_done_retired_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "terminal.md"
            task.write_text(
                task_frontmatter(status="done").replace("runat: wl:2", "runat: retired"),
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("terminal.md"), "pending-list"))

            self.assertEqual(2, exit_code)
            self.assertIn("`runat: retired` is only valid when `status` is `blocked`", stderr.getvalue())

    def test_summary_rejects_done_retired_task_with_pending_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "not-terminal.md"
            task.write_text(
                task_frontmatter(status="done", pending_items=("still open",)).replace("runat: wl:2", "runat: retired"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = run(Args(root, Path("not-terminal.md"), "summary"))

            self.assertEqual(2, exit_code)
            self.assertEqual("", stdout.getvalue())
            self.assertIn("historical done/retired summary requires an empty pending queue", stderr.getvalue())

    def test_summary_rejects_active_retired_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "active.md"
            task.write_text(task_frontmatter().replace("runat: wl:2", "runat: retired"), encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("active.md"), "summary"))

            self.assertEqual(2, exit_code)
            self.assertIn("`runat: retired` is only valid when `status` is `blocked`", stderr.getvalue())

    def test_summary_rejects_invalid_file_without_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "valid.md").write_text(task_frontmatter(), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = run(parse_args(["--root", str(root), "summary", "valid.md", "missing.md"]))

            self.assertEqual(2, exit_code)
            self.assertEqual("", stdout.getvalue())
            self.assertIn("missing.md", stderr.getvalue())

    def test_lists_pending_items_one_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(pending_items=("finish review", "email human")) + "body\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(Args(root, Path("task.md"), "pending-list"))

            self.assertEqual(0, exit_code)
            self.assertEqual("finish review\nemail human\n", stdout.getvalue())

    def test_adds_pending_items_and_preserves_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), "pending-add", items=("finish review", "email human")))

            self.assertEqual(0, exit_code)
            self.assertEqual(task_frontmatter(pending_items=("finish review", "email human")) + "body\n", task.read_text(encoding="utf-8"))

    def test_adds_pending_items_to_long_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(status="long_running") + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), "pending-add", items=("review queue",)))

            self.assertEqual(0, exit_code)
            self.assertEqual(
                task_frontmatter(status="long_running", pending_items=("review queue",)) + "body\n",
                task.read_text(encoding="utf-8"),
            )

    def test_add_rejects_done_task_and_empty_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter(status="done") + "body\n"
            task.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                done_exit = run(Args(root, Path("task.md"), "pending-add", items=("new item",)))
                empty_exit = run(Args(root, Path("task.md"), "pending-add", items=(" ",)))

            self.assertEqual(2, done_exit)
            self.assertEqual(2, empty_exit)
            self.assertEqual(original, task.read_text(encoding="utf-8"))
            self.assertIn("do not add pending task items to done tasks", stderr.getvalue())
            self.assertIn("pending task item must not be empty", stderr.getvalue())

    def test_replaces_exact_pending_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(pending_items=("old wording", "keep this")) + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), "pending-replace", old_item="old wording", new_item="new wording"))

            self.assertEqual(0, exit_code)
            self.assertEqual(task_frontmatter(pending_items=("new wording", "keep this")) + "body\n", task.read_text(encoding="utf-8"))

    def test_replace_rejects_done_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter(status="done", pending_items=("old wording",)) + "body\n"
            task.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "pending-replace", old_item="old wording", new_item="new wording"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, task.read_text(encoding="utf-8"))
            self.assertIn("do not replace pending task items on done tasks", stderr.getvalue())

    def test_removes_pending_item_and_prints_verification_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(pending_items=("finish review",)) + "body\n", encoding="utf-8")
            stdout = io.StringIO()

            email = object()
            with patch("omo_manager.omo_task_edit.require_owner_completion", return_value=True), patch(
                "omo_manager.omo_task_edit.plan_completion_email", return_value=email
            ) as plan, patch(
                "omo_manager.omo_task_edit.send_completion_email", return_value=True
            ) as send, redirect_stdout(stdout):
                exit_code = run(Args(root, Path("task.md"), "pending-remove", items=("finish review",), evidence="review passed"))

            self.assertEqual(0, exit_code)
            self.assertEqual(task_frontmatter() + "body\n(verified removed pending item: review passed)\n", task.read_text(encoding="utf-8"))
            self.assertIn(REMOVE_REMINDER, stdout.getvalue())
            self.assertIn("evaluator agents", stdout.getvalue())
            self.assertEqual(("finish review",), plan.call_args.kwargs["items"])
            send.assert_called_once_with(email)

    def test_manager_pending_remove_owner_callback_then_retry_mutates_once(self) -> None:
        from omo_manager.omo_completion_email import main as completion_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            manager = root / "manager.md"
            original = task_frontmatter(pending_items=("finish review",)) + "body\n"
            task.write_text(original, encoding="utf-8")
            args = Args(root, Path("task.md"), "pending-remove", items=("finish review",), evidence="review passed")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_completion_email.current_active_task", return_value=manager
            ), patch("omo_manager.omo_tmux_send.send_system_to_codex") as queue, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))
                self.assertEqual(original, task.read_text(encoding="utf-8"))
                with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.subprocess.run"
                ) as email:
                    self.assertEqual(
                        0,
                        completion_main(
                            [
                                "--root",
                                str(root),
                                "--task",
                                str(task),
                                "--outcome",
                                "pending item removed after verification",
                                "--item",
                                "finish review",
                                "--evidence",
                                "review passed",
                            ]
                        ),
                    )
                    self.assertEqual(0, run(args))
                email.assert_called_once()
            queue.assert_called_once()
            self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))

    def test_pending_remove_requires_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter(pending_items=("finish review",)) + "body\n"
            task.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "pending-remove", items=("finish review",)))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, task.read_text(encoding="utf-8"))
            self.assertIn("comment must not be empty", stderr.getvalue())

    def test_appends_parenthesized_comment_wraps_existing_parentheses_and_rejects_empty_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "body", encoding="utf-8")
            stderr = io.StringIO()

            exit_code = run(Args(root, Path("task.md"), "comment-add", comment="(checked with evaluator)"))
            with redirect_stderr(stderr):
                empty_exit = run(Args(root, Path("task.md"), "comment-add", comment="()"))

            self.assertEqual(0, exit_code)
            self.assertEqual(2, empty_exit)
            self.assertEqual(task_frontmatter() + "body\n(manager note: (checked with evaluator))\n", task.read_text(encoding="utf-8"))
            self.assertIn("comment must not be empty", stderr.getvalue())

    def test_comment_rejects_text_that_would_create_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "body\n"
            task.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "comment-add", comment="pending"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, task.read_text(encoding="utf-8"))
            self.assertIn("must not create a live `(pending)` marker", stderr.getvalue())

    def test_pending_move_removes_from_source_and_adds_to_destination_without_remove_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manager.md"
            target = root / "worker.md"
            source.write_text(task_frontmatter(pending_items=("delegate audit", "keep source")) + "source body\n", encoding="utf-8")
            target.write_text(task_frontmatter(pending_items=("keep target",)) + "target body\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(Args(root, None, "pending-move", items=("delegate audit",), source_file=Path("manager.md"), target_file=Path("worker.md")))

            self.assertEqual(0, exit_code)
            self.assertEqual(task_frontmatter(pending_items=("keep source",)) + "source body\n", source.read_text(encoding="utf-8"))
            self.assertEqual(task_frontmatter(pending_items=("keep target", "delegate audit")) + "target body\n", target.read_text(encoding="utf-8"))
            self.assertNotIn(REMOVE_REMINDER, stdout.getvalue())

    def test_pending_marker_clear_requires_comment_evidence_and_can_ack_human_email_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "body\n(pending)\nrequest that needs no new item\n"
            task.write_text(text, encoding="utf-8")
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            email = mail_dir / "7.txt"
            email.write_text("Subject: Re: Existing thread\n\nbody\n", encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            calls: list[tuple[str, str]] = []

            def fake_run(command: list[str], check: bool) -> None:
                self.assertTrue(check)
                subject = Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8")
                body = Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8")
                calls.append((subject, body))

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=fake_run):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "pending-marker-clear",
                        comment="handled in existing item",
                        line=pending_line,
                        ack_human=True,
                        email_file=Path("manager_mail/7.txt"),
                        clear_kind="report-only",
                    )
                )

            self.assertEqual(0, exit_code)
            expected_text = task_frontmatter() + "body\nrequest that needs no new item\n(pending marker cleared line=11: report-only: handled in existing item)\n(human ack sent for pending marker clear line=11: report-only: handled in existing item)\n"
            self.assertEqual(expected_text, task.read_text(encoding="utf-8"))
            self.assertEqual([("Re: Existing thread\n", "No pending item was added.\nClassification: report-only\nReason: handled in existing item\n")], calls)

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=fake_run):
                retry_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "pending-marker-clear",
                        comment="handled in existing item",
                        line=pending_line,
                        ack_human=True,
                        email_file=Path("manager_mail/7.txt"),
                        clear_kind="report-only",
                    )
                )

            self.assertEqual(0, retry_code)
            self.assertEqual(expected_text, task.read_text(encoding="utf-8"))
            self.assertEqual([("Re: Existing thread\n", "No pending item was added.\nClassification: report-only\nReason: handled in existing item\n")], calls)

    def test_pending_marker_clear_human_ack_retry_succeeds_after_email_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "(pending)\nFYI only\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            args = Args(root, Path("task.md"), "pending-marker-clear", comment="FYI only", line=pending_line, ack_human=True, clear_kind="report-only")

            def fail_email(command: list[str], check: bool) -> None:
                raise subprocess.CalledProcessError(1, command)

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))

            self.assertEqual(task_frontmatter() + "FYI only\n(pending marker cleared line=10: report-only: FYI only)\n", task.read_text(encoding="utf-8"))
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=send_email):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertEqual(task_frontmatter() + "FYI only\n(pending marker cleared line=10: report-only: FYI only)\n(human ack sent for pending marker clear line=10: report-only: FYI only)\n", task.read_text(encoding="utf-8"))

    def test_pending_marker_clear_human_ack_requires_clear_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "(pending)\nFYI only\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "pending-marker-clear", comment="FYI only", line=pending_line, ack_human=True))

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("requires `--clear-kind`", stderr.getvalue())

    def test_pending_marker_clear_human_origin_requires_clear_kind_without_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "(pending)\nFYI only\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "pending-marker-clear", comment="FYI only", line=pending_line))

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("requires `--clear-kind`", stderr.getvalue())

    def test_pending_marker_clear_replay_does_not_remove_new_pending_at_same_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "body\n(pending)\nnew request\n(pending marker cleared line=11: report-only: old request)\n(human ack sent for pending marker clear line=11: report-only: old request)\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            calls: list[list[str]] = []

            def fake_run(command: list[str], check: bool) -> None:
                calls.append(command)

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=fake_run):
                exit_code = run(Args(root, Path("task.md"), "pending-marker-clear", comment="old request", line=pending_line, ack_human=True, clear_kind="report-only"))

            self.assertEqual(0, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual([], calls)

    def test_pending_marker_clear_email_failure_retry_rejects_new_pending_at_same_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "body\n(pending)\nnew request\n(pending marker cleared line=11: report-only: old request)\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            calls: list[list[str]] = []
            stderr = io.StringIO()

            def fake_run(command: list[str], check: bool) -> None:
                calls.append(command)

            with patch("omo_manager.omo_task_edit.subprocess.run", side_effect=fake_run), redirect_stderr(stderr):
                exit_code = run(Args(root, Path("task.md"), "pending-marker-clear", comment="old request", line=pending_line, ack_human=True, clear_kind="report-only"))

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual([], calls)
            self.assertIn("new live `(pending)` marker", stderr.getvalue())

    def test_pending_marker_clear_agent_origin_allows_no_clear_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "(pending)\n(from agent hcfg:1)\nFYI only\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1

            exit_code = run(Args(root, Path("task.md"), "pending-marker-clear", comment="FYI only", line=pending_line))

            self.assertEqual(0, exit_code)
            self.assertEqual(task_frontmatter() + "(from agent hcfg:1)\nFYI only\n(pending marker cleared line=10: FYI only)\n", task.read_text(encoding="utf-8"))

    def test_pending_marker_clear_existing_owner_item_verifies_active_task_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            owner = root / "owner.md"
            text = task_frontmatter() + "(pending)\nalready tracked\n"
            source.write_text(text, encoding="utf-8")
            owner.write_text(task_frontmatter(pending_items=("already tracked",)) + "body\n", encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1

            with patch("omo_manager.omo_task_edit.subprocess.run", return_value=None) as send:
                exit_code = run(
                    Args(
                        root,
                        Path("source.md"),
                        "pending-marker-clear",
                        comment="already tracked on owner task",
                        line=pending_line,
                        ack_human=True,
                        clear_kind="existing-owner-item",
                        owner_task_file=Path("owner.md"),
                        owner_item="already tracked",
                    )
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(
                task_frontmatter() + "already tracked\n(pending marker cleared line=10: existing-owner-item: already tracked on owner task)\n",
                source.read_text(encoding="utf-8"),
            )
            send.assert_not_called()

    def test_pending_marker_clear_duplicate_does_not_email_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter() + "(pending)\nalready handled\n"
            task.write_text(text, encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1

            with patch("omo_manager.omo_task_edit.subprocess.run", return_value=None) as send:
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "pending-marker-clear",
                        comment="already handled elsewhere",
                        line=pending_line,
                        ack_human=True,
                        clear_kind="duplicate",
                    )
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(
                task_frontmatter() + "already handled\n(pending marker cleared line=10: duplicate: already handled elsewhere)\n",
                task.read_text(encoding="utf-8"),
            )
            send.assert_not_called()

    def test_source_pointer_dedupe_removes_only_bare_completed_source_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            task.write_text(
                task_frontmatter()
                + f"{pointer}\n\n{pointer}\n"
                + "(verified removed pending item: original reply sent)\n"
                + f"\n{pointer}\n",
                encoding="utf-8",
            )

            exit_code = run(
                Args(
                    root,
                    Path("task.md"),
                    "source-pointer-dedupe",
                    source_ref=ref,
                    evidence="original reply and pause routing are complete",
                )
            )

            self.assertEqual(0, exit_code)
            updated = task.read_text(encoding="utf-8")
            self.assertNotIn(pointer, updated)
            self.assertIn("(verified removed pending item: original reply sent)", updated)
            self.assertIn("deduped 3 bare source pointer(s) for manager_mail/691.txt", updated)

    def test_source_pointer_dedupe_refuses_live_pending_source_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            text = task_frontmatter() + f"(pending)\n(record and delegate {ref})\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("inside a live `(pending)` block", stderr.getvalue())

    def test_source_pointer_dedupe_refuses_a_single_bare_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            text = task_frontmatter() + f"(record and delegate {ref})\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("single bare source pointer", stderr.getvalue())

    def test_source_pointer_dedupe_ignores_quoted_and_fenced_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            text = task_frontmatter() + f"{pointer}\n> {pointer}\n```text\n{pointer}\n```\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("single bare source pointer", stderr.getvalue())

    def test_source_pointer_dedupe_refuses_pointer_later_in_live_pending_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            text = task_frontmatter() + f"(pending)\nrequest body\n\n{pointer}\n{pointer}\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("inside a live `(pending)` block", stderr.getvalue())

    def test_source_pointer_dedupe_preserves_live_source_with_matching_active_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            item = f"Handle active request ({ref})"
            text = task_frontmatter(pending_items=(item,)) + f"{pointer}\n{pointer}\n(pending)\nrequest body\n{pointer}\n"
            task.write_text(text, encoding="utf-8")

            exit_code = run(
                Args(
                    root,
                    Path("task.md"),
                    "source-pointer-dedupe",
                    source_ref=ref,
                    evidence="active item preserved",
                    preserve_live_source=True,
                )
            )

            self.assertEqual(0, exit_code)
            updated = task.read_text(encoding="utf-8")
            self.assertEqual(1, updated.count(pointer))
            self.assertIn("(pending)\nrequest body\n" + pointer, updated)
            self.assertIn(item, updated)

    def test_source_pointer_dedupe_preserves_live_source_with_one_prior_bare_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            item = f"Handle active request ({ref})"
            text = task_frontmatter(pending_items=(item,)) + f"{pointer}\n(pending)\n{pointer}\n"
            task.write_text(text, encoding="utf-8")

            exit_code = run(
                Args(
                    root,
                    Path("task.md"),
                    "source-pointer-dedupe",
                    source_ref=ref,
                    evidence="active item preserved",
                    preserve_live_source=True,
                )
            )

            self.assertEqual(0, exit_code)
            updated = task.read_text(encoding="utf-8")
            self.assertEqual(1, updated.count(pointer))
            self.assertIn("(pending)\n" + pointer, updated)

    def test_source_pointer_dedupe_preserve_live_source_requires_matching_active_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            text = task_frontmatter(pending_items=("different active item",)) + f"{pointer}\n{pointer}\n(pending)\n{pointer}\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                        preserve_live_source=True,
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("requires an active pending item", stderr.getvalue())

    def test_source_pointer_dedupe_preserve_live_source_requires_live_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            item = f"Handle active request ({ref})"
            text = task_frontmatter(pending_items=(item,)) + f"{pointer}\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                        preserve_live_source=True,
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("requires an exact source pointer inside a live", stderr.getvalue())

    def test_source_pointer_dedupe_refuses_whitespace_wrapped_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            ref = "manager_mail/691.txt"
            pointer = f"(record and delegate {ref})"
            text = task_frontmatter() + f"  (pending)  \n{pointer}\n{pointer}\n"
            task.write_text(text, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("task.md"),
                        "source-pointer-dedupe",
                        source_ref=ref,
                        evidence="would be unsafe",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertIn("inside a live `(pending)` block", stderr.getvalue())

    def test_pending_marker_clear_existing_owner_item_rejects_missing_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            owner = root / "owner.md"
            text = task_frontmatter() + "(pending)\nalready tracked\n"
            source.write_text(text, encoding="utf-8")
            owner.write_text(task_frontmatter(pending_items=("different item",)) + "body\n", encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("source.md"),
                        "pending-marker-clear",
                        comment="already tracked on owner task",
                        line=pending_line,
                        ack_human=True,
                        clear_kind="existing-owner-item",
                        owner_task_file=Path("owner.md"),
                        owner_item="already tracked",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, source.read_text(encoding="utf-8"))
            self.assertIn("does not contain the cited pending item", stderr.getvalue())

    def test_pending_marker_clear_existing_owner_item_rejects_done_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            owner = root / "owner.md"
            text = task_frontmatter() + "(pending)\nalready tracked\n"
            source.write_text(text, encoding="utf-8")
            owner.write_text(task_frontmatter(status="done", pending_items=("already tracked",)) + "body\n", encoding="utf-8")
            pending_line = text.splitlines().index("(pending)") + 1
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(
                    Args(
                        root,
                        Path("source.md"),
                        "pending-marker-clear",
                        comment="already tracked on owner task",
                        line=pending_line,
                        ack_human=True,
                        clear_kind="existing-owner-item",
                        owner_task_file=Path("owner.md"),
                        owner_item="already tracked",
                    )
                )

            self.assertEqual(2, exit_code)
            self.assertEqual(text, source.read_text(encoding="utf-8"))
            self.assertIn("owner task is already done", stderr.getvalue())

    def test_pending_marker_clear_allows_main_manager_file_without_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "work_manager_today.md"
            task.write_text("header\n(pending)\nNo new task item here.\n", encoding="utf-8")

            exit_code = run(Args(root, Path("work_manager_today.md"), "pending-marker-clear", comment="informational only", line=2, clear_kind="report-only"))

            self.assertEqual(0, exit_code)
            self.assertEqual("header\nNo new task item here.\n(pending marker cleared line=2: report-only: informational only)\n", task.read_text(encoding="utf-8"))

    def test_delegate_message_appends_direct_pending_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "worker.md"
            message = root / "message.md"
            task.write_text(task_frontmatter() + "body", encoding="utf-8")
            message.write_text("Please inspect the failing shard.\n", encoding="utf-8")

            exit_code = run(Args(root, Path("worker.md"), "delegate-message", message_file=message))

            self.assertEqual(0, exit_code)
            self.assertEqual(
                task_frontmatter() + "body\n(pending)\n(from manager omo_task_edit delegate-message)\nPlease inspect the failing shard.\n",
                task.read_text(encoding="utf-8"),
            )

    def test_delegate_message_rejects_manager_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            message = root / "message.md"
            task.write_text(task_frontmatter(is_manager=True) + "body\n", encoding="utf-8")
            message.write_text("Please inspect the failing shard.\n", encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(Args(root, Path("manager.md"), "delegate-message", message_file=message))

            self.assertEqual(2, exit_code)
            self.assertIn("requires a worker task file", stderr.getvalue())

    def test_aliases_parse_to_canonical_commands(self) -> None:
        self.assertEqual("pending-list", parse_args(["list", "task.md"]).command)
        self.assertEqual("pending-add", parse_args(["add", "task.md", "--item", "new"]).command)
        self.assertEqual("pending-remove", parse_args(["remove", "task.md", "--item", "old", "--evidence", "done"]).command)
        args = parse_args(["update", "task.md", "--old-item", "old", "--new-item", "new"])

        self.assertEqual("pending-replace", args.command)
        self.assertEqual("old", args.old_item)
        self.assertEqual("new", args.new_item)
        comment = parse_args(["comment", "task.md", "noted"])
        self.assertEqual("comment-add", comment.command)
        self.assertEqual("noted", comment.comment)


if __name__ == "__main__":
    unittest.main()
