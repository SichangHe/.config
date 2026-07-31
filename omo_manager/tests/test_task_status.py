from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import DONE_REMINDER
from omo_manager.omo_task_status import StopArgs
from omo_manager.omo_task_status import parse_args
from omo_manager.omo_task_status import reconcile_running_index
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import run
from omo_manager.omo_task_status import stop_done_agent
from omo_manager.omo_task_status import update_frontmatter_status
from omo_manager.omo_task_status import Args as StatusArgs
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_blocking import load_yaml_mapping, render_task, split_task_text, sync_generated_blocker
from omo_manager.tests.test_task_metadata_v2 import v2_task


def task_frontmatter(
    *,
    status: str = "running",
    blocked_on: str = "",
    pending_items: tuple[str, ...] = (),
    runat: str = "wl:2",
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
            f"runat: {runat}",
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
    def test_stop_done_agent_treats_verified_missing_exact_pane_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch("omo_manager.omo_task_status.stop") as stop_call:
                stop_args, session_id = stop_done_agent(root, path, metadata)

            stop_call.assert_not_called()
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("", session_id)

    def test_cli_done_closes_verified_missing_pane_and_moves_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manager.md"
            path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nmanager.md wl:3\n\nprevious:\n", encoding="utf-8")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop") as stop_call,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(0, exit_code)
            stop_call.assert_not_called()
            self.assertIn("status: done\nrunat: wl:3", path.read_text(encoding="utf-8"))
            self.assertIn("tmux target `wl:3`; Codex session id not found", path.read_text(encoding="utf-8"))
            self.assertIn("current:\n\nprevious:\nmanager.md wl:3\n", todo.read_text(encoding="utf-8"))

    def test_stop_done_agent_accepts_disappearance_during_status_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None
            paste_error = subprocess.CalledProcessError(1, ["tmux", "paste-buffer", "-t", "%3"])

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", "")),
                patch("omo_manager.omo_task_status.pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop", side_effect=paste_error) as stop_call,
            ):
                stop_args, session_id = stop_done_agent(root, path, metadata)

            self.assertEqual("%3", stop_call.call_args.args[0].target)
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("", session_id)

    def test_stop_done_agent_propagates_failure_for_live_or_reused_pane(self) -> None:
        for current_pane_id, original_pane_id in (("%3", "%3"), ("%4", ""), ("", "%3")):
            with self.subTest(current_pane_id=current_pane_id, original_pane_id=original_pane_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", current_pane_id)),
                    patch("omo_manager.omo_task_status.pane_id", return_value=original_pane_id),
                    patch("omo_manager.omo_task_status.stop", side_effect=RuntimeError("status paste failed")),
                    self.assertRaisesRegex(RuntimeError, "status paste failed"),
                ):
                    stop_done_agent(root, path, metadata)

    def test_stop_done_agent_allows_unique_current_worker_to_close_own_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "stale_done.md").write_text(task_frontmatter(status="done", runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\nstale_done.md wl:3\n\nhuman pending:\nstale.md\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertTrue(stop_args.allow_self)
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("%3", stop_call.call_args.args[0].target)
            self.assertTrue(stop_call.call_args.args[0].allow_self)

    def test_stop_done_agent_ignores_valid_noncurrent_target_reuse(self) -> None:
        for section in ("Previous", "Human Pending"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
                (root / "stale.md").write_text(task_frontmatter(status="blocked", blocked_on="waiting for human", runat="wl:3") + "body\n", encoding="utf-8")
                (root / "TODO.md").write_text(f"Current\nworker.md wl 3\n\n{section}:\nstale.md wl:3\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertTrue(stop_args.allow_self)

    def test_stop_done_agent_refuses_malformed_current_record(self) -> None:
        for todo_line in ("broken.md wl:3", "broken.md wl 3"):
            with self.subTest(todo_line=todo_line), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
                (root / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
                (root / "TODO.md").write_text(f"current:\nworker.md wl:3\n{todo_line}\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_rechecks_current_ownership_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            other = root / "other.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch(
                "omo_manager.omo_task_status.current_target_task_paths", side_effect=((path,), (other, path))
            ), patch(
                "omo_manager.omo_task_status.stop", return_value="session-1"
            ):
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_refuses_self_close_after_pane_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", "%4")), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)
            self.assertEqual("%3", stop_call.call_args.args[0].target)

    def test_stop_done_agent_does_not_fall_back_to_prefix_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:1", managerat="main:0") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:1\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)
            self.assertEqual("", session_id)
            stop_call.assert_not_called()

    def test_stop_done_agent_refuses_self_close_for_manager_or_current_collision(self) -> None:
        cases = {
            "manager task": (True, "current:\nworker.md wl:3\n"),
            "current collision": (False, "current:\nworker.md wl:3\nmanager.md wl:3\n"),
        }
        for name, (is_manager, todo_text) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3", is_manager=is_manager) + "body\n", encoding="utf-8")
                (root / "manager.md").write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_refuses_configured_main_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch.dict(os.environ, {"OMO_MANAGER_TMUX_TARGET": "wl:3"}), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)

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

    def test_cli_running_moves_the_single_previous_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(runat="vl_root_consolidate:1", managerat="vl_stage1_mgr:0", pending_items=("keep the queue",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text(
                "preamble stays\ncurrent:\n\ncurrent.md wl:9 unchanged\n\nhuman pending:\nwaiting.md wl:4 unchanged\n\nprevious:\nold.md wl:2 unchanged\n  task.md vl_root_consolidate:1 keep this row exactly\nlater.md wl:3 unchanged\n",
                encoding="utf-8",
            )
            expected = "preamble stays\ncurrent:\n\n  task.md vl_root_consolidate:1 keep this row exactly\ncurrent.md wl:9 unchanged\n\nhuman pending:\nwaiting.md wl:4 unchanged\n\nprevious:\nold.md wl:2 unchanged\nlater.md wl:3 unchanged\n"

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))
            self.assertEqual(1, todo.read_text(encoding="utf-8").count("task.md vl_root_consolidate:1 keep this row exactly"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))

    def test_cli_running_fails_closed_for_invalid_todo_placement(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nprevious:\nold.md wl:1\n",
            "duplicate": "current:\ntask.md wl:2\n\nprevious:\ntask.md wl:2\n",
            "human pending": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2\n",
            "low priority": "current:\nother.md wl:2\n\nlow priority:\ntask.md wl:2\n",
            "duplicate current": "current:\ntask.md wl:2\n\ncurrent:\nother.md wl:3\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter() + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")
                stderr = io.StringIO()

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(stderr):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_running_transition_does_not_reconcile_todo_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nother.md wl:2\n\nprevious:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertIn("status: running", path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()

    def test_running_index_reconciliation_rechecks_the_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter() + "body\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\n\nprevious:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "task changed while running index reconciliation"):
                reconcile_running_index(root, path, original_task, before)

            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_running_index_reconciliation_deduplicates_a_todo_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            todo.write_text(task_frontmatter() + "current:\n\nprevious:\nTODO.md wl:2\n", encoding="utf-8")
            text = todo.read_text(encoding="utf-8")

            reconcile_running_index(root, todo, text, todo.stat())

            self.assertIn("current:\n\nTODO.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_long_running_is_a_normal_status_transition(self) -> None:
        text = task_frontmatter() + "body\n"

        with patch("omo_manager.omo_task_status.frontmatter_parts", wraps=frontmatter_parts) as parse_parts:
            updated = update_frontmatter_status(text, "long_running", "persistent contact")

        parse_parts.assert_called_once_with(text)
        self.assertIn("status: long_running\nblocked_on: persistent contact\nrunat:", updated)
        self.assertIn("body\n", updated)

    def test_long_running_allows_missing_blocked_on(self) -> None:
        updated = update_frontmatter_status(task_frontmatter() + "body\n", "long_running", "")

        self.assertIn("status: long_running\nrunat:", updated)
        self.assertNotIn("blocked_on:", updated)

    def test_v2_dependency_blocked_long_running_preserves_resume_reason(self) -> None:
        updated = update_frontmatter_status(v2_task(), "long_running", "persistent contact")

        metadata = parse_task_metadata(updated)

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertEqual("long_running", metadata.resume_status)
        self.assertIn("persistent contact", metadata.blocked_on)

        frontmatter, body = split_task_text(updated)
        values = load_yaml_mapping(frontmatter)
        values["pending_task_items"][0]["blocked_on"] = []
        self.assertTrue(sync_generated_blocker(values))
        resumed = parse_task_metadata(render_task(values, body))
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual("long_running", resumed.status)
        self.assertEqual("persistent contact", resumed.blocked_on)

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
            original = task_frontmatter(runat="vl:2", managerat="vl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(task_frontmatter(runat="vl:3", managerat="vl:2.0") + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager with children")) as stop_agent, redirect_stderr(stderr):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()
            self.assertIn("manager task still owns active child task(s): child.md", stderr.getvalue())
            self.assertIn("--migrate-manager-owner --old-manager-target vl:2 --new-manager-target vl:1", stderr.getvalue())

    def test_cli_done_allows_manager_with_done_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            notes = root / "notes.md"
            manager.write_text(task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n", encoding="utf-8")
            child.write_text(task_frontmatter(status="done", runat="retired", managerat="wl:2.0") + "body\n", encoding="utf-8")
            child_before = child.read_bytes()
            notes.write_text("---\nversion: article\nstatus: draft\n---\nnotes\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, root, "manager.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close"
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(0, exit_code)
            self.assertEqual(child_before, child.read_bytes())
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())

    def test_cli_done_rejects_manager_with_active_retired_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(task_frontmatter(runat="retired", managerat="wl:2.0") + "body\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager with invalid child")) as stop_agent, redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()

    def test_cli_done_rejects_manager_when_child_ownership_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n"
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

    def test_finish_replaced_done_closes_stale_bookkeeping_and_preserves_live_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = root / "replacement.md"
            evidence = "Old pane was stopped and replaced by the authoritative task."
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="wrong bookkeeping root; replaced")
                + f"(verified removed pending item: {evidence})\n",
                encoding="utf-8",
            )
            replacement.write_text(task_frontmatter(status="blocked", blocked_on="waiting on repair", pending_items=("finish real work",)), encoding="utf-8")
            replacement_before = replacement.read_bytes()
            todo = root / "TODO.md"
            todo.write_text("current:\nstale.md wl:2\nreplacement.md wl:2\n\nprevious:\nold.md wl:1\n", encoding="utf-8")
            stdout = io.StringIO()
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="authoritative replacement paused")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value="%2"),
                patch("omo_manager.omo_task_status.capture", return_value="authoritative replacement paused") as capture_call,
                patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not stop reused pane")),
                patch("omo_manager.omo_task_status.record_close", side_effect=AssertionError("must not append a close note for replacement")),
                redirect_stdout(stdout),
            ):
                exit_code = run(args)

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))
            self.assertEqual(replacement_before, replacement.read_bytes())
            capture_call.assert_called_once_with("%2", 2000)
            self.assertIn("current:\nreplacement.md wl:2\n\nprevious:\nstale.md wl:2\nold.md wl:1\n", todo.read_text(encoding="utf-8"))
            self.assertIn("without signaling reused replacement pane wl:2", stdout.getvalue())

    def test_finish_replaced_done_retires_empty_running_stale_record_without_signaling_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = root / "replacement.md"
            evidence = "The stale legacy record has no pending work and the replacement owns the reused pane."
            path.write_text(task_frontmatter() + f"(verified empty stale task: {evidence})\n", encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("finish real work",)), encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nstale.md wl:2\nreplacement.md wl:2\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="authoritative replacement")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value="%2"),
                patch("omo_manager.omo_task_status.capture", return_value="authoritative replacement"),
                patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not stop reused pane")) as stop_agent,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))

            stop_agent.assert_not_called()
            self.assertIn("status: done", path.read_text(encoding="utf-8"))
            self.assertIn("previous:\nstale.md wl:2", todo.read_text(encoding="utf-8"))

    def test_finish_replaced_done_rejects_manager_with_active_children_before_pane_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = root / "replacement.md"
            evidence = "The stale manager has no pending work and the replacement owns the reused pane."
            original = task_frontmatter(is_manager=True) + f"(verified empty stale task: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("finish real work",)), encoding="utf-8")
            (root / "child.md").write_text(task_frontmatter(managerat="wl:2", pending_items=("child work",)), encoding="utf-8")
            (root / "TODO.md").write_text("current:\nstale.md wl:2\nreplacement.md wl:2\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            with patch("omo_manager.omo_task_status.exact_pane_id") as pane_call, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))

            self.assertEqual(original, path.read_text(encoding="utf-8"))
            pane_call.assert_not_called()

    def test_finish_replaced_done_rejects_replacement_outside_work_log_before_pane_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "The stale legacy record has no pending work and the replacement owns the reused pane."
            original = task_frontmatter() + f"(verified empty stale task: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("finish real work",)), encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            with patch("omo_manager.omo_task_status.exact_pane_id") as pane_call, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))

            self.assertEqual(original, path.read_text(encoding="utf-8"))
            pane_call.assert_not_called()

    def test_finish_replaced_done_rejects_pending_stale_task_before_pane_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "Old pane was stopped."
            original = task_frontmatter(status="blocked", blocked_on="replaced", pending_items=("still open",)) + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("real work",)), encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                exit_code = run(args)

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            capture_call.assert_not_called()

    def test_finish_replaced_done_rejects_wrong_record_or_pane_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "Old pane was stopped."
            original = task_frontmatter(status="blocked", blocked_on="replaced") + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("real work",)), encoding="utf-8")
            wrong_record = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence="wrong", replacement_pane_evidence="replacement")
            wrong_pane = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="wrong pane")

            with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(wrong_record))
            capture_call.assert_not_called()
            with patch("omo_manager.omo_task_status.capture", return_value="replacement is here"), redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(wrong_pane))
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_finish_replaced_done_rejects_ambiguous_nonreplacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "Old pane was stopped."
            original = task_frontmatter(status="blocked", blocked_on="replaced") + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(status="done"), encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                exit_code = run(args)

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            capture_call.assert_not_called()

    def test_finish_replaced_done_rejects_live_original_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "Old pane was stopped."
            original = task_frontmatter() + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("real work",)), encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                exit_code = run(args)

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            capture_call.assert_not_called()

    def test_finish_replaced_done_rolls_todo_back_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as replacement_tmp:
            root = Path(tmp)
            path = root / "stale.md"
            replacement = Path(replacement_tmp) / "replacement.md"
            evidence = "Old pane was stopped."
            original = task_frontmatter(status="blocked", blocked_on="replaced") + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            replacement.write_text(task_frontmatter(pending_items=("real work",)), encoding="utf-8")
            todo = root / "TODO.md"
            todo_original = "current:\nstale.md wl:2\n"
            todo.write_text(todo_original, encoding="utf-8")
            args = StatusArgs(root, Path("stale.md"), "done", "", finish_replaced_done=True, replacement_task=replacement, stopped_evidence=evidence, replacement_pane_evidence="replacement")

            def fail_task_replace(target: Path, text: str, before: object) -> None:
                if target == path:
                    raise OSError("task write failed")
                replace_if_unchanged(target, text, before)  # type: ignore[arg-type]

            with patch("omo_manager.omo_task_status.capture", return_value="replacement"), patch("omo_manager.omo_task_status.replace_if_unchanged", side_effect=fail_task_replace), redirect_stderr(io.StringIO()):
                exit_code = run(args)

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_original, todo.read_text(encoding="utf-8"))

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

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("should not stop twice")), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertNotIn("blocked_on:", text)
            self.assertIn("session_id: `session-1`", text)
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())
            self.assertIn(DONE_REMINDER, stdout.getvalue())

    def test_cli_finish_closed_done_failure_stays_blocked_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failed: TODO locked") + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.finish_done_transaction", side_effect=RuntimeError("TODO still locked")), redirect_stderr(stderr):
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

    def test_cli_finish_closed_done_accepts_close_failed_with_matching_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_failed: refusing to stop the current pane: wl:2")
                + "(manager closed Codex agent 07-15 00:19 PDT; tmux target `wl:2`; session_id: `session-1`.)\n",
                encoding="utf-8",
            )

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("already closed")), redirect_stdout(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))

    def test_cli_finish_closed_done_accepts_missing_worker_already_in_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_failed: target disappeared during status query") + "body\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text("current:\n\nprevious:\ntask.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch(
                "omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("already closed")
            ), redirect_stdout(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("task.md"), "done", "", True, "unverified-session"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertIn("Codex session id not found in captured tmux output", text)
            self.assertNotIn("unverified-session", text)

    def test_cli_finish_closed_done_does_not_trust_missing_manager_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_failed: target disappeared", is_manager=True) + "body\n"
            path.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("previous:\ntask.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("task.md"), "done", "", True, ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_cli_finish_closed_done_rejects_close_failed_with_wrong_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = (
                task_frontmatter(status="blocked", blocked_on="done_close_failed: refusing to stop the current pane: wl:2")
                + "(manager closed Codex agent 07-15 00:19 PDT; tmux target `wl:2`; session_id: `session-1`.)\n"
            )
            path.write_text(original, encoding="utf-8")

            with redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "wrong-session"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

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
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

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
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

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
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

    def test_parse_finish_closed_done_without_status(self) -> None:
        args = parse_args(["--root", "/tmp/work", "--finish-closed-done", "--session-id", "session-1", "task.md"])

        self.assertEqual(Path("/tmp/work"), args.root)
        self.assertEqual(Path("task.md"), args.task_file)
        self.assertEqual("done", args.status)
        self.assertTrue(args.finish_closed_done)
        self.assertEqual("session-1", args.session_id)

    def test_parse_finish_replaced_done_requires_all_explicit_evidence(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work", "--finish-replaced-done", "task.md"])

        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--finish-replaced-done",
                "--replacement-task",
                "/tmp/replacement.md",
                "--stopped-evidence",
                "verified stop",
                "--replacement-pane-evidence",
                "replacement output",
                "task.md",
            ]
        )

        self.assertTrue(args.finish_replaced_done)
        self.assertEqual(Path("/tmp/replacement.md"), args.replacement_task)

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
