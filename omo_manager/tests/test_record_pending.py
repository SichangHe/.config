from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_blocking import ENABLE_FILE
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_record_pending import Args
from omo_manager.omo_record_pending import ack_sent_line
from omo_manager.omo_record_pending import recorded_line
from omo_manager.omo_record_pending import run


def task_frontmatter(*, status: str = "running", is_manager: bool = False, pending_items: tuple[str, ...] = ()) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
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


def v2_task_frontmatter() -> str:
    return """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000041
status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items: []
resolved_task_items: []
---
"""


class RecordPendingTests(unittest.TestCase):
    def test_records_human_item_as_v2_object_after_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(v2_task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            line = task.read_text(encoding="utf-8").splitlines().index("(pending)") + 1

            exit_code = run(Args(root, Path("task.md"), line, Path("task.md"), ("finish review",), False))

            text = task.read_text(encoding="utf-8")
            metadata = parse_task_metadata(text, root)
            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", text)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("finish review",), metadata.pending_task_items)
            self.assertTrue(metadata.pending_items[0].id.startswith("pi_"))

    def test_rejects_v2_recording_before_enablement_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = v2_task_frontmatter() + "(pending)\nPlease do it.\n"
            task.write_text(original, encoding="utf-8")
            line = original.splitlines().index("(pending)") + 1

            exit_code = run(Args(root, Path("task.md"), line, Path("task.md"), ("finish review",), False))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_records_items_and_removes_pending_line_in_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review", "email result"), False))

            text = task.read_text(encoding="utf-8")
            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", text)
            self.assertIn("pending_task_items:\n  - finish review\n  - email result\n", text)
            self.assertIn("Please do it.\n", text)

    def test_records_items_in_separate_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("existing item",)) + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertIn("pending_task_items:\n  - existing item\n  - new worker item\n", worker.read_text(encoding="utf-8"))

    def test_repeated_separate_target_recording_does_not_duplicate_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("existing item", "new worker item")) + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertEqual(1, worker.read_text(encoding="utf-8").count("new worker item"))

    def test_rejects_done_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(status="done") + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(2, exit_code)
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertIn("pending_task_items: []", worker.read_text(encoding="utf-8"))

    def test_rejects_missing_pending_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "not pending\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), False))

            self.assertEqual(2, exit_code)
            self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))

    def test_ack_human_sends_email_after_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            stdout = io.StringIO()
            commands: list[list[str]] = []
            subjects: list[str] = []
            bodies: list[str] = []

            def fake_run(command: list[str], check: bool) -> None:
                commands.append(command)
                subjects.append(Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8"))
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fake_run), redirect_stdout(stdout):
                exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), True))

            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(commands))
            self.assertIn("--manager-human", commands[0])
            self.assertIn("--subject-file", commands[0])
            self.assertIn("--message-file", commands[0])
            self.assertEqual("Request recorded\n", subjects[0])
            self.assertEqual("Added pending items:\n- finish review\n", bodies[0])
            self.assertNotIn("task.md", bodies[0])
            self.assertIn("recorded 1 pending item", stdout.getvalue())

    def test_ack_human_uses_email_file_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: Re: PB review request\n\nPlease do it.\n", encoding="utf-8")
            subjects: list[str] = []
            bodies: list[str] = []

            def fake_run(command: list[str], check: bool) -> None:
                subjects.append(Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8"))
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fake_run):
                exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("Please do it.",), True, Path("manager_mail/4002.txt")))

            self.assertEqual(0, exit_code)
            self.assertEqual(["Re: PB review request\n"], subjects)
            self.assertEqual(["Added pending items:\n- Please do it.\n"], bodies)

    def test_ack_human_retry_succeeds_after_email_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), True)

            def fail_email(command: list[str], check: bool) -> None:
                raise subprocess.CalledProcessError(1, command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))

            text_after_failure = task.read_text(encoding="utf-8")
            self.assertNotIn("(pending)\n", text_after_failure)
            self.assertIn("  - finish review\n", text_after_failure)
            commands: list[list[str]] = []
            stdout = io.StringIO()

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email), redirect_stdout(stdout):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertIn("--manager-human", commands[0])
            self.assertIn("already removed", stdout.getvalue())

    def test_ack_human_retry_ignores_unrelated_later_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), True)

            def fail_email(command: list[str], check: bool) -> None:
                raise subprocess.CalledProcessError(1, command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))
            task.write_text(task.read_text(encoding="utf-8") + "(pending)\nNew unrelated request.\n", encoding="utf-8")
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertIn("(pending)\nNew unrelated request.\n", task.read_text(encoding="utf-8"))

    def test_ack_human_retry_rejects_shifted_live_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "inserted line\n(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("new worker item",)) + "body\n", encoding="utf-8")
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), True))

            self.assertEqual(2, exit_code)
            self.assertEqual([], commands)
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))

    def test_ack_human_retry_rejects_new_marker_at_recorded_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            items = ("new worker item",)
            original = task_frontmatter(is_manager=True) + "(pending)\nNew unrelated request.\n" + recorded_line(10, items) + "\n"
            manager.write_text(original, encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=items) + "body\n", encoding="utf-8")
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), items, True))

            self.assertEqual(2, exit_code)
            self.assertEqual([], commands)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))

    def test_ack_human_retry_succeeds_after_oserror_email_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), True)

            def fail_email(command: list[str], check: bool) -> None:
                raise OSError("mail helper missing")

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))
            text_after_failure = task.read_text(encoding="utf-8")
            self.assertIn(recorded_line(10, ("finish review",)), text_after_failure)
            self.assertNotIn(ack_sent_line(10, ("finish review",)), text_after_failure)
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertIn(ack_sent_line(10, ("finish review",)), task.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
