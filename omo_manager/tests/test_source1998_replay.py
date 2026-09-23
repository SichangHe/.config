from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager import omo_task_status as status


class Source1998ReplayTests(unittest.TestCase):
    def test_after_after_replay_expects_only_previous_target_claim(self) -> None:
        task_before = b"task-before"
        task_after = b"task-after"
        todo_before = b"current:\ntask.md config:45\nprevious:\n"
        todo_after = b"current:\n\nprevious:\ntask.md\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            todo = root / "TODO.md"
            task.write_bytes(task_after)
            todo.write_bytes(todo_after)
            task_state = task.stat()
            todo_state = todo.stat()
            metadata = SimpleNamespace(
                version="v1.0.0",
                status="blocked",
                blocked_on="blocker",
                runat="config:45",
                managerat="config:39",
                tool="codex",
                session_id="session-1998",
                is_manager=False,
                pending_task_items=(),
            )
            args = SimpleNamespace(root=root)
            with patch.multiple(
                status,
                SOURCE1998_TASK_SHA256=hashlib.sha256(task_before).hexdigest(),
                SOURCE1998_TASK_AFTER_SHA256=hashlib.sha256(task_after).hexdigest(),
                SOURCE1998_TODO_SHA256=hashlib.sha256(todo_before).hexdigest(),
                SOURCE1998_TODO_AFTER_SHA256=hashlib.sha256(todo_after).hexdigest(),
                SOURCE1998_TARGET="config:45",
                SOURCE1998_MANAGER="config:39",
                SOURCE1998_BLOCKER="blocker",
                SOURCE1998_SESSION_ID="session-1998",
            ), patch.object(status, "source1998_stable_file_snapshot", side_effect=[(task_after, task_state), (todo_after, todo_state)]), patch.object(
                status, "update_frontmatter_status", return_value=task_after.decode()
            ), patch.object(status, "reconcile_todo_text", return_value=todo_after.decode()), patch.object(
                status, "parse_task_metadata", return_value=metadata
            ), patch.object(status, "validate_source1998_envelope"), patch.object(
                status, "authoritative_active_target_task_paths", return_value=()
            ), patch.object(status, "source1998_todo_target_claims", return_value=()), patch.object(
                status, "source1998_all_todo_target_claims", return_value=()
            ), patch.object(status, "source1998_authority_snapshot", return_value=(b"authority", task_state)), patch.object(
                status, "source1998_pane_snapshot"
            ), patch.object(status, "validate_source1998_transcript", return_value=todo_state):
                status.source1998_reconcile_final_evidence_pass(args, task, todo, (task_before, task_after, todo_before, todo_after))
