from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking_actor import BlockingActor
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_context import current_pending_task


TASK = """---
version: v1.0.0
status: running
runat: hcfg:1
tool: codex
managerat: pb:13
is_manager: false
pending_task_items:
  - answer the human
---
work
"""

BLOCKED_TASK = TASK.replace("status: running", "status: blocked\nblocked_on: preserved history", 1)


class TaskContextTests(unittest.TestCase):
    def test_actor_recovers_owner_when_direct_tmux_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(TASK, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md hcfg:1\nprevious:\n", encoding="utf-8")
            with patch(
                "omo_manager.omo_task_context.current_tmux_target",
                side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
            ), patch(
                "omo_manager.omo_blocking_actor.request",
                return_value={
                    "ok": True,
                    "target": "hcfg:1.0",
                    "task": "task.md",
                    "task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
                    "todo_sha256": hashlib.sha256(todo.read_bytes()).hexdigest(),
                },
            ) as actor:
                self.assertEqual(task, current_active_task(root))

        actor.assert_called_once_with(root, {"operation": "active-task"})

    def test_actor_recovery_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "omo_manager.omo_task_context.current_tmux_target",
                side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
            ), patch("omo_manager.omo_blocking_actor.request", return_value={"ok": True, "task": "../task.md"}):
                with self.assertRaisesRegex(TaskFrontmatterError, "invalid task"):
                    current_active_task(root)

    def test_actor_recovers_pending_owner_with_pending_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(TASK, encoding="utf-8")
            blocked = root / "blocked.md"
            blocked.write_text(BLOCKED_TASK, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md hcfg:1\nprevious:\nblocked.md hcfg:1\n", encoding="utf-8")
            with patch(
                "omo_manager.omo_task_context.current_tmux_target",
                side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
            ), patch(
                "omo_manager.omo_blocking_actor.request",
                return_value={
                    "ok": True,
                    "target": "hcfg:1.0",
                    "task": "task.md",
                    "task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
                    "todo_sha256": hashlib.sha256(todo.read_bytes()).hexdigest(),
                },
            ) as actor:
                self.assertEqual(task, current_pending_task(root))

        actor.assert_called_once_with(root, {"operation": "pending-task"})

    def test_non_tmux_resolution_error_does_not_use_actor(self) -> None:
        with patch("omo_manager.omo_task_context.current_tmux_target", return_value="hcfg:1.0"), patch(
            "omo_manager.omo_task_context.infer_active_task",
            side_effect=TaskFrontmatterError("multiple active work queues match the current agent"),
        ), patch("omo_manager.omo_blocking_actor.request") as actor:
            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                current_active_task(Path("/work"))

        actor.assert_not_called()

    def test_actor_authenticates_peer_pane_and_process_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            actor = BlockingActor.__new__(BlockingActor)
            actor.root = root
            with patch.object(Path, "read_bytes", return_value=b"TMUX_PANE=%324\0"), patch(
                "omo_manager.omo_blocking_actor.subprocess.run",
                return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
            ), patch("omo_manager.omo_blocking_actor._ancestor_pids", return_value={4242, 5000}), patch(
                "omo_manager.omo_blocking_actor.infer_active_task", return_value=task
            ) as infer:
                self.assertEqual((task, "hcfg:1.0"), actor._active_task_for_peer(5000))

        infer.assert_called_once_with(root, "hcfg:1.0")

    def test_actor_rejects_peer_outside_claimed_pane_process(self) -> None:
        actor = BlockingActor.__new__(BlockingActor)
        actor.root = Path("/work")
        with patch.object(Path, "read_bytes", return_value=b"TMUX_PANE=%324\0"), patch(
            "omo_manager.omo_blocking_actor.subprocess.run",
            return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
        ), patch("omo_manager.omo_blocking_actor._ancestor_pids", return_value={5000}):
            with self.assertRaisesRegex(BlockingError, "does not originate"):
                actor._active_task_for_peer(5000)


if __name__ == "__main__":
    unittest.main()
