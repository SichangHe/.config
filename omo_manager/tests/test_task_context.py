from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking_actor import BlockingActor
from omo_manager.omo_omnigent_identity import NotOmniGentEnvironment
from omo_manager.omo_omnigent_identity import OmniGentIdentityError
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_context import authenticated_pending_task
from omo_manager.omo_task_context import current_pending_task
from omo_manager.omo_task_context import current_tmux_target
from omo_manager.omo_pending import parse_args as parse_pending_args


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
    def test_authenticated_pending_task_ignores_detached_explicit_task_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(os.environ, {"OMO_AGENT_TASK_FILE": "config_repair_0926.md", "TMUX": "", "TMUX_PANE": ""}),
                patch("omo_manager.omo_task_context.authenticate_current_omnigent", side_effect=NotOmniGentEnvironment("not OmniGent")),
                patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("actor did not authenticate this shell")),
                self.assertRaisesRegex(TaskFrontmatterError, "current tmux pane cannot be identified"),
            ):
                authenticated_pending_task(root)

    def test_explicit_pending_task_file_requires_exact_unique_queue_not_matching_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"TMUX": "", "TMUX_PANE": ""}):
            root = Path(temp_dir)
            task = root / "task.md"
            task.write_text(TASK.replace("is_manager: false", "is_manager: false\nsession_id: 00000000-0000-4000-8000-000000000123"), encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\ntask.md hcfg:1\n\nprevious:\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_task_context.authenticate_current_omnigent", side_effect=NotOmniGentEnvironment("not OmniGent")),
                patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("actor did not authenticate this shell")),
            ):
                self.assertEqual(task, current_pending_task(root, "task.md"))
            with patch("omo_manager.omo_task_context.current_tmux_target", return_value="hcfg:1"):
                self.assertEqual(task, current_pending_task(root, "task.md"))
                with self.assertRaisesRegex(TaskFrontmatterError, "outside the work logs"):
                    current_pending_task(root, "../other.md")
            with patch("omo_manager.omo_task_context.current_tmux_target", return_value="hcfg:2"):
                self.assertEqual(task, current_pending_task(root, "task.md"))
            with patch("omo_manager.omo_task_context._current_task", return_value=root / "another.md"):
                self.assertEqual(task, current_pending_task(root, "task.md"))
                with self.assertRaisesRegex(TaskFrontmatterError, "task file does not belong to the current agent"):
                    current_active_task(root, "task.md")
            (root / "TODO.md").write_text("current:\n\nprevious:\n", encoding="utf-8")
            with self.assertRaisesRegex(TaskFrontmatterError, "no active work queue"):
                current_pending_task(root, "task.md")

    def test_pending_cli_accepts_task_file_before_or_after_command(self) -> None:
        for arguments in (["--task-file", "task.md", "list"], ["list", "--task-file", "task.md"]):
            with self.subTest(arguments=arguments):
                self.assertEqual("task.md", parse_pending_args(arguments).task_file)

    def test_current_tmux_target_requires_the_claimed_pane_in_process_ancestry(self) -> None:
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%324"}),
            patch(
                "omo_manager.omo_task_context.subprocess.run",
                return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
            ) as tmux,
            patch("omo_manager.omo_task_context.os.getpid", return_value=5000),
            patch(
                "omo_manager.omo_task_context.Path.read_text",
                side_effect=["5000 (python) S 4242 1 1 0", "4242 (shell) S 1 1 1 0"],
            ),
        ):
            self.assertEqual("hcfg:1.0", current_tmux_target())
        self.assertEqual("%324", tmux.call_args.args[0][tmux.call_args.args[0].index("-t") + 1])

    def test_current_tmux_target_rejects_copied_pane_environment(self) -> None:
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%324"}),
            patch(
                "omo_manager.omo_task_context.subprocess.run",
                return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
            ),
            patch("omo_manager.omo_task_context.os.getpid", return_value=5000),
            patch("omo_manager.omo_task_context.os.getsid", return_value=5000),
            patch(
                "omo_manager.omo_task_context.Path.read_text",
                return_value="5000 (python) S 1 1 1 0",
            ),
        ):
            with self.assertRaisesRegex(TaskFrontmatterError, "cannot be identified"):
                current_tmux_target()

    def test_detached_tool_uses_kernel_session_of_exact_claimed_pane(self) -> None:
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%324"}),
            patch(
                "omo_manager.omo_task_context.subprocess.run",
                return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
            ),
            patch("omo_manager.omo_task_context.os.getpid", return_value=5000),
            patch("omo_manager.omo_task_context.os.getsid", return_value=4242),
            patch(
                "omo_manager.omo_task_context.Path.read_text",
                return_value="5000 (python) S 1 1 1 0",
            ),
        ):
            self.assertEqual("hcfg:1.0", current_tmux_target())

    def test_omnigent_identity_resolves_active_task_without_tmux_or_actor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = "omnigent://session-1"
            task = root / "task.md"
            task.write_text(TASK.replace("runat: hcfg:1", f"runat: {target}"), encoding="utf-8")
            (root / "TODO.md").write_text(f"current:\ntask.md {target}\nprevious:\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_task_context.current_tmux_target", return_value="main:0.0") as tmux,
                patch(
                    "omo_manager.omo_task_context.authenticate_current_omnigent",
                    return_value=SimpleNamespace(target=target),
                ),
                patch("omo_manager.omo_blocking_actor.request") as actor,
            ):
                self.assertEqual(task, current_active_task(root))
                self.assertEqual(task, current_pending_task(root))
            tmux.assert_not_called()
            actor.assert_not_called()

    def test_omnigent_identity_error_does_not_fall_back_to_tmux(self) -> None:
        with (
            patch("omo_manager.omo_task_context.current_tmux_target") as tmux,
            patch(
                "omo_manager.omo_task_context.authenticate_current_omnigent",
                side_effect=OmniGentIdentityError("cursor identity is not ready"),
            ),
            patch("omo_manager.omo_blocking_actor.request") as actor,
        ):
            with self.assertRaisesRegex(TaskFrontmatterError, "cannot be authenticated"):
                current_pending_task(Path("/work"))
        tmux.assert_not_called()
        actor.assert_not_called()

    def test_actor_recovers_owner_when_direct_tmux_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(TASK, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md hcfg:1\nprevious:\n", encoding="utf-8")
            with (
                patch(
                    "omo_manager.omo_task_context.authenticate_current_omnigent",
                    side_effect=NotOmniGentEnvironment("not omnigent"),
                ),
                patch(
                    "omo_manager.omo_task_context.current_tmux_target",
                    side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
                ),
                patch(
                    "omo_manager.omo_blocking_actor.request",
                    return_value={
                        "ok": True,
                        "target": "hcfg:1.0",
                        "task": "task.md",
                        "task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
                        "todo_sha256": hashlib.sha256(todo.read_bytes()).hexdigest(),
                    },
                ) as actor,
            ):
                self.assertEqual(task, current_active_task(root))

        actor.assert_called_once_with(root, {"operation": "active-task"})

    def test_actor_recovery_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch(
                    "omo_manager.omo_task_context.authenticate_current_omnigent",
                    side_effect=NotOmniGentEnvironment("not omnigent"),
                ),
                patch(
                    "omo_manager.omo_task_context.current_tmux_target",
                    side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
                ),
                patch("omo_manager.omo_blocking_actor.request", return_value={"ok": True, "task": "../task.md"}),
            ):
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
            with (
                patch(
                    "omo_manager.omo_task_context.authenticate_current_omnigent",
                    side_effect=NotOmniGentEnvironment("not omnigent"),
                ),
                patch(
                    "omo_manager.omo_task_context.current_tmux_target",
                    side_effect=TaskFrontmatterError("current tmux pane cannot be identified"),
                ),
                patch(
                    "omo_manager.omo_blocking_actor.request",
                    return_value={
                        "ok": True,
                        "target": "hcfg:1.0",
                        "task": "task.md",
                        "task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
                        "todo_sha256": hashlib.sha256(todo.read_bytes()).hexdigest(),
                    },
                ) as actor,
            ):
                self.assertEqual(task, current_pending_task(root))

        actor.assert_called_once_with(root, {"operation": "pending-task"})

    def test_non_tmux_resolution_error_does_not_use_actor(self) -> None:
        with (
            patch(
                "omo_manager.omo_task_context.authenticate_current_omnigent",
                side_effect=NotOmniGentEnvironment("not omnigent"),
            ),
            patch("omo_manager.omo_task_context.current_tmux_target", return_value="hcfg:1.0"),
            patch(
                "omo_manager.omo_task_context.infer_active_task",
                side_effect=TaskFrontmatterError("multiple active work queues match the current agent"),
            ),
            patch("omo_manager.omo_blocking_actor.request") as actor,
        ):
            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                current_active_task(Path("/work"))

        actor.assert_not_called()

    def test_actor_authenticates_peer_pane_and_process_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            actor = BlockingActor.__new__(BlockingActor)
            actor.root = root
            with (
                patch.object(Path, "read_bytes", return_value=b"TMUX_PANE=%324\0"),
                patch(
                    "omo_manager.omo_blocking_actor.subprocess.run",
                    return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
                ),
                patch("omo_manager.omo_blocking_actor._ancestor_pids", return_value={4242, 5000}),
                patch("omo_manager.omo_blocking_actor.infer_active_task", return_value=task) as infer,
            ):
                self.assertEqual((task, "hcfg:1.0"), actor._active_task_for_peer(5000))

        infer.assert_called_once_with(root, "hcfg:1.0")

    def test_actor_rejects_peer_outside_claimed_pane_process(self) -> None:
        actor = BlockingActor.__new__(BlockingActor)
        actor.root = Path("/work")
        with (
            patch.object(Path, "read_bytes", return_value=b"TMUX_PANE=%324\0"),
            patch(
                "omo_manager.omo_blocking_actor.subprocess.run",
                return_value=CompletedProcess([], 0, "hcfg:1.0\t4242\n", ""),
            ),
            patch("omo_manager.omo_blocking_actor._ancestor_pids", return_value={5000}),
        ):
            with self.assertRaisesRegex(BlockingError, "does not originate"):
                actor._active_task_for_peer(5000)


if __name__ == "__main__":
    unittest.main()
