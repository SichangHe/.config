from __future__ import annotations

import hashlib
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
from omo_manager.omo_task_status import reconcile_blocked_index
from omo_manager.omo_task_status import reconcile_done_index
from omo_manager.omo_task_status import reconcile_running_index
from omo_manager.omo_task_status import reconcile_long_running_human_index
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import reserve_private_audit
from omo_manager.omo_task_status import run
from omo_manager.omo_task_status import stop_done_agent
from omo_manager.omo_task_status import update_frontmatter_status
from omo_manager.omo_task_status import Args as StatusArgs
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_blocking import ENABLE_FILE, load_yaml_mapping, render_task, split_task_text, sync_generated_blocker
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
    def test_reconcile_long_running_human_index_preserves_task_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: human\nrunat: wl:2\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n  - wait\n---\nbody\n"
            path.write_text(text)
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md wl:2\nother.md wl:3\n\nhuman pending:\nwaiting.md wl:4\n")
            reconcile_long_running_human_index(root, path, text, path.stat())
            self.assertEqual(text, path.read_text())
            self.assertEqual("current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\nwaiting.md wl:4\n", todo.read_text())

    def test_reconcile_long_running_human_index_rejects_nonexact_human_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: human review\nrunat: wl:2\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n---\n"
            path.write_text(text)
            (root / "TODO.md").write_text("current:\ntask.md wl:2\ntask.md wl:2\n\nhuman pending:\n")
            with self.assertRaisesRegex(TaskFrontmatterError, "blocked exactly on human"):
                reconcile_long_running_human_index(root, path, text, path.stat())
            exact = text.replace("human review", "human")
            path.write_text(exact)
            with self.assertRaisesRegex(TaskFrontmatterError, "exactly one TODO row"):
                reconcile_long_running_human_index(root, path, exact, path.stat())
            (root / "TODO.md").write_text("current:\n\nhuman pending:\ntask.md wl:2\n")
            with self.assertRaisesRegex(TaskFrontmatterError, "expected.*current"):
                reconcile_long_running_human_index(root, path, exact, path.stat())

    def test_reconcile_long_running_human_index_rejects_v2_without_actor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000001
status: long_running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
blocked_on:
  - kind: persistent
    reason: human
pending_task_items: []
resolved_task_items: []
---
"""
            path.write_text(text)
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n")
            (root / "TODO.md").write_text("current:\ntask.md wl:2\n\nhuman pending:\n")
            args = StatusArgs(root, path, "", "", reconcile_long_running_human_index=True)
            with patch("omo_manager.omo_task_status.blocking_request") as actor:
                self.assertEqual(2, run(args))
            actor.assert_not_called()
            self.assertEqual(text, path.read_text())
            self.assertEqual("current:\ntask.md wl:2\n\nhuman pending:\n", (root / "TODO.md").read_text())
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

    def test_cli_running_transition_moves_single_human_pending_row_and_preserves_task_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human", runat="pb:14", managerat="pbsocialcli:0", pending_items=("keep the queue",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nother.md wl:9\n\nhuman pending:\ntask.md\n\nprevious:\nold.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            updated_task = path.read_text(encoding="utf-8")
            self.assertIn("status: running\nrunat: pb:14", updated_task)
            self.assertNotIn("blocked_on:", updated_task)
            self.assertIn("pending_task_items:\n  - keep the queue", updated_task)
            self.assertTrue(updated_task.endswith("all task content stays\n"))
            self.assertEqual("current:\ntask.md\nother.md wl:9\n\nhuman pending:\n\nprevious:\nold.md wl:2\n", todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

    def test_cli_running_transition_refuses_duplicate_or_mismatched_human_pending_row(self) -> None:
        cases = {
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n",
            "mismatched target": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:3\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                todo.write_text(todo_text, encoding="utf-8")

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_running_transition_rolls_back_todo_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")

            def fail_task_replace(target: Path, text: str, before: os.stat_result) -> None:
                if target == path:
                    raise OSError("task replace failed")
                replace_if_unchanged_locked(target, text, before)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace), redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_blocked_moves_live_rednote_path_only_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rednote-recovery.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human", runat="social:4", managerat="social-manager:1", pending_items=("human RedNote sign-in and read-only verification",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nrednote-recovery.md\nother.md wl:9\n\nhuman pending:\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("rednote-recovery.md"), "blocked", "human")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual("current:\nother.md wl:9\n\nhuman pending:\nrednote-recovery.md\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n", todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

    def test_cli_blocked_fails_closed_for_invalid_todo_placement(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nhuman pending:\nwaiting.md wl:3\n",
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n",
            "ambiguous row": "current:\ntask.md wl:2 task.md wl:2\n\nhuman pending:\n",
            "mismatched target": "current:\ntask.md wl:3\n\nhuman pending:\n",
            "target before task": "current:\nwl:2 task.md\n\nhuman pending:\n",
            "description before task": "current:\nnotes task.md\n\nhuman pending:\n",
            "malformed task suffix": "current:\ntask.mdx\n\nhuman pending:\n",
            "blocked annotation": "current:\ntask.md wl:2 (blocked: human)\n\nhuman pending:\n",
            "done annotation": "current:\ntask.md wl:2 (done)\n\nhuman pending:\n",
            "duplicate under unknown heading": "current:\ntask.md wl:2\n\nhuman pending:\n\nblocked:\ntask.md wl:2\n",
            "previous": "current:\nother.md wl:2\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n",
            "low priority": "current:\nother.md wl:2\n\nhuman pending:\n\nlow priority:\ntask.md wl:2\n",
            "duplicate destination": "current:\ntask.md wl:2\n\nhuman pending:\n\nhuman pending:\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "blocked", "human")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_blocked_reconciliation_rejects_mismatched_authoritative_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nhuman pending:\n"
            todo.write_text(todo_text, encoding="utf-8")

            with redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "blocked", "different human reason")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_done_reissue_moves_live_rednote_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "pb_social_followup.md"
            original_task = (
                task_frontmatter(status="done", runat="pb:2", managerat="wl:6")
                + "(Completed RedNote recovery: bounded live public search succeeded.)\n\n"
                + "(manager closed Codex agent 08-06 11:47 PDT; tmux target `pb:2`; session_id: `session-1`.)\n"
            )
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\npb_spectrum_news.md\n\nhuman pending:\npb_social_followup.md pb:2\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")
            expected = "current:\npb_spectrum_news.md\n\nhuman pending:\nwaiting.md wl:4\n\nprevious:\npb_social_followup.md pb:2\nold.md wl:2\n"

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                patch("omo_manager.omo_task_status.stop") as stop_agent,
                patch("omo_manager.omo_task_status.record_close") as record_close_call,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(StatusArgs(root, Path("pb_social_followup.md"), "done", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()
            record_close_call.assert_not_called()

    def test_cli_done_reissue_fails_closed_for_invalid_todo_or_task_state(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nhuman pending:\n\nprevious:\n",
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n",
            "ambiguous row": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 task.md wl:2\n\nprevious:\n",
            "mismatched target": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:3\n\nprevious:\n",
            "target before task": "current:\nother.md wl:3\n\nhuman pending:\nwl:2 task.md\n\nprevious:\n",
            "description before task": "current:\nother.md wl:3\n\nhuman pending:\nnotes task.md wl:2\n\nprevious:\n",
            "malformed task suffix": "current:\nother.md wl:3\n\nhuman pending:\ntask.mdx wl:2\n\nprevious:\n",
            "blocked annotation": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 (blocked: stale)\n\nprevious:\n",
            "done annotation": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 (done)\n\nprevious:\n",
            "wrong section": "current:\nother.md wl:3\n\nhuman pending:\n\nlow priority:\ntask.md wl:2\n\nprevious:\n",
            "already previous": "current:\nother.md wl:3\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n",
            "duplicate destination": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n\nprevious:\n",
            "mixed-case duplicate destination": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n\nPrevious:\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="done") + "close history stays\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    patch("omo_manager.omo_task_status.stop") as stop_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()
                stop_agent.assert_not_called()

        for name, original_task in {
            "nonempty queue": task_frontmatter(status="done", pending_items=("still open",)) + "history\n",
            "live pending marker": task_frontmatter(status="done") + "(pending)\nnew request\n",
            "noncanonical bytes": (task_frontmatter(status="done") + "history\n").replace("status: done\n", "status: done  \n"),
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_done_reissue_rejects_active_pane_or_current_owner(self) -> None:
        for name, pane, owner_runat in (("active pane", "%2", ""), ("active current owner", "", "wl:2"), ("conflicting current TODO owner", "", "wl:3")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="done") + "history\n"
                path.write_text(original_task, encoding="utf-8")
                owner_row = "owner.md wl:2\n" if owner_runat else ""
                if owner_runat:
                    (root / "owner.md").write_text(task_frontmatter(runat=owner_runat) + "active\n", encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = f"current:\n{owner_row}\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=pane),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_done_index_reconciliation_rechecks_task_and_todo_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="done") + "history\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            changed_task = original_task + "concurrent note\n"
            path.write_text(changed_task, encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), self.assertRaisesRegex(TaskFrontmatterError, "task changed"):
                reconcile_done_index(root, path, original_task, before)

            self.assertEqual(changed_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="done") + "history\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
            concurrent_todo = todo_text + "concurrent row\n"
            todo.write_text(todo_text, encoding="utf-8")

            def race_todo(*args: object) -> str:
                todo.write_text(concurrent_todo, encoding="utf-8")
                return "current:\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n"

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.reconcile_done_todo_text", side_effect=race_todo),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(concurrent_todo, todo.read_text(encoding="utf-8"))

    def test_cli_running_to_done_keeps_normal_close_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md wl:2\n\nprevious:\n", encoding="utf-8")
            close_args = StopArgs("wl:2", 10.0, 2000, False, False, root, "task.md", True, 0.0)

            with (
                patch("omo_manager.omo_task_status.stop_done_agent", return_value=(close_args, "session-1")) as stop_done_agent,
                patch("omo_manager.omo_task_status.record_close"),
                patch("omo_manager.omo_task_status.reconcile_done_index") as reconcile_done,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "done", "")))

            stop_done_agent.assert_called_once()
            reconcile_done.assert_not_called()
            self.assertIn("status: done", path.read_text(encoding="utf-8"))

    def test_cli_running_fails_closed_for_invalid_todo_placement(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nprevious:\nold.md wl:1\n",
            "duplicate": "current:\ntask.md wl:2\n\nprevious:\ntask.md wl:2\n",
            "ambiguous row": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 task.md wl:2\n",
            "mismatched target": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:3\n",
            "target before task": "current:\nother.md wl:2\n\nhuman pending:\nwl:3 task.md\n",
            "description before task": "current:\nother.md wl:2\n\nhuman pending:\nnotes task.md\n",
            "malformed task suffix": "current:\nother.md wl:2\n\nhuman pending:\ntask.mdx\n",
            "blocked annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 (blocked: human)\n",
            "done annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 (done)\n",
            "uppercase done annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md (DONE)\n",
            "blocked section": "current:\nother.md wl:2\n\nblocked:\ntask.md wl:2\n",
            "done section": "current:\nother.md wl:2\n\ndone:\ntask.md wl:2\n",
            "unknown nested section": "current:\nother.md wl:2\n\nhuman pending:\nnotes:\ntask.md wl:2\n",
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

    def test_cli_running_transition_moves_previous_row_to_current(self) -> None:
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
            self.assertEqual("current:\ntask.md wl:2\nother.md wl:2\n\nprevious:\n", todo.read_text(encoding="utf-8"))
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

    def test_blocked_index_reconciliation_rechecks_the_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nhuman pending:\n"
            todo.write_text(todo_text, encoding="utf-8")
            path.write_text(task_frontmatter(status="blocked", blocked_on="different") + "body\n", encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "task changed or no longer matches"):
                reconcile_blocked_index(root, path, original_task, before, "human")

            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_retires_blocked_human_target_without_tmux_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            original_task = task_frontmatter(
                status="blocked",
                blocked_on="human",
                pending_items=("first question", "second question"),
            ) + "closure evidence\nbody\n"
            path.write_text(original_task, encoding="utf-8")
            owner = root / "owner.md"
            owner_text = task_frontmatter(status="long_running", blocked_on="persistent manager role", is_manager=True) + "owner body\n"
            owner.write_text(owner_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nowner.md wl:2\n\nhuman pending:\n`stale.md` wl:2\n", encoding="utf-8")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id") as exact_pane_id,
                patch("omo_manager.omo_task_status.stop") as stop,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

            self.assertEqual(0, exit_code)
            exact_pane_id.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(original_task.replace("runat: wl:2", "runat: retired"), path.read_text(encoding="utf-8"))
            self.assertEqual(owner_text, owner.read_text(encoding="utf-8"))
            self.assertEqual("current:\nowner.md wl:2\n\nhuman pending:\n`stale.md` retired\n", todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_fails_closed_for_invalid_preconditions(self) -> None:
        cases = {
            "manager": task_frontmatter(status="blocked", blocked_on="human", is_manager=True),
            "wrong blocker": task_frontmatter(status="blocked", blocked_on="dependency"),
            "human target": task_frontmatter(status="blocked", blocked_on="human", runat="hcfg:2"),
            "empty queue": task_frontmatter(status="blocked", blocked_on="human"),
        }
        for name, stale_frontmatter in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "stale.md"
                stale_target = "hcfg:2" if name == "human target" else "wl:2"
                original_task = stale_frontmatter + "body\n"
                path.write_text(original_task, encoding="utf-8")
                (root / "owner.md").write_text(task_frontmatter(runat=stale_target) + "owner\n", encoding="utf-8")
                todo = root / "TODO.md"
                original_todo = f"current:\nowner.md {stale_target}\n\nhuman pending:\nstale.md {stale_target}\n"
                todo.write_text(original_todo, encoding="utf-8")

                with redirect_stderr(io.StringIO()):
                    exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target=stale_target, retire_blocked_target=True))

                self.assertEqual(2, exit_code)
                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_rejects_v2_before_mutation_or_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            original_task = v2_task().replace(
                "  - kind: pending_items\n    item_ids: [pi_019f0000-0000-7000-8000-000000000003]\n",
                "",
            )
            path.write_text(original_task, encoding="utf-8")
            (root / "owner.md").write_text(task_frontmatter() + "owner\n", encoding="utf-8")
            todo = root / "TODO.md"
            original_todo = "current:\nowner.md wl:2\n\nhuman pending:\nstale.md wl:2\n"
            todo.write_text(original_todo, encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.blocking_request") as reconcile, redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

            self.assertEqual(2, exit_code)
            reconcile.assert_not_called()
            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_rejects_missing_duplicate_or_nonlive_owner(self) -> None:
        for name, owner_status, duplicate in (
            ("missing", None, False),
            ("duplicate", "running", True),
            ("blocked owner", "blocked", False),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "stale.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                owner_rows = ""
                if owner_status is not None:
                    blocker = "dependency" if owner_status == "blocked" else ""
                    (root / "owner.md").write_text(task_frontmatter(status=owner_status, blocked_on=blocker) + "owner\n", encoding="utf-8")
                    owner_rows = "owner.md wl:2\n"
                if duplicate:
                    (root / "other.md").write_text(task_frontmatter() + "other\n", encoding="utf-8")
                    owner_rows += "other.md wl:2\n"
                todo = root / "TODO.md"
                original_todo = f"current:\n{owner_rows}\nhuman pending:\nstale.md wl:2\n"
                todo.write_text(original_todo, encoding="utf-8")

                with redirect_stderr(io.StringIO()):
                    exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

                self.assertEqual(2, exit_code)
                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

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

    def test_cli_v2_dependency_blocked_long_running_remains_a_normal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task(), encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.blocking_request", return_value={"ok": True}):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "long_running", "persistent contact")))

            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("blocked", metadata.status)
            self.assertEqual("long_running", metadata.resume_status)
            self.assertIn("persistent contact", metadata.blocked_on)

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

    def replacement_args(self, root: Path, **changes: object) -> StatusArgs:
        stale = root / "stale.md"
        replacement = root / "replacement.md"
        values: dict[str, object] = {
            "root": root,
            "task_file": Path("stale.md"),
            "status": "done",
            "blocked_on": "",
            "finish_replaced_done": True,
            "replacement_task": replacement,
            "stale_target": "old:2",
            "replacement_target": "new:3",
            "stale_sha256": hashlib.sha256(stale.read_bytes()).hexdigest(),
            "replacement_sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
            "replacement_status": "long_running",
            "protected_targets": ("protected:8",),
            "stopped_evidence": "verified stopped legacy target",
            "replacement_pane_evidence": "successor is active",
            "audit_output": root / "replacement.audit",
        }
        values.update(changes)
        return StatusArgs(**values)  # type: ignore[arg-type]

    def write_replacement_tasks(
        self,
        root: Path,
        *,
        stale_pending: tuple[str, ...] = (),
        replacement_managerat: str = "owner:1",
        replacement_is_manager: bool = True,
    ) -> tuple[Path, Path, str, str]:
        evidence = "verified stopped legacy target"
        stale_text = task_frontmatter(status="blocked", blocked_on="replaced", pending_items=stale_pending, runat="old:2", managerat="owner:1", is_manager=True) + f"(verified empty stale task: {evidence})\n"
        replacement_text = task_frontmatter(status="long_running", pending_items=("finish authoritative work",), runat="new:3", managerat=replacement_managerat, is_manager=replacement_is_manager)
        stale = root / "stale.md"
        replacement = root / "replacement.md"
        stale.write_text(stale_text, encoding="utf-8")
        replacement.write_text(replacement_text, encoding="utf-8")
        (root / "TODO.md").write_text("current:\nstale.md old:2\nreplacement.md new:3\n\nprevious:\n", encoding="utf-8")
        return stale, replacement, stale_text, replacement_text

    def test_finish_replaced_done_accepts_different_live_successor_and_writes_private_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, replacement, _stale_text, replacement_text = self.write_replacement_tasks(root)
            args = self.replacement_args(root)

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active") as capture_call,
                patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not signal either pane")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))

            self.assertIn("status: done\nrunat: old:2", stale.read_text(encoding="utf-8"))
            self.assertEqual(replacement_text, replacement.read_text(encoding="utf-8"))
            self.assertEqual("current:\nreplacement.md new:3\n\nprevious:\nstale.md old:2\n", (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertEqual(2, capture_call.call_count)
            capture_call.assert_called_with("%3", 2000)
            audit = root / "replacement.audit"
            self.assertEqual(0o600, audit.stat().st_mode & 0o777)
            audit_text = audit.read_text(encoding="utf-8")
            self.assertIn("replacement-pane-id: %3\n", audit_text)
            self.assertIn(f"stopped-evidence-sha256: {hashlib.sha256(args.stopped_evidence.encode()).hexdigest()}\n", audit_text)
            self.assertIn(f"replacement-pane-evidence-sha256: {hashlib.sha256(args.replacement_pane_evidence.encode()).hexdigest()}\n", audit_text)
            self.assertIn("completion: unknown-until-finalized\n", audit_text)
            self.assertIn("final-result: success\n", audit_text)

    def test_finish_replaced_done_refuses_mismatched_task_target_and_nonempty_stale_queue(self) -> None:
        for case in ("stale target", "successor target", "queue"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root, stale_pending=("still open",) if case == "queue" else ())
                args = self.replacement_args(root, stale_target="wrong:2") if case == "stale target" else self.replacement_args(root, replacement_target="wrong:3") if case == "successor target" else self.replacement_args(root)
                with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_changed_lifecycle_bytes_and_missing_successor_pane(self) -> None:
        for case in ("bytes", "pane"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, replacement, stale_text, replacement_text = self.write_replacement_tasks(root)
                args = self.replacement_args(root)

                def capture(_pane: str, _lines: int) -> str:
                    replacement.write_text(replacement_text + "changed lifecycle bytes\n", encoding="utf-8")
                    return "successor is active"

                pane = (lambda target: "" if target == "old:2" else "") if case == "pane" else (lambda target: "" if target == "old:2" else "%3")
                capture_side_effect = capture if case == "bytes" else None
                with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane), patch("omo_manager.omo_task_status.capture", side_effect=capture_side_effect) as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                if case == "pane":
                    capture_call.assert_not_called()

    def test_finish_replaced_done_revalidates_live_targets_after_audit_reservation(self) -> None:
        for case in ("stale", "successor pane", "successor evidence"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                reserved = False

                def reserve(path: Path, text: str) -> None:
                    nonlocal reserved
                    reserve_private_audit(path, text)
                    reserved = True

                def pane_id(target: str) -> str:
                    if target == "old:2":
                        return "%2" if reserved and case == "stale" else ""
                    return "%9" if reserved and case == "successor pane" else "%3"

                def capture(_pane: str, _lines: int) -> str:
                    return "evidence changed" if reserved and case == "successor evidence" else "successor is active"

                with (
                    patch("omo_manager.omo_task_status.reserve_private_audit", side_effect=reserve),
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_id),
                    patch("omo_manager.omo_task_status.capture", side_effect=capture),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(self.replacement_args(root)))

                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertIn("final-result: not-completed", (root / "replacement.audit").read_text(encoding="utf-8"))

    def test_finish_replaced_done_refuses_manager_owner_or_role_mismatch(self) -> None:
        for case in ("owner", "role"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(
                    root,
                    replacement_managerat="other:1" if case == "owner" else "owner:1",
                    replacement_is_manager=case != "role",
                )
                args = self.replacement_args(root)
                with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"), patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_duplicate_or_invalid_competing_successor_owner(self) -> None:
        for case in ("duplicate", "invalid"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                competitor = root / "competitor.md"
                competitor.write_text(
                    task_frontmatter(status="running", pending_items=("competing work",), runat="new:3", managerat="owner:1", is_manager=True)
                    if case == "duplicate"
                    else task_frontmatter(status="running", pending_items=("competing work",), runat="new:3", managerat="owner:1", is_manager=True).replace("runat: new:3\n", "runat: new:3\nrunat: new:3\n"),
                    encoding="utf-8",
                )
                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                    patch("omo_manager.omo_task_status.capture") as capture_call,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(self.replacement_args(root)))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_human_or_explicitly_protected_target_before_capture(self) -> None:
        for case in ("human", "protected"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                if case == "human":
                    stale.write_text(stale_text.replace("runat: old:2", "runat: hlegacy:2"), encoding="utf-8")
                    args = self.replacement_args(
                        root,
                        stale_target="hlegacy:2",
                        stale_sha256=hashlib.sha256(stale.read_bytes()).hexdigest(),
                    )
                else:
                    args = self.replacement_args(root, protected_targets=("new:3.0",))
                with patch("omo_manager.omo_task_status.exact_pane_id") as pane_call, patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                pane_call.assert_not_called()
                capture_call.assert_not_called()
                self.assertFalse((root / "replacement.audit").exists())

    def test_finish_replaced_done_leaves_durable_unknown_audit_if_success_finalization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, _replacement, _stale_text, _replacement_text = self.write_replacement_tasks(root)
            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active"),
                patch("omo_manager.omo_task_status.finish_private_audit", side_effect=TaskFrontmatterError("audit storage failed")),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(self.replacement_args(root)))
            self.assertIn("status: done\nrunat: old:2", stale.read_text(encoding="utf-8"))
            audit_text = (root / "replacement.audit").read_text(encoding="utf-8")
            self.assertIn("completion: unknown-until-finalized", audit_text)
            self.assertNotIn("final-result:", audit_text)

    def test_finish_replaced_done_rolls_back_todo_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
            todo = root / "TODO.md"
            todo_text = todo.read_text(encoding="utf-8")
            args = self.replacement_args(root)

            def fail_task_replace(target: Path, text: str, before: object) -> None:
                if target == stale:
                    raise OSError("task write failed")
                replace_if_unchanged_locked(target, text, before)  # type: ignore[arg-type]

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active"),
                patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))

            self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertIn("final-result: not-completed", (root / "replacement.audit").read_text(encoding="utf-8"))

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

    def test_cli_recover_exited_shell_done_closes_and_finishes_exact_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = "%42"
            session_id = "11111111-2222-3333-4444-555555555555"
            path = root / "task.md"
            blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
            path.write_text(task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1") + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md cfg:1\n\nprevious:\nother.md\n", encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                session_id=session_id,
                recover_exited_shell_done=True,
                pane_id=pane,
                terminal_evidence="accepted-report-token",
            )

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=pane),
                patch("omo_manager.omo_task_status.close_exited_codex_shell") as close,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(args)

            self.assertEqual(0, exit_code)
            close.assert_called_once_with("cfg:1", pane, session_id, "accepted-report-token")
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat: cfg:1", text)
            self.assertIn(f"session_id: `{session_id}`", text)
            self.assertEqual("current:\n\nprevious:\ntask.md cfg:1\nother.md\n", todo.read_text(encoding="utf-8"))

    def test_cli_recover_exited_shell_done_rejects_unsafe_task_or_index(self) -> None:
        pane = "%42"
        session_id = "11111111-2222-3333-4444-555555555555"
        blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
        cases = (
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1", is_manager=True), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1", pending_items=("open",)), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on="done_close_failed: another failure", runat="cfg:1"), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1"), "task.md (done)", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1"), "task.md cfg:1", task_frontmatter(status="running", runat="cfg:1")),
        )
        for task_text, todo_row, reused_text in cases:
            with self.subTest(todo_row=todo_row, reused=bool(reused_text)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original = task_text + "body\n"
                path.write_text(original, encoding="utf-8")
                todo = root / "TODO.md"
                todo_original = f"current:\n{todo_row}\n\nprevious:\n"
                todo.write_text(todo_original, encoding="utf-8")
                if reused_text is not None:
                    (root / "reused.md").write_text(reused_text + "body\n", encoding="utf-8")
                args = StatusArgs(
                    root,
                    Path("task.md"),
                    "done",
                    "",
                    session_id=session_id,
                    recover_exited_shell_done=True,
                    pane_id=pane,
                    terminal_evidence="accepted-report-token",
                )
                with patch("omo_manager.omo_task_status.close_exited_codex_shell") as close, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                close.assert_not_called()
                self.assertEqual(original, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_original, todo.read_text(encoding="utf-8"))

    def test_cli_recover_exited_shell_done_rejects_human_pending_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = "%42"
            session_id = "11111111-2222-3333-4444-555555555555"
            blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
            path = root / "task.md"
            original = task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1") + "body\n"
            path.write_text(original, encoding="utf-8")
            todo = root / "TODO.md"
            todo_original = "current:\n\nhuman pending:\ntask.md cfg:1\n\nprevious:\n"
            todo.write_text(todo_original, encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                session_id=session_id,
                recover_exited_shell_done=True,
                pane_id=pane,
                terminal_evidence="accepted-report-token",
            )

            with patch("omo_manager.omo_task_status.close_exited_codex_shell") as close, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))

            close.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_original, todo.read_text(encoding="utf-8"))

    def test_parse_recover_exited_shell_done_requires_explicit_evidence(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--recover-exited-shell-done", "task.md"])

        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--recover-exited-shell-done",
                "--pane-id",
                "%42",
                "--session-id",
                "11111111-2222-3333-4444-555555555555",
                "--terminal-evidence",
                "accepted-report-token",
                "task.md",
            ]
        )
        self.assertTrue(args.recover_exited_shell_done)
        self.assertEqual("%42", args.pane_id)

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
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--root", "/tmp/work", "--finish-replaced-done", "task.md"])

        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--finish-replaced-done",
                "--replacement-task",
                "/tmp/replacement.md",
                "--stale-target",
                "old:2",
                "--replacement-target",
                "new:3",
                "--stale-sha256",
                "a" * 64,
                "--replacement-sha256",
                "b" * 64,
                "--replacement-status",
                "long_running",
                "--protected-target",
                "protected:8",
                "--stopped-evidence",
                "verified stop",
                "--replacement-pane-evidence",
                "replacement output",
                "--audit-output",
                "/tmp/replacement.audit",
                "task.md",
            ]
        )

        self.assertTrue(args.finish_replaced_done)
        self.assertEqual(Path("/tmp/replacement.md"), args.replacement_task)
        self.assertEqual(("protected:8",), args.protected_targets)

    def test_cli_running_has_no_done_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\nhuman pending:\ntask.md wl:2\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(StatusArgs(root, Path("task.md"), "running", ""))

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
