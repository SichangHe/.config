from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from omo_manager.omo_human_worker_close import (
    CURRENT_MANAGER,
    ORIGINAL_MANAGER,
    REPLAY_ID,
    REPORT_MESSAGE_SHA256,
    TARGET,
    PanePin,
    lifecycle_state,
    task_after,
    todo_after,
    validate_authority,
    validate_live,
    validate_terminal_report,
)
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata


class HumanWorkerCloseTests(TestCase):
    def test_authority_requires_exact_two_line_source(self) -> None:
        source = b"subject\n\nYou're saying they're completed, then just send a bunch of control C and\nkill the Tmux window, no?\n"
        validate_authority(source, (3, 4))
        with self.assertRaises(TaskFrontmatterError):
            validate_authority(source, (3, 3))

    def test_after_images_close_only_current_worker(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "dw2_input_clear.md"
            task_text = """---
version: v1.0.0
status: blocked
blocked_on: config16_close.md
runat: config:16
tool: codex
managerat: wl:21
is_manager: false
pending_task_items: []
---
done
"""
            todo = """current:
dw2_input_clear.md config:16
config16_close.md config:18
human pending:
dw2_wrap_cancel.md config:19
dw2_cleanup_input.md config:20
low priority:
previous:
""".encode()
            task.write_text(task_text)
            updated_task = task_after(root, task_text, root / "manager_mail/source.txt").decode()
            updated_todo = todo_after(root, task, todo).decode()
            metadata = parse_task_metadata(updated_task, root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertIn("previous:\ndw2_input_clear.md\n", updated_todo)
            self.assertIn("config16_close.md config:18", updated_todo)
            self.assertIn("dw2_wrap_cancel.md config:19", updated_todo)
            self.assertIn("dw2_cleanup_input.md config:20", updated_todo)

    def test_terminal_replay_binds_old_manager_and_commitment(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / "agent_done.md"
            commitment = root / f"{REPLAY_ID}.commitment"
            commitment.write_text("{}")
            transfer = {
                "routing": {
                    "requested_manager_target": ORIGINAL_MANAGER,
                    "resolved_manager_target": ORIGINAL_MANAGER,
                },
                "queue_item": {"replay_id": REPLAY_ID, "pointer": str(report)},
            }
            data = (
                b"[omo-transfer: "
                + json.dumps(transfer, separators=(",", ":")).encode()
                + b"]\nmessage:\nCompletion and no-mail closure: subsequent `omo_pending.py list` returned empty output. No Human email was sent."
            )
            authenticated = {
                "message_sha256": REPORT_MESSAGE_SHA256,
                "producer_target": TARGET,
                "source_task": str(root / "dw2_input_clear.md"),
                "commitment_path": str(commitment),
            }
            with patch("omo_manager.omo_human_worker_close.authenticated_report", return_value=authenticated):
                self.assertEqual(commitment, validate_terminal_report(data, report, root))
                transfer["routing"]["resolved_manager_target"] = CURRENT_MANAGER
                changed = data.replace(
                    b'"resolved_manager_target":"wl:12"',
                    b'"resolved_manager_target":"wl:21"',
                )
                with self.assertRaises(TaskFrontmatterError):
                    validate_terminal_report(changed, report, root)

    def test_live_binding_rejects_tail_drift(self) -> None:
        pin = PanePin(TARGET, "%12", 123, 456, "01a00000-0000-7000-8000-000000000000")
        with (
            patch("omo_manager.omo_human_worker_close.inspect_target", return_value="stuck_input"),
            patch("omo_manager.omo_human_worker_close.target_identity", return_value=("%12", 123, 456)),
            patch("omo_manager.omo_human_worker_close.session_from_process", return_value=pin.session_id),
            patch("omo_manager.omo_human_worker_close.exact_tail", return_value=(True, ["done", "stale"])),
        ):
            digest = hashlib.sha256(b"done\nstale").hexdigest()
            self.assertEqual(digest, validate_live(pin))
            with self.assertRaises(TaskFrontmatterError):
                validate_live(pin, "0" * 64)

    def test_lifecycle_recovery_accepts_only_todo_first_states(self) -> None:
        before_task = b"before task"
        before_todo = b"before todo"
        after_task = b"after task"
        after_todo = b"after todo"
        packet: dict[str, object] = {
            "task_before_sha256": hashlib.sha256(before_task).hexdigest(),
            "todo_before_sha256": hashlib.sha256(before_todo).hexdigest(),
            "task_after_sha256": hashlib.sha256(after_task).hexdigest(),
            "todo_after_sha256": hashlib.sha256(after_todo).hexdigest(),
        }
        self.assertEqual(
            (packet["task_before_sha256"], packet["todo_before_sha256"]),
            lifecycle_state(packet, before_task, before_todo),
        )
        self.assertEqual(
            (packet["task_before_sha256"], packet["todo_after_sha256"]),
            lifecycle_state(packet, before_task, after_todo),
        )
        self.assertEqual(
            (packet["task_after_sha256"], packet["todo_after_sha256"]),
            lifecycle_state(packet, after_task, after_todo),
        )
        with self.assertRaises(TaskFrontmatterError):
            lifecycle_state(packet, after_task, before_todo)

    def test_prepare_scope_rejects_wrong_replay_before_pane_access(self) -> None:
        from omo_manager.omo_human_worker_close import prepare

        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "dw2_input_clear.md").write_text("x")
            (root / "TODO.md").write_text("x")
            (root / "manager_mail").mkdir()
            authority = root / "manager_mail/source.txt"
            authority.write_text("x")
            report = root / "report"
            report.write_text("x")
            manager = root / "transport_closure_mgr.md"
            manager.write_text("x")
            private = root / "private"
            private.mkdir(mode=0o700)
            ns = argparse.Namespace(
                root=root,
                task=Path("dw2_input_clear.md"),
                target=TARGET,
                task_sha256="0" * 64,
                todo_sha256="0" * 64,
                authority=authority,
                authority_sha256="0" * 64,
                authority_lines=(3, 4),
                terminal_report=report,
                terminal_report_sha256="0" * 64,
                replay_id="0" * 64,
                original_manager=ORIGINAL_MANAGER,
                manager_task=Path("transport_closure_mgr.md"),
                manager_task_sha256="0" * 64,
                manager_target=CURRENT_MANAGER,
                pane_id="%1",
                pane_pid=2,
                pane_start_ticks=3,
                session_id="01a00000-0000-7000-8000-000000000000",
                protected_target=["config:18", "config:19", "config:20"],
                destination_target=CURRENT_MANAGER,
                audit=private / "audit",
                packet=private / "packet",
            )
            with patch("omo_manager.omo_human_worker_close.validate_live") as live:
                with self.assertRaises(TaskFrontmatterError):
                    prepare(ns)
                live.assert_not_called()
