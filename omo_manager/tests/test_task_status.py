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
from omo_manager.omo_task_status import parse_args
from omo_manager.omo_task_status import run
from omo_manager.omo_task_status import update_frontmatter_status
from omo_manager.omo_task_status import Args as StatusArgs


def task_frontmatter(
    *,
    status: str = "running",
    blocked_on: str = "",
    pending_items: tuple[str, ...] = (),
    managerat: str = "wl:1",
    is_manager: bool = False,
) -> str:
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
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
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

        with self.assertRaisesRegex(TaskFrontmatterError, "verify each pending item is actually complete"):
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

    def test_cli_done_rejects_manager_with_active_child_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(managerat="wl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(
                "\n".join(
                    [
                        "---",
                        "version: v1.0.0",
                        "status: running",
                        "runat: wl:3",
                        "tool: codex",
                        "managerat: wl:2.0",
                        "is_manager: false",
                        "pending_task_items: []",
                        "---",
                        "body",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager with children")) as stop_agent, redirect_stderr(stderr):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            self.assertIn("--migrate-manager-owner --old-manager-target wl:2 --new-manager-target wl:1", stderr.getvalue())
            stop_agent.assert_not_called()
            self.assertIn("manager task still owns active child task(s): child.md", stderr.getvalue())

    def test_cli_done_allows_manager_with_done_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            notes = root / "notes.md"
            manager.write_text(task_frontmatter(managerat="wl:1", is_manager=True) + "body\n", encoding="utf-8")
            child.write_text(
                "\n".join(
                    [
                        "---",
                        "version: v1.0.0",
                        "status: done",
                        "runat: wl:3",
                        "tool: codex",
                        "managerat: wl:2.0",
                        "is_manager: false",
                        "pending_task_items: []",
                        "---",
                        "body",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            notes.write_text("---\nversion: article\nstatus: draft\n---\nnotes\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, root, "manager.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close"
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(0, exit_code)
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())

    def test_cli_done_rejects_manager_when_child_ownership_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(managerat="wl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(
                "\n".join(
                    [
                        "---",
                        "version: v1.0.0",
                        "status: running",
                        "runat: wl:3",
                        "tool: codex",
                        "managerat: wl:9",
                        "managerat: wl:2",
                        "is_manager: false",
                        "unexpected: bad",
                        "pending_task_items: []",
                        "---",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager when ownership is uncertain")) as stop_agent, redirect_stderr(stderr):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()
            self.assertIn("cannot verify manager child ownership because `child.md` has invalid task frontmatter", stderr.getvalue())

    def test_cli_done_failure_marks_blocked_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter() + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=RuntimeError("tmux target not found")), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertNotEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("status: blocked\nblocked_on: done_close_failed: tmux target not found\n", path.read_text(encoding="utf-8"))
            self.assertIn("failed to close done agent", stderr.getvalue())

    def test_cli_done_marks_blocked_when_close_bookkeeping_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=RuntimeError("TODO locked"),
            ), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: TODO locked\n", text)
            self.assertIn("done close bookkeeping failed", stderr.getvalue())

    def test_cli_done_bookkeeping_failure_normalizes_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=RuntimeError("line one\nline two"),
            ), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: line one line two\n", path.read_text(encoding="utf-8"))

    def test_cli_done_write_failure_after_close_leaves_blocked_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter() + "body\n"
            path.write_text(original, encoding="utf-8")

            def flaky_replace(target: Path, text: str, before: object) -> None:
                if "status: done" in text:
                    raise RuntimeError("write raced")
                target.write_text(text, encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.replace_if_unchanged",
                side_effect=flaky_replace,
            ), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertNotEqual(original, text)
            self.assertIn("status: blocked\nblocked_on: done_close_in_progress: manager is closing the agent before marking done\n", text)
            self.assertIn("session_id: `session-1`", text)
            self.assertNotIn("status: done", text)

    def test_cli_finish_closed_done_records_bookkeeping_without_closing_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failed: TODO locked") + "body\n", encoding="utf-8")
            stdout = io.StringIO()
            close_calls: list[tuple[StopArgs, str]] = []

            def fake_record(args: StopArgs, session_id: str) -> None:
                close_calls.append((args, session_id))

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("should not stop twice")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=fake_record,
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))
            self.assertNotIn("blocked_on:", path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(close_calls))
            self.assertEqual("session-1", close_calls[0][1])
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())
            self.assertIn(DONE_REMINDER, stdout.getvalue())

    def test_cli_finish_closed_done_failure_stays_blocked_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failed: TODO locked") + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.record_close", side_effect=RuntimeError("TODO still locked")), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: TODO still locked\n", text)
            self.assertIn("done close bookkeeping retry failed", stderr.getvalue())

    def test_cli_finish_closed_done_allows_close_in_progress_with_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_in_progress: manager is closing the agent before marking done")
                + "body\n"
                + "(manager closed Codex agent 07-14 11:00 PDT; tmux target `wl:2`; session_id: `session-1`.)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("should not stop twice")), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertNotIn("blocked_on:", text)
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())

    def test_cli_finish_closed_done_rejects_pre_close_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_failed: tmux target not found") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires a task blocked by failed done close", stderr.getvalue())

    def test_cli_finish_closed_done_rejects_invalid_bookkeeping_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failedly: forged") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires a task blocked by failed done close", stderr.getvalue())

    def test_cli_finish_closed_done_rejects_close_in_progress_without_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_in_progress: manager is closing the agent before marking done") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires a task blocked by failed done close", stderr.getvalue())

    def test_parse_finish_closed_done_without_status(self) -> None:
        args = parse_args(["--root", "/tmp/work", "--finish-closed-done", "--session-id", "session-1", "task.md"])

        self.assertEqual(Path("/tmp/work"), args.root)
        self.assertEqual(Path("task.md"), args.task_file)
        self.assertEqual("done", args.status)
        self.assertTrue(args.finish_closed_done)
        self.assertEqual("session-1", args.session_id)

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
