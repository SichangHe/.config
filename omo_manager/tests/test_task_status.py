from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_task_status import DONE_REMINDER
from omo_manager.omo_task_status import StopArgs
from omo_manager.omo_task_status import run
from omo_manager.omo_task_status import update_frontmatter_status
from omo_manager.omo_task_status import Args as StatusArgs


def task_frontmatter(*, status: str = "running", blocked_on: str = "", pending_items: tuple[str, ...] = ()) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
    ]
    if blocked_on:
        lines.append(f"blocked_on: {blocked_on}")
    lines.extend(
        [
            "runat: wl:2",
            "tool: codex",
            "managerat: wl:1",
            "is_manager: false",
        ]
    )
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


class TaskStatusTests(unittest.TestCase):
    def test_sets_blocked_status_and_blocked_on(self) -> None:
        text = task_frontmatter() + "body\n"

        updated = update_frontmatter_status(text, "blocked", "waiting on human")

        self.assertIn("status: blocked\nblocked_on: waiting on human\n", updated)
        self.assertIn("body\n", updated)

    def test_running_removes_blocked_on(self) -> None:
        text = task_frontmatter(status="blocked", blocked_on="waiting on human") + "body\n"

        updated = update_frontmatter_status(text, "running", "")

        self.assertIn("status: running\nrunat:", updated)
        self.assertNotIn("blocked_on:", updated)

    def test_pending_marker_blocks_status_change(self) -> None:
        text = task_frontmatter() + "(pending)\nplease route\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "pending"):
            update_frontmatter_status(text, "done", "")

    def test_inline_pending_text_does_not_block_status_change(self) -> None:
        text = task_frontmatter() + "removed `(pending)` after recording it\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_quoted_pending_line_does_not_block_status_change(self) -> None:
        text = task_frontmatter() + "> (pending)\nquoted old context\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_done_rejects_pending_task_items(self) -> None:
        text = task_frontmatter(pending_items=("finish review",)) + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "pending_task_items"):
            update_frontmatter_status(text, "done", "")

    def test_pending_marker_inside_fenced_code_is_allowed(self) -> None:
        text = task_frontmatter() + "```text\n(pending)\n```\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_blocked_on_rejects_newline(self) -> None:
        text = task_frontmatter() + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "one line"):
            update_frontmatter_status(text, "blocked", "line one\ninjected: value")

    def test_blocked_requires_blocked_on(self) -> None:
        text = task_frontmatter() + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "required"):
            update_frontmatter_status(text, "blocked", "")

    def test_non_blocked_status_rejects_blocked_on(self) -> None:
        text = task_frontmatter(status="blocked", blocked_on="waiting") + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "only valid"):
            update_frontmatter_status(text, "running", "still waiting")

    def test_malformed_frontmatter_is_rejected(self) -> None:
        text = "---\nstatus: impossible\n---\nbody\n"

        with self.assertRaises(TaskFrontmatterError):
            update_frontmatter_status(text, "done", "")

    def test_cli_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            stdout = io.StringIO()
            close_calls: list[tuple[StopArgs, str]] = []

            def fake_record(args: StopArgs, session_id: str) -> None:
                close_calls.append((args, session_id))

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=fake_record,
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(close_calls))
            self.assertEqual("task.md", close_calls[0][0].task_file)
            self.assertEqual("session-1", close_calls[0][1])
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())
            self.assertIn(DONE_REMINDER, stdout.getvalue())

    def test_cli_done_reports_failure_when_close_fails_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=RuntimeError("tmux target not found")), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))
            self.assertIn("failed to close done agent", stderr.getvalue())

    def test_cli_running_has_no_done_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "running", ""))

            self.assertEqual(0, exit_code)
            self.assertEqual("", stdout.getvalue())

    def test_cli_rejects_relative_path_that_escapes_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("../task.md"), "done", ""))

            self.assertEqual(2, exit_code)


if __name__ == "__main__":
    unittest.main()
