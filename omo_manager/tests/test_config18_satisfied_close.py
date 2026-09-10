from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from omo_manager.omo_config18_satisfied_close import (
    AUTHORITY_SHA256,
    CURRENT_MANAGER,
    PENDING_ITEM,
    REPLAY_ID,
    TARGET,
    UPSTREAM_AUDIT,
    UPSTREAM_SCHEMA,
    PanePin,
    close_bound_target,
    lifecycle_state,
    task_after,
    todo_after,
    validate_executor,
    validate_live,
    validate_upstream,
)
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata


class Config18SatisfiedCloseTests(TestCase):
    def test_upstream_requires_exact_committed_config16_audit(self) -> None:
        audit = {
            "schema": UPSTREAM_SCHEMA,
            "state": "committed",
            "audit": str(UPSTREAM_AUDIT),
            "task": "dw2_input_clear.md",
            "replay_id": REPLAY_ID,
            "authority_sha256": AUTHORITY_SHA256,
            "manager_target": CURRENT_MANAGER,
        }
        validate_upstream(json.dumps(audit).encode())
        audit["state"] = "prepared"
        with self.assertRaises(TaskFrontmatterError):
            validate_upstream(json.dumps(audit).encode())

    def test_after_images_discharge_only_exact_item_and_preserve_other_rows(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "config16_close.md"
            source = f"""---
version: v1.0.0
status: blocked
blocked_on: human
runat: config:18
tool: codex
managerat: wl:21
is_manager: false
pending_task_items:
  - {PENDING_ITEM!r}
---
body
"""
            todo = """current:
human pending:
config16_close.md config:18
dw2_wrap_cancel.md config:19
dw2_cleanup_input.md config:20
low priority:
previous:
"""
            task.write_text(source)
            updated_task = task_after(root, source).decode()
            updated_todo = todo_after(root, task, todo).decode()
            metadata = parse_task_metadata(updated_task, root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertIn("previous:\nconfig16_close.md config:18\n", updated_todo)
            self.assertIn("dw2_wrap_cancel.md config:19", updated_todo)
            self.assertIn("dw2_cleanup_input.md config:20", updated_todo)

    def test_live_binding_rejects_tail_or_composer_drift(self) -> None:
        pin = PanePin(TARGET, "%12", 123, 456, "01a00000-0000-7000-8000-000000000000")
        composer: dict[str, object] = {"capture_sha256": "1" * 64, "runtime": {"pane_id": "%12"}}
        with (
            patch("omo_manager.omo_config18_satisfied_close.inspect_target", return_value="stuck_input"),
            patch("omo_manager.omo_config18_satisfied_close.target_identity", return_value=("%12", 123, 456)),
            patch("omo_manager.omo_config18_satisfied_close.session_from_process", return_value=pin.session_id),
            patch("omo_manager.omo_config18_satisfied_close.exact_tail", return_value=(True, ["done", "stale"])),
            patch("omo_manager.omo_config18_satisfied_close.composer_snapshot", return_value=composer),
        ):
            tail = hashlib.sha256(b"done\nstale").hexdigest()
            self.assertEqual(tail, validate_live(pin, tail, composer)["tail_sha256"])
            with self.assertRaises(TaskFrontmatterError):
                validate_live(pin, "0" * 64, composer)
            with self.assertRaises(TaskFrontmatterError):
                validate_live(pin, tail, {"capture_sha256": "2" * 64, "runtime": composer["runtime"]})

    def test_guarded_close_rejects_identity_drift(self) -> None:
        pin = PanePin(TARGET, "%12", 123, 456, "01a00000-0000-7000-8000-000000000000")
        with (
            patch("omo_manager.omo_config18_satisfied_close.target_identity", return_value=("%12", 123, 456)),
            patch("omo_manager.omo_config18_satisfied_close.session_from_process", return_value=pin.session_id),
            patch("omo_manager.omo_config18_satisfied_close.guarded_tmux_command") as guarded,
        ):
            close_bound_target(pin)
        guarded.assert_called_once_with(TARGET, "%12", ["kill-pane", "-t", "%12"], 123)
        with (
            patch("omo_manager.omo_config18_satisfied_close.target_identity", return_value=("%13", 123, 456)),
            patch("omo_manager.omo_config18_satisfied_close.guarded_tmux_command") as guarded,
            self.assertRaises(TaskFrontmatterError),
        ):
            close_bound_target(pin)
        guarded.assert_not_called()

    def test_recovery_accepts_only_initial_todo_first_and_committed(self) -> None:
        before_task, before_todo = b"before task", b"before todo"
        after_task, after_todo = b"after task", b"after todo"
        packet: dict[str, object] = {
            "task_before_sha256": hashlib.sha256(before_task).hexdigest(),
            "todo_before_sha256": hashlib.sha256(before_todo).hexdigest(),
            "task_after_sha256": hashlib.sha256(after_task).hexdigest(),
            "todo_after_sha256": hashlib.sha256(after_todo).hexdigest(),
        }
        lifecycle_state(packet, before_task, before_todo)
        lifecycle_state(packet, before_task, after_todo)
        lifecycle_state(packet, after_task, after_todo)
        with self.assertRaises(TaskFrontmatterError):
            lifecycle_state(packet, after_task, before_todo)

    def test_executor_rejects_non_wl21_before_packet_read(self) -> None:
        from omo_manager.omo_config18_satisfied_close import execute

        with patch.dict("os.environ", {"TMUX_PANE": "%77"}), patch(
            "omo_manager.omo_config18_satisfied_close.exact_pane_id", return_value="%77"
        ):
            validate_executor()
        ns = argparse.Namespace(packet=Path("missing"), packet_sha256="0" * 64)
        with (
            patch.dict("os.environ", {"TMUX_PANE": "%76"}),
            patch("omo_manager.omo_config18_satisfied_close.exact_pane_id", return_value="%77"),
            patch("omo_manager.omo_config18_satisfied_close.read_regular") as read,
            self.assertRaises(TaskFrontmatterError),
        ):
            execute(ns)
        read.assert_not_called()
