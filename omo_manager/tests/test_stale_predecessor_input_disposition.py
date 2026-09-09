from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_codex_stop, omo_stale_predecessor_input_disposition as subject
from omo_manager.omo_stale_predecessor_close import PanePin
from omo_manager.omo_task_metadata import TaskFrontmatterError

MENU_LINES = [
    "› /status",
    "",
    "  /status      show current session configuration and token usage",
    "  /statusline  configure which items appear in the status line",
]


def disposition_packet(tmp: Path) -> dict[str, object]:
    return {
        "root": str(tmp),
        "task": str(tmp / "worker.md"),
        "todo": str(tmp / "TODO.md"),
        "manager_task": str(tmp / "manager.md"),
        "manager_target": "dw:0",
        "predecessor_target": "dw8:0",
        "predecessor_pane": {"target": "dw8:0", "pane_id": "%1", "pane_pid": 101, "pane_start_ticks": 201},
        "predecessor_session_id": "01a07f0f-ffbd-7f13-89f1-4936c50be5c2",
        "protected_target": "dw8:1",
        "protected_pane": {"target": "dw8:1", "pane_id": "%2", "pane_pid": 102, "pane_start_ticks": 202},
        "protected_session_id": "01a07faa-011d-7983-86bb-978cf5a25169",
        "prior_packet_sha256": "1" * 64,
        "prepared_close_audit": str(tmp / "close.json.prepared"),
        "prepared_close_audit_sha256": "2" * 64,
        "execution_report_replay_id": "3" * 64,
        "recovery_report_replay_id": "4" * 64,
        "authorized_input": "/status",
        "input_authorization_sha256": "5" * 64,
        "menu_capture_sha256": "6" * 64,
        "binding_id": "7" * 64,
        "audit": str(tmp / "disposition.json"),
        "inputs": [],
    }


class StalePredecessorInputDispositionTests(unittest.TestCase):
    def todo_recovery_fixture(self, tmp: Path) -> tuple[dict[str, object], dict[str, object], Path]:
        task = tmp / "worker.md"
        todo = tmp / "TODO.md"
        manager = tmp / "manager.md"
        current_manager = tmp / "dw_root_new.md"
        before_task = b"---\nversion: v1.0.0\nstatus: blocked\nrunat: dw8:1\ntool: codex\nmanagerat: dw:0\nis_manager: false\npending_task_items: []\n---\nworker\n"
        after_task = before_task.replace(b"managerat: dw:0\n", b"managerat: dw:15\n")
        task.write_bytes(after_task)
        manager.write_text("manager\n")
        current_manager.write_text("---\nversion: v1.0.0\nstatus: blocked\nrunat: dw:15\ntool: codex\nmanagerat: config:1\nis_manager: true\npending_task_items: []\n---\nmanager\n")
        todo.write_bytes(b"current:\nworker.md dw8:1\nother.md config:2\n")
        packet = disposition_packet(tmp)
        packet.update(
            {
                "task_sha256": subject.sha256(after_task),
                "todo_sha256": "a" * 64,
                "helper": str(Path(subject.__file__).resolve(strict=True)),
                "helper_sha256": subject.RECOVERABLE_HELPER_SHA256,
            }
        )
        root_audit = tmp / "root-audit.json"
        root_audit.write_text(
            json.dumps(
                {
                    "state": "committed",
                    "operation": "manager-replace",
                    "old_task": "dw_manager.md",
                    "old_target": "dw:0",
                    "successor_task": "dw_root_new.md",
                    "new_target": "dw:15",
                    "parent_target": "config:1",
                    "children": [{"task": "worker.md", "sha256": subject.sha256(before_task), "queue_sha256": subject.sha256(b"[]")}],
                    "source1485_topology": {
                        "root_task": "dw_root_new.md",
                        "root_target": "dw:15.0",
                        "parent_target": "config:1.0",
                        "rows": [
                            {
                                "task": "worker.md",
                                "sha256": subject.sha256(after_task),
                                "status": "blocked",
                                "runat": "dw8:1.0",
                                "managerat": "dw:15.0",
                                "tool": "codex",
                                "is_manager": False,
                                "queue_sha256": subject.sha256(b"[]"),
                            },
                            {
                                "task": "dw_root_new.md",
                                "status": "blocked",
                                "runat": "dw:15.0",
                                "managerat": "config:1.0",
                                "tool": "codex",
                                "is_manager": True,
                            },
                        ],
                    },
                    "files": [
                        {
                            "task": "worker.md",
                            "before": base64.b64encode(before_task).decode(),
                            "after": base64.b64encode(after_task).decode(),
                        }
                    ],
                }
            )
        )
        root_audit.chmod(0o600)
        self.enterContext(patch.object(subject, "SOURCE1485_ROOT_AUDIT_SHA256", subject.sha256(root_audit.read_bytes())))
        self.enterContext(patch.object(subject, "SOURCE1485_ORIGINAL_TASK_SHA256", subject.sha256(before_task)))
        row, chunks = subject.partition_todo(todo.read_bytes(), subject.owned_todo_row(packet))
        unsigned: dict[str, object] = {
            "schema": subject.TODO_RECOVERY_SCHEMA,
            "operation": subject.TODO_RECOVERY_OPERATION,
            "packet": str(tmp / "packet.json"),
            "packet_sha256": subject.RECOVERABLE_PACKET_SHA256,
            "review_report": str(tmp / "review.json"),
            "review_report_sha256": subject.RECOVERABLE_REVIEW_SHA256,
            "prepared_audit": str(tmp / "disposition.json.prepared"),
            "prepared_audit_sha256": subject.RECOVERABLE_PREPARED_AUDIT_SHA256,
            "task": str(task),
            "original_task_sha256": packet["task_sha256"],
            "current_task_input": subject.file_input(task, "rebound protected task"),
            "todo": str(todo),
            "original_todo_sha256": packet["todo_sha256"],
            "owned_rows_base64": [base64.b64encode(row).decode()],
            "unrelated_todo_base64": base64.b64encode(b"".join(chunks)).decode(),
            "unrelated_todo_sha256": subject.sha256(b"".join(chunks)),
            "current_todo_input": subject.file_input(todo, "rebound TODO"),
            "recovery_helper_input": subject.file_input(Path(subject.__file__).resolve(strict=True), "TODO recovery helper"),
            "source1485_root_audit": str(root_audit),
            "source1485_root_audit_sha256": subject.SOURCE1485_ROOT_AUDIT_SHA256,
            "current_manager_task": str(current_manager),
            "current_manager_target": "dw:15",
        }
        return packet, {**unsigned, "binding_id": subject.bound_receipt_id(unsigned)}, todo

    def execute_states(
        self,
        states: list[str],
        events: list[str],
        *,
        prepared_exists: bool,
        recoverable: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet = disposition_packet(tmp)
            prepared_close = Path(str(packet["prepared_close_audit"]))
            prepared_close.write_bytes(b"preserved-close-audit\n")
            predecessor = PanePin("dw8:0", "%1", 101, 201)
            protected = PanePin("dw8:1", "%2", 102, 202)

            def live_state(
                _packet: dict[str, object],
                *,
                require_original_menu: bool,
                rebind_recoverable_helper: bool = False,
                todo_recovery: dict[str, object] | None = None,
            ) -> tuple[PanePin, PanePin, str, bytes]:
                self.assertEqual(recoverable, rebind_recoverable_helper)
                self.assertIsNone(todo_recovery)
                state = states.pop(0)
                events.append(f"state:{state}:{require_original_menu}")
                return predecessor, protected, state, state.encode()

            def publish(path: Path, _data: bytes, label: str) -> None:
                events.append(f"publish:{label}:{path}")

            def hold_inputs(
                _packet: dict[str, object],
                _stack: contextlib.ExitStack,
                *,
                rebind_recoverable_helper: bool = False,
                todo_recovery: dict[str, object] | None = None,
            ) -> list[object]:
                self.assertEqual(recoverable, rebind_recoverable_helper)
                self.assertIsNone(todo_recovery)
                return []

            def send_key(
                _pin: PanePin,
                command: list[str],
                *,
                validate_before: Callable[[], tuple[str, bytes]],
                validate_after: Callable[[], tuple[str, bytes]],
            ) -> tuple[str, bytes]:
                _state, capture = validate_before()
                events.append(f"key:{command[-1]}:{capture.decode()}")
                return validate_after()

            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )
            with (
                patch.object(subject, "read_bound", return_value=b"packet"),
                patch.object(subject, "validate_packet", return_value=packet),
                patch.object(subject, "validate_review"),
                patch.object(subject, "tmux_input_lock", side_effect=lambda _target: contextlib.nullcontext()),
                patch.object(subject, "task_target_lock", side_effect=lambda _root, _target: contextlib.nullcontext()),
                patch.object(subject, "task_file_lock", side_effect=lambda _path: contextlib.nullcontext()),
                patch.object(subject, "authorize_prepared_helper_recovery", return_value=recoverable),
                patch.object(subject, "hold_inputs", side_effect=hold_inputs),
                patch.object(subject, "path_entry_exists", side_effect=[prepared_exists, False]),
                patch.object(subject, "live_state", side_effect=live_state),
                patch.object(subject, "publish_or_validate", side_effect=publish),
                patch.object(subject, "guarded_tmux_command_for_capture", side_effect=send_key),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                subject.execute(args)
            self.assertEqual(b"preserved-close-audit\n", prepared_close.read_bytes())

    def test_exact_incomplete_status_menu_is_recognized(self) -> None:
        lines = ["older output", *MENU_LINES, "", ""]
        self.assertTrue(subject.exact_status_menu(lines))
        self.assertEqual("status_menu", subject.exact_recovery_state(lines))

    def test_menu_recognizer_rejects_any_expansion_or_drift(self) -> None:
        cases = (
            ["› /status now", *MENU_LINES[1:]],
            [MENU_LINES[0], "  /status      show current session configuration and token usage", MENU_LINES[-1]],
            [*MENU_LINES, "  /statusline extra"],
            [MENU_LINES[0], "", MENU_LINES[-1], MENU_LINES[-2]],
            [*MENU_LINES, "", "  gpt-5.5"],
        )
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertFalse(subject.exact_status_menu(lines))
                self.assertEqual("other", subject.exact_recovery_state(lines))

    def test_bare_incomplete_status_input_is_not_authorized(self) -> None:
        self.assertEqual("other", subject.exact_recovery_state(["› /status"]))
        self.assertEqual("status_input", subject.exact_recovery_state(["› /status", "", "  gpt-5.5"]))
        self.assertEqual("ready", subject.exact_recovery_state(["› Ask Codex to do anything", "", "  gpt-5.5"]))

    def test_post_escape_ready_capture_accepts_exact_tmux_n_footer_spacer(self) -> None:
        lines = [
            "│  Warning:                     limits may be stale - run /status again shortly │",
            "╰──────────────────────────────────────────────────────────────────────────────╯",
            " ",
            " ",
            "› Ask Codex to do anything",
            " ",
            "  gpt-5.6-sol high · /workspace/dw8 · weekly 66% left · 6.82M used · …",
        ]
        report = subject.report_from_lines(lines)
        self.assertEqual(("ready", "Ask Codex to do anything"), (report.status, report.input_text))
        self.assertEqual("ready", subject.exact_recovery_state(lines))

    def test_post_escape_ready_capture_rejects_nonexact_footer_spacers(self) -> None:
        base = [
            "› Ask Codex to do anything",
            " ",
            "  gpt-5.6-sol high · /workspace/dw8 · weekly 66% left · 6.82M used · …",
        ]
        for spacer in ("  ", "\t", " unexpected"):
            with self.subTest(spacer=repr(spacer)):
                lines = [base[0], spacer, base[2]]
                self.assertEqual("other", subject.exact_recovery_state(lines))

    def test_recovery_report_requirements_match_immutable_incident_wording(self) -> None:
        report_body = (
            "Supplemental dw8:0 recovery result consumed without a duplicate queue item. "
            "The supported exact `/status` cancellation dry-run validated, but live "
            "`--cancel-existing-file` failed before mutation with `target input is not in a "
            "complete Codex view`; fresh supported status remains `running` with the menu "
            "visible. This supplies no safe-stop evidence. I will not retry, force keys, "
            "regenerate a packet, or touch dw8:0/dw8:1 until supported status is stably ready "
            "or a narrow independently reviewed provenance-safe disposition is released. "
            "Both panes and the prepared audit remain preserved.\n"
        )
        required_text = (
            "dw8:0",
            *subject.RECOVERY_REPORT_REQUIRED_TEXT,
            "dw8:1",
        )
        self.assertTrue(all(token in report_body for token in required_text))
        self.assertNotIn("not safe-stop evidence", report_body)

    def test_todo_recovery_authenticates_owned_row_and_all_unrelated_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            current, _task, _manager, _target, _parent = subject.validate_todo_recovery_current(packet, recovery)
            self.assertEqual(b"current:\nworker.md dw8:1\nother.md config:2\n", current)

    def test_fresh_packet_authenticates_source1485_migration_custody(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            prior = {
                "task": packet["task"],
                "task_sha256": subject.SOURCE1485_ORIGINAL_TASK_SHA256,
                "manager_task": packet["manager_task"],
                "manager_target": packet["manager_target"],
            }
            task = Path(str(packet["task"]))
            fresh = {
                **packet,
                "schema": subject.SOURCE1485_SCHEMA,
                "task_sha256": subject.sha256(task.read_bytes()),
                "original_task_sha256": subject.SOURCE1485_ORIGINAL_TASK_SHA256,
                "manager_task": recovery["current_manager_task"],
                "manager_target": recovery["current_manager_target"],
                "original_manager_task": packet["manager_task"],
                "original_manager_target": packet["manager_target"],
                "source1485_root_audit": recovery["source1485_root_audit"],
                "source1485_root_audit_sha256": recovery["source1485_root_audit_sha256"],
            }
            task_data, manager, target, parent = subject.validate_source1485_packet(fresh, prior)
            self.assertEqual(task.read_bytes(), task_data)
            self.assertEqual(Path(str(recovery["current_manager_task"])), manager)
            self.assertEqual(("dw:15", "config:1"), (target, parent))

    def test_fresh_packet_rejects_stale_pre_source1485_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            prior = {
                "task": packet["task"],
                "task_sha256": subject.SOURCE1485_ORIGINAL_TASK_SHA256,
                "manager_task": packet["manager_task"],
                "manager_target": packet["manager_target"],
            }
            fresh = {
                **packet,
                "schema": subject.SOURCE1485_SCHEMA,
                "task_sha256": subject.sha256(Path(str(packet["task"])).read_bytes()),
                "original_task_sha256": "f" * 64,
                "manager_task": recovery["current_manager_task"],
                "manager_target": recovery["current_manager_target"],
                "original_manager_task": packet["manager_task"],
                "original_manager_target": packet["manager_target"],
                "source1485_root_audit": recovery["source1485_root_audit"],
                "source1485_root_audit_sha256": recovery["source1485_root_audit_sha256"],
            }
            with self.assertRaisesRegex(TaskFrontmatterError, "pre-Source-1485 custody"):
                subject.validate_source1485_packet(fresh, prior)

    def test_fresh_prepare_rejects_capture_race(self) -> None:
        predecessor = PanePin("dw8:0", "%1", 101, 201)
        protected = PanePin("dw8:1", "%2", 102, 202)
        ready = b"\xe2\x80\xba Ask Codex to do anything\n\n  gpt-5.5\n"
        menu = "\n".join(MENU_LINES).encode()
        with (
            patch.object(subject, "capture_pinned", side_effect=[ready, menu, menu + b"changed\n"]),
            self.assertRaisesRegex(TaskFrontmatterError, "capture raced"),
        ):
            subject.capture_fresh_preparation_menu(predecessor, protected)

    def test_fresh_prepare_rejects_nonready_or_changed_protected_successor(self) -> None:
        predecessor = PanePin("dw8:0", "%1", 101, 201)
        protected = PanePin("dw8:1", "%2", 102, 202)
        ready = b"\xe2\x80\xba Ask Codex to do anything\n\n  gpt-5.5\n"
        busy = b"\xe2\x80\xba /status\n"
        menu = "\n".join(MENU_LINES).encode()
        with (
            patch.object(subject, "capture_pinned", return_value=busy),
            self.assertRaisesRegex(TaskFrontmatterError, "not in its preserved ready state"),
        ):
            subject.capture_fresh_preparation_menu(predecessor, protected)
        with (
            patch.object(subject, "capture_pinned", side_effect=[ready, menu, menu, busy]),
            self.assertRaisesRegex(TaskFrontmatterError, "successor changed"),
        ):
            subject.capture_fresh_preparation_menu(predecessor, protected)

    def test_v2_review_and_execute_state_reject_protected_successor_drift(self) -> None:
        packet = disposition_packet(Path("/tmp"))
        packet.update(
            {
                "schema": subject.SOURCE1485_SCHEMA,
                "authorized_input": "/status",
                "menu_capture_base64": base64.b64encode("\n".join(MENU_LINES).encode()).decode(),
                "menu_capture_sha256": subject.sha256("\n".join(MENU_LINES).encode()),
            }
        )
        predecessor = PanePin("dw8:0", "%1", 101, 201)
        protected = PanePin("dw8:1", "%2", 102, 202)
        menu = "\n".join(MENU_LINES).encode()
        busy = b"\xe2\x80\xba /status\n"
        with (
            patch.object(subject, "static_evidence", return_value=(predecessor, protected)),
            patch.object(subject, "current_pin", return_value=True),
            patch.object(
                subject,
                "session_from_process",
                side_effect=[packet["predecessor_session_id"], packet["protected_session_id"]],
            ),
            patch.object(subject, "capture_pinned", side_effect=[menu, busy]),
            self.assertRaisesRegex(TaskFrontmatterError, "left its preserved ready state"),
        ):
            subject.live_state(packet, require_original_menu=True)

    def test_todo_recovery_rejects_owned_row_drift_even_with_fresh_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, todo = self.todo_recovery_fixture(Path(directory))
            todo.write_bytes(b"current:\nworker.md dw8:2\nother.md config:2\n")
            recovery["current_todo_input"] = subject.file_input(todo, "rebound TODO")
            with self.assertRaisesRegex(TaskFrontmatterError, "one exact transaction-owned row"):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_unrelated_row_drift_even_with_fresh_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, todo = self.todo_recovery_fixture(Path(directory))
            todo.write_bytes(b"current:\nworker.md dw8:1\nother.md config:3\n")
            recovery["current_todo_input"] = subject.file_input(todo, "rebound TODO")
            with self.assertRaisesRegex(TaskFrontmatterError, "unrelated bytes changed"):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_read_to_identity_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, todo = self.todo_recovery_fixture(Path(directory))
            original_read_bound = subject.read_bound

            def raced_read(path: Path, expected_sha256: str, label: str, *, private: bool = False) -> bytes:
                data = original_read_bound(path, expected_sha256, label, private=private)
                todo.write_bytes(data + b"raced.md config:7\n")
                return data

            with (
                patch.object(subject, "read_bound", side_effect=raced_read),
                self.assertRaisesRegex(TaskFrontmatterError, "current TODO recovery input changed"),
            ):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_original_child_before_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            audit_path = Path(str(recovery["source1485_root_audit"]))
            audit = json.loads(audit_path.read_bytes())
            audit["children"][0]["sha256"] = "f" * 64
            audit_path.write_text(json.dumps(audit))
            recovery["source1485_root_audit_sha256"] = subject.sha256(audit_path.read_bytes())
            self.enterContext(patch.object(subject, "SOURCE1485_ROOT_AUDIT_SHA256", recovery["source1485_root_audit_sha256"]))
            with self.assertRaisesRegex(TaskFrontmatterError, "changed unsupported bytes"):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_migrated_packet_after_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            packet["task_sha256"] = "f" * 64
            recovery["original_task_sha256"] = packet["task_sha256"]
            with self.assertRaisesRegex(TaskFrontmatterError, "changed unsupported bytes"):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_protected_task_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            task = Path(str(packet["task"]))
            task.write_bytes(task.read_bytes().replace(b"status: blocked\n", b"status: running\n"))
            recovery["current_task_input"] = subject.file_input(task, "rebound protected task")
            with self.assertRaisesRegex(TaskFrontmatterError, "transition evidence is invalid"):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_rejects_task_read_to_identity_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet, recovery, _todo = self.todo_recovery_fixture(Path(directory))
            task = Path(str(packet["task"]))
            original_read_bound = subject.read_bound

            def raced_read(path: Path, expected_sha256: str, label: str, *, private: bool = False) -> bytes:
                data = original_read_bound(path, expected_sha256, label, private=private)
                if label == "rebound protected task":
                    task.write_bytes(data + b"raced\n")
                return data

            with (
                patch.object(subject, "read_bound", side_effect=raced_read),
                self.assertRaisesRegex(TaskFrontmatterError, "current task recovery input changed"),
            ):
                subject.validate_todo_recovery_current(packet, recovery)

    def test_todo_recovery_holds_current_task_and_detects_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, recovery, todo = self.todo_recovery_fixture(tmp)
            task = Path(str(packet["task"]))
            manager = Path(str(packet["manager_task"]))
            helper = Path(subject.__file__).resolve(strict=True)

            def old_input(path: Path, digest: str, label: str) -> dict[str, object]:
                value = subject.file_input(path, label)
                identity = subject.object_map(value["file"], label)
                identity["sha256"] = digest
                value["file"] = identity
                return value

            packet.update(
                {
                    "inputs": [
                        old_input(task, str(packet["task_sha256"]), "old task"),
                        old_input(todo, str(packet["todo_sha256"]), "old TODO"),
                        old_input(manager, "b" * 64, "old manager"),
                        old_input(helper, subject.RECOVERABLE_HELPER_SHA256, "old helper"),
                    ],
                    "manager_task_sha256": "b" * 64,
                }
            )
            with contextlib.ExitStack() as stack, patch.object(subject, "is_recoverable_prepared_packet", return_value=True):
                held = subject.hold_inputs(packet, stack, rebind_recoverable_helper=True, todo_recovery=recovery)
                self.assertEqual(subject.sha256(task.read_bytes()), held[0].identity.sha256)
                task.write_bytes(task.read_bytes() + b"raced\n")
                with self.assertRaises(subject.CustodyError):
                    subject.validate_held_absolute(held[0])

    def test_todo_recovery_accepts_changed_manager_bytes_only_with_valid_semantic_custody(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, _recovery, todo = self.todo_recovery_fixture(tmp)
            task = Path(str(packet["task"]))
            manager = Path(str(packet["manager_task"]))
            task.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: dw8:1\ntool: codex\nmanagerat: dw:0\nis_manager: false\npending_task_items: []\n---\n")
            manager.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: dw:0\ntool: codex\nmanagerat: config:1\nis_manager: true\npending_task_items: []\n---\nchanged after preparation\n")
            packet.update(
                {
                    "task_sha256": subject.sha256(task.read_bytes()),
                    "manager_task_sha256": "0" * 64,
                }
            )

            def active(_root: Path, target: str) -> tuple[Path, ...]:
                return (task,) if target == "dw8:1" else ()

            with patch.object(subject, "authoritative_active_target_task_paths", side_effect=active):
                subject.validate_lifecycle(
                    packet,
                    rebound_todo_data=todo.read_bytes(),
                    rebound_task_data=task.read_bytes(),
                    current_manager_task=manager,
                    current_manager_target="dw:0",
                    current_manager_parent="config:1",
                )
                manager.write_text(manager.read_text().replace("status: running", "status: done"))
                with self.assertRaisesRegex(TaskFrontmatterError, "lifecycle custody is invalid"):
                    subject.validate_lifecycle(
                        packet,
                        rebound_todo_data=todo.read_bytes(),
                        rebound_task_data=task.read_bytes(),
                        current_manager_task=manager,
                        current_manager_target="dw:0",
                        current_manager_parent="config:1",
                    )

    def test_todo_recovery_rejects_reparented_or_mistooled_successor_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, recovery, todo = self.todo_recovery_fixture(tmp)
            task = Path(str(packet["task"]))
            manager = Path(str(recovery["current_manager_task"]))
            task.write_text(task.read_text().replace("status: blocked", "status: running"))
            manager.write_text(manager.read_text().replace("status: blocked", "status: running"))

            def active(_root: Path, target: str) -> tuple[Path, ...]:
                return (task,) if target == "dw8:1" else ()

            with patch.object(subject, "authoritative_active_target_task_paths", side_effect=active):
                manager.write_text(manager.read_text().replace("managerat: config:1", "managerat: config:2"))
                with self.assertRaisesRegex(TaskFrontmatterError, "lifecycle custody is invalid"):
                    subject.validate_lifecycle(
                        packet,
                        rebound_todo_data=todo.read_bytes(),
                        rebound_task_data=task.read_bytes(),
                        current_manager_task=manager,
                        current_manager_target="dw:15",
                        current_manager_parent="config:1",
                    )
                manager.write_text(manager.read_text().replace("managerat: config:2", "managerat: config:1").replace("tool: codex", "tool: omnigent"))
                with self.assertRaisesRegex(TaskFrontmatterError, "lifecycle custody is invalid"):
                    subject.validate_lifecycle(
                        packet,
                        rebound_todo_data=todo.read_bytes(),
                        rebound_task_data=task.read_bytes(),
                        current_manager_task=manager,
                        current_manager_target="dw:15",
                        current_manager_parent="config:1",
                    )

    def test_todo_recovery_holds_current_manager_and_detects_race_before_key_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, recovery, todo = self.todo_recovery_fixture(tmp)
            manager = Path(str(packet["manager_task"]))
            task = Path(str(packet["task"]))
            helper = Path(subject.__file__).resolve(strict=True)
            old_todo = subject.file_input(todo, "old TODO")
            old_todo_file = subject.object_map(old_todo["file"], "old TODO")
            old_todo_file["sha256"] = "a" * 64
            old_todo["file"] = old_todo_file
            old_manager = subject.file_input(manager, "old manager")
            old_manager_file = subject.object_map(old_manager["file"], "old manager")
            old_manager_file["sha256"] = "b" * 64
            old_manager["file"] = old_manager_file
            old_task = subject.file_input(task, "old task")
            old_task_file = subject.object_map(old_task["file"], "old task")
            old_task_file["sha256"] = packet["task_sha256"]
            old_task["file"] = old_task_file
            old_helper = subject.file_input(helper, "old helper")
            old_helper_file = subject.object_map(old_helper["file"], "old helper")
            old_helper_file["sha256"] = subject.RECOVERABLE_HELPER_SHA256
            old_helper["file"] = old_helper_file
            packet.update(
                {
                    "todo_sha256": "a" * 64,
                    "manager_task_sha256": "b" * 64,
                    "helper": str(helper),
                    "inputs": [old_task, old_todo, old_manager, old_helper],
                }
            )
            with contextlib.ExitStack() as stack, patch.object(subject, "is_recoverable_prepared_packet", return_value=True):
                held = subject.hold_inputs(packet, stack, rebind_recoverable_helper=True, todo_recovery=recovery)
                current_manager = Path(str(recovery["current_manager_task"]))
                self.assertEqual(subject.sha256(current_manager.read_bytes()), held[2].identity.sha256)
                current_manager.write_text("raced invalid manager\n")
                with self.assertRaises(subject.CustodyError):
                    subject.validate_held_absolute(held[2])

    def test_prepare_todo_recovery_is_idempotent_and_rolls_back_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, _recovery, _todo = self.todo_recovery_fixture(tmp)
            packet.update({"audit": str(tmp / "disposition.json")})
            packet_path = tmp / "packet.json"
            review_path = tmp / "review.json"
            prepared_path = tmp / "disposition.json.prepared"
            close_prepared_path = Path(str(packet["prepared_close_audit"]))
            for path in (packet_path, review_path, prepared_path, close_prepared_path):
                path.write_text("immutable\n")
            output = tmp / "todo-recovery.json"
            args = argparse.Namespace(
                packet=packet_path,
                packet_sha256=subject.RECOVERABLE_PACKET_SHA256,
                review_report=review_path,
                review_report_sha256=subject.RECOVERABLE_REVIEW_SHA256,
                todo_recovery_output=output,
                source1485_root_audit=Path(str(_recovery["source1485_root_audit"])),
                source1485_root_audit_sha256=subject.SOURCE1485_ROOT_AUDIT_SHA256,
            )
            with (
                patch.object(subject, "load_exact_failed_disposition", return_value=(packet, packet_path, review_path, prepared_path)),
                patch.object(subject, "validate_todo_recovery_packet"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                subject.prepare_todo_recovery(args)
                first = output.read_bytes()
                subject.prepare_todo_recovery(args)
                self.assertEqual(first, output.read_bytes())
            rollback_output = tmp / "rollback.json"
            args.todo_recovery_output = rollback_output
            with (
                patch.object(subject, "load_exact_failed_disposition", return_value=(packet, packet_path, review_path, prepared_path)),
                patch.object(subject, "validate_todo_recovery_packet"),
                patch.object(subject, "validate_held_absolute", side_effect=TaskFrontmatterError("race")),
                self.assertRaisesRegex(TaskFrontmatterError, "race"),
            ):
                subject.prepare_todo_recovery(args)
            self.assertFalse(rollback_output.exists())

    def test_latest_failed_disposition_is_the_only_new_recovery_eligibility(self) -> None:
        latest = subject.recoverable_incident(subject.LATEST_RECOVERABLE_PACKET_SHA256)
        self.assertEqual(
            (
                subject.LATEST_RECOVERABLE_REVIEW_SHA256,
                subject.LATEST_RECOVERABLE_PREPARED_AUDIT_SHA256,
                subject.LATEST_RECOVERABLE_HELPER_SHA256,
            ),
            latest,
        )
        self.assertIsNone(subject.recoverable_incident("0" * 64))
        packet = {"helper_sha256": subject.LATEST_RECOVERABLE_HELPER_SHA256}
        with (
            patch.object(subject, "packet_bytes", return_value=b"latest packet"),
            patch.object(subject, "sha256", return_value=subject.LATEST_RECOVERABLE_PACKET_SHA256),
        ):
            self.assertTrue(subject.is_recoverable_prepared_packet(packet))
            packet["helper_sha256"] = subject.RECOVERABLE_HELPER_SHA256
            self.assertFalse(subject.is_recoverable_prepared_packet(packet))

    def test_latest_prepared_helper_recovery_rejects_review_and_audit_drift(self) -> None:
        packet: dict[str, object] = {}
        prepared = b"latest prepared audit\n"
        prepared_path = Path("/tmp/latest-prepared-audit")
        with (
            patch.object(subject, "is_recoverable_prepared_packet", return_value=True),
            patch.object(subject, "sha256", return_value=subject.LATEST_RECOVERABLE_PREPARED_AUDIT_SHA256),
            patch.object(subject, "read_bound", return_value=prepared),
        ):
            self.assertTrue(
                subject.authorize_prepared_helper_recovery(
                    packet,
                    subject.LATEST_RECOVERABLE_PACKET_SHA256,
                    subject.LATEST_RECOVERABLE_REVIEW_SHA256,
                    prepared_path,
                    prepared,
                )
            )
            self.assertFalse(
                subject.authorize_prepared_helper_recovery(
                    packet,
                    subject.LATEST_RECOVERABLE_PACKET_SHA256,
                    subject.RECOVERABLE_REVIEW_SHA256,
                    prepared_path,
                    prepared,
                )
            )
        with patch.object(subject, "is_recoverable_prepared_packet", return_value=True):
            self.assertFalse(
                subject.authorize_prepared_helper_recovery(
                    packet,
                    subject.LATEST_RECOVERABLE_PACKET_SHA256,
                    subject.LATEST_RECOVERABLE_REVIEW_SHA256,
                    prepared_path,
                    b"drifted audit\n",
                )
            )

    def test_prepare_todo_recovery_reserves_every_absent_prior_close_control_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, _recovery, _todo = self.todo_recovery_fixture(tmp)
            packet.update({"audit": str(tmp / "disposition.json"), "inputs": []})
            packet_path = tmp / "packet.json"
            review_path = tmp / "review.json"
            prepared_path = tmp / "disposition.json.prepared"
            prior_prepared = Path(str(packet["prepared_close_audit"]))
            for path in (packet_path, review_path, prepared_path, prior_prepared):
                path.write_text("immutable\n")
            prior_complete = Path(str(prior_prepared)[: -len(".prepared")])
            absent_controls = subject.prior_close_control_paths(prior_complete, prior_prepared) - {prior_prepared}
            for output in absent_controls:
                with self.subTest(output=output):
                    args = argparse.Namespace(
                        packet=packet_path,
                        packet_sha256=subject.RECOVERABLE_PACKET_SHA256,
                        review_report=review_path,
                        review_report_sha256=subject.RECOVERABLE_REVIEW_SHA256,
                        todo_recovery_output=output,
                        source1485_root_audit=Path(str(_recovery["source1485_root_audit"])),
                        source1485_root_audit_sha256=subject.SOURCE1485_ROOT_AUDIT_SHA256,
                    )
                    with (
                        patch.object(subject, "load_exact_failed_disposition", return_value=(packet, packet_path, review_path, prepared_path)),
                        self.assertRaisesRegex(TaskFrontmatterError, "overlaps immutable or lifecycle evidence"),
                    ):
                        subject.prepare_todo_recovery(args)
                    self.assertFalse(output.exists())

    def test_execute_dismisses_menu_then_cancels_only_status_input(self) -> None:
        events: list[str] = []
        self.execute_states(["status_menu", "status_menu", "status_input", "status_input", "ready", "ready"], events, prepared_exists=False)
        self.assertEqual(
            ["key:Escape:status_menu", "key:C-c:status_input"],
            [event for event in events if event.startswith("key:")],
        )
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )
        prepared_index = next(index for index, event in enumerate(events) if event.startswith("publish:prepared"))
        escape_index = next(index for index, event in enumerate(events) if event.startswith("key:Escape:"))
        self.assertLess(prepared_index, escape_index)

    def test_prepared_recovery_completes_from_ready_without_keys(self) -> None:
        events: list[str] = []
        self.execute_states(["ready", "ready"], events, prepared_exists=True)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )

    def test_exact_prepared_recovery_rebinds_helper_and_completes_without_keys(self) -> None:
        events: list[str] = []
        self.execute_states(["ready", "ready"], events, prepared_exists=True, recoverable=True)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )

    def test_prepared_helper_recovery_requires_all_exact_incident_hashes(self) -> None:
        packet: dict[str, object] = {}
        prepared = b"exact prepared audit\n"
        prepared_path = Path("/tmp/exact-prepared-audit")
        prepared_sha256 = subject.sha256(prepared)
        with (
            patch.object(subject, "is_recoverable_prepared_packet", return_value=True),
            patch.object(subject, "RECOVERABLE_PREPARED_AUDIT_SHA256", prepared_sha256),
            patch.object(subject, "read_bound", return_value=prepared) as read,
        ):
            self.assertTrue(
                subject.authorize_prepared_helper_recovery(
                    packet,
                    subject.RECOVERABLE_PACKET_SHA256,
                    subject.RECOVERABLE_REVIEW_SHA256,
                    prepared_path,
                    prepared,
                )
            )
            read.assert_called_once_with(
                prepared_path,
                prepared_sha256,
                "recoverable prepared disposition audit",
                private=True,
            )
            for packet_sha256, review_sha256 in (
                ("0" * 64, subject.RECOVERABLE_REVIEW_SHA256),
                (subject.RECOVERABLE_PACKET_SHA256, "0" * 64),
            ):
                with self.subTest(packet_sha256=packet_sha256, review_sha256=review_sha256):
                    self.assertFalse(
                        subject.authorize_prepared_helper_recovery(
                            packet,
                            packet_sha256,
                            review_sha256,
                            prepared_path,
                            prepared,
                        )
                    )

    def test_prepared_helper_recovery_holds_current_helper_in_place_of_old_binding(self) -> None:
        helper = Path(subject.__file__).resolve(strict=True)
        old_helper_input = subject.file_input(helper, "input disposition helper")
        old_helper_file = subject.object_map(old_helper_input["file"], "old helper")
        old_helper_file["sha256"] = subject.RECOVERABLE_HELPER_SHA256
        old_helper_input["file"] = old_helper_file
        packet: dict[str, object] = {
            "helper": str(helper),
            "helper_sha256": subject.RECOVERABLE_HELPER_SHA256,
            "inputs": [old_helper_input],
        }
        with (
            contextlib.ExitStack() as stack,
            patch.object(subject, "is_recoverable_prepared_packet", return_value=True),
        ):
            held = subject.hold_inputs(packet, stack, rebind_recoverable_helper=True)
            self.assertEqual(1, len(held))
            subject.validate_held_absolute(held[0])
            self.assertEqual(subject.sha256(helper.read_bytes()), held[0].identity.sha256)
            self.assertNotEqual(subject.RECOVERABLE_HELPER_SHA256, held[0].identity.sha256)

    def test_menu_race_before_escape_fails_without_key(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_menu", "other"], events, prepared_exists=False)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_state_race_after_escape_never_sends_control_c(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_menu", "status_menu", "other"], events, prepared_exists=False)
        self.assertEqual(["key:Escape:status_menu"], [event for event in events if event.startswith("key:")])
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_post_escape_ready_capture_race_never_publishes_complete(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_menu", "status_menu", "ready", "other"], events, prepared_exists=True)
        self.assertEqual(["key:Escape:status_menu"], [event for event in events if event.startswith("key:")])
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_ready_race_after_cancel_never_publishes_complete(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_input", "status_input", "ready", "status_input"], events, prepared_exists=True)
        self.assertEqual(["key:C-c:status_input"], [event for event in events if event.startswith("key:")])
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_post_action_binding_failure_prevents_a_second_key_and_complete(self) -> None:
        events: list[str] = []

        def changed_session(
            _packet: dict[str, object],
            *,
            require_original_menu: bool,
            rebind_recoverable_helper: bool = False,
            todo_recovery: dict[str, object] | None = None,
        ) -> tuple[PanePin, PanePin, str, bytes]:
            self.assertFalse(rebind_recoverable_helper)
            self.assertIsNone(todo_recovery)
            calls = sum(event.startswith("state:") for event in events)
            events.append(f"state:{calls}:{require_original_menu}")
            if calls == 2:
                raise TaskFrontmatterError("predecessor or protected Codex session changed.")
            state = "status_menu"
            return PanePin("dw8:0", "%1", 101, 201), PanePin("dw8:1", "%2", 102, 202), state, state.encode()

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet = disposition_packet(tmp)
            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )

            def send_key(
                _pin: PanePin,
                command: list[str],
                *,
                validate_before: Callable[[], tuple[str, bytes]],
                validate_after: Callable[[], tuple[str, bytes]],
            ) -> tuple[str, bytes]:
                _state, capture = validate_before()
                events.append(f"key:{command[-1]}:{capture.decode()}")
                return validate_after()

            with (
                patch.object(subject, "read_bound", return_value=b"packet"),
                patch.object(subject, "validate_packet", return_value=packet),
                patch.object(subject, "validate_review"),
                patch.object(subject, "tmux_input_lock", side_effect=lambda _target: contextlib.nullcontext()),
                patch.object(subject, "task_target_lock", side_effect=lambda _root, _target: contextlib.nullcontext()),
                patch.object(subject, "task_file_lock", side_effect=lambda _path: contextlib.nullcontext()),
                patch.object(subject, "hold_inputs", return_value=[]),
                patch.object(subject, "path_entry_exists", side_effect=[False, False]),
                patch.object(subject, "live_state", side_effect=changed_session),
                patch.object(subject, "publish_or_validate"),
                patch.object(subject, "guarded_tmux_command_for_capture", side_effect=send_key),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(TaskFrontmatterError, "session changed"),
            ):
                subject.execute(args)
        self.assertEqual(["key:Escape:status_menu"], [event for event in events if event.startswith("key:")])

    def test_capture_guard_compares_exact_fresh_bytes_in_one_tmux_queue(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        capture = "older output\n› /status\n\n  /status      show current session configuration and token usage\n  /statusline  configure which items appear in the status line\n".encode()
        observed: list[list[str]] = []

        def guarded_sequence(
            target: str,
            pane_id: str,
            commands: list[list[str]],
            pane_pid: int,
        ) -> str:
            self.assertEqual((pin.target, pin.pane_id, pin.pane_pid), (target, pane_id, pane_pid))
            observed.extend(commands)
            return f"OMO_DISPOSITION_CAPTURE_ACCEPTED_{'a' * 32}\n"

        with (
            patch.object(subject.secrets, "token_hex", return_value="a" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "", "", "", capture.decode() + "\n"],
            ),
            patch.object(
                subject,
                "tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "", ""),
            ),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(subject, "guarded_tmux_sequence", side_effect=guarded_sequence),
        ):
            state, after_capture = subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", capture),
                validate_after=lambda: ("status_input", b"after"),
            )
        self.assertEqual(("status_input", b"after"), (state, after_capture))

        option = f"@omo-disposition-capture-{'a' * 32}"
        self.assertEqual("if-shell", observed[0][0])
        self.assertEqual(["-F", "-t", pin.pane_id], observed[0][1:4])
        self.assertIn("PANE_IDENTITY", observed[0][4])
        self.assertIn("#{==:#{pane_dead},0}", observed[0][4])
        self.assertIn("#{==:#{pane_current_command},bunx}", observed[0][4])
        self.assertIn("#{==:#{buffer-limit},50}", observed[0][4])
        self.assertNotIn(capture.decode(), observed[0][5])
        self.assertIn(f"set-option -g buffer-limit {subject.TEMPORARY_TMUX_BUFFER_LIMIT}", observed[0][5])
        self.assertIn("capture-pane -J -N -t %1", observed[0][5])
        self.assertIn("set-option -g buffer-limit 50", observed[0][5])
        self.assertIn(f"#{{==:#{{buffer_full}},#{{{option}}}}}", observed[0][5])
        self.assertIn("send-keys -t %1 Escape", observed[0][5])
        self.assertNotIn("tmux -S", observed[0][5])
        self.assertNotIn("run-shell", observed[0][5])
        self.assertEqual(1, observed[0][5].count("if-shell -F"))
        self.assertLess(observed[0][5].index("capture-pane"), observed[0][5].index("#{buffer_full}"))
        self.assertLess(observed[0][5].index("#{buffer_full}"), observed[0][5].index("send-keys"))
        self.assertLess(observed[0][5].index("delete-buffer"), observed[0][5].index("send-keys"))
        self.assertNotIn("send-keys", observed[0][6])
        self.assertEqual(1, len(observed))

    def test_capture_guard_rejects_drift_and_non_cancellation_commands(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="a" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "", "", "", "reviewed capture\n"],
            ),
            patch.object(
                subject,
                "tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "", ""),
            ),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                return_value=f"OMO_DISPOSITION_CAPTURE_REJECTED_{'a' * 32}\n",
            ) as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "C-c"],
                validate_before=lambda: ("status_input", b"reviewed capture"),
                validate_after=lambda: ("ready", b"ready"),
            )
        guarded.assert_called_once()

        with (
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "cancellation-only"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Enter"],
                validate_before=lambda: ("status_input", b"reviewed capture"),
                validate_after=lambda: ("ready", b"ready"),
            )
        guarded.assert_not_called()

        buffers = "1\n" * 95
        with (
            patch.object(subject.secrets, "token_hex", return_value="b" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", buffers, "", "", "reviewed capture\n"],
            ),
            patch.object(
                subject,
                "tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "", ""),
            ),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                return_value=f"OMO_DISPOSITION_CAPTURE_ACCEPTED_{'b' * 32}\n",
            ) as guarded,
        ):
            result = subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", b"reviewed capture"),
                validate_after=lambda: ("status_input", b"status input"),
            )
        self.assertEqual(("status_input", b"status input"), result)
        command = guarded.call_args.args[2][0][5]
        self.assertIn(f"set-option -g buffer-limit {subject.TEMPORARY_TMUX_BUFFER_LIMIT}", command)
        guarded.assert_called_once()

    def test_capture_guard_fails_closed_when_inventory_exceeds_bound(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=[
                    "bunx|0|50\n",
                    "".join(f"buffer-{index}\n" for index in range(subject.MAX_TMUX_BUFFER_INVENTORY + 1)),
                ],
            ),
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "inventory exceeds"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: self.fail("capture validation must not run"),
                validate_after=lambda: self.fail("post-validation must not run"),
            )
        guarded.assert_not_called()

    def test_capture_guard_fails_closed_before_overwriting_existing_option(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="c" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "", "existing option\n"],
            ),
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "guard option already exists"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: self.fail("capture validation must not run"),
                validate_after=lambda: self.fail("post-validation must not run"),
            )
        guarded.assert_not_called()

    def test_capture_guard_attempts_owned_state_restoration_after_tmux_timeout(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="d" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "", "", "", "reviewed capture\n"],
            ),
            patch.object(
                subject,
                "tmux",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "", ""),
            ),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                side_effect=subprocess.TimeoutExpired(["tmux", "if-shell"], 5),
            ),
            patch.object(subject, "restore_temporary_capture_state") as restore,
            self.assertRaisesRegex(TaskFrontmatterError, "did not complete"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", b"reviewed capture"),
                validate_after=lambda: ("status_menu", b"reviewed capture"),
            )
        restore.assert_called_once_with(
            pin,
            lease_option=f"@omo-disposition-buffer-limit-{'d' * 32}",
            capture_option=f"@omo-disposition-capture-{'d' * 32}",
            original_buffer_limit="50",
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the format integration test")
    def test_capture_guard_runs_against_supported_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run(
                "new-session",
                "-d",
                "-s",
                "guard",
                "-x",
                "80",
                "-y",
                "24",
                "printf 'héllø 世界\\n#{literal}, braces and backslash \\\\\\ here\\nsecond line\\n'; sleep 30",
            )
            try:
                run("set-option", "-g", "buffer-limit", "100")
                for index in range(95):
                    run("set-buffer", f"preserved-{index}")
                run("set-option", "-g", "buffer-limit", "50")
                before_buffers = run(
                    "list-buffers",
                    "-F",
                    "#{buffer_name}|#{buffer_size}|#{buffer_full}",
                ).stdout
                self.assertEqual(95, len(before_buffers.splitlines()))
                identity = run(
                    "display-message",
                    "-p",
                    "-t",
                    "guard:0.0",
                    "#{pane_id}|#{pane_pid}",
                ).stdout.strip()
                pane_id, raw_pid = identity.split("|", 1)
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()
                self.assertIn("héllø 世界\n#{literal}, braces and backslash \\\\ here\nsecond line\n".encode(), capture)
                self.assertTrue(capture.endswith(b"\n"))

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                with (
                    patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "tmux", side_effect=isolated_tmux),
                ):
                    state, _after = subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "Escape"],
                        validate_before=lambda: ("status_menu", capture),
                        validate_after=lambda: ("status_input", capture),
                    )
                    self.assertEqual("status_input", state)
                    self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                    self.assertEqual(
                        before_buffers,
                        run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                    )
                    with self.assertRaisesRegex(TaskFrontmatterError, "capture changed"):
                        subject.guarded_tmux_command_for_capture(
                            pin,
                            ["send-keys", "-t", pane_id, "Escape"],
                            validate_before=lambda: ("status_menu", b"not the current capture"),
                            validate_after=lambda: ("status_input", capture),
                        )
                    self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                    self.assertEqual(
                        before_buffers,
                        run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                    )
            finally:
                run("kill-server", check=False)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the failure integration test")
    def test_capture_guard_restores_real_server_after_mid_queue_capture_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run("new-session", "-d", "-s", "guard", "-x", "80", "-y", "24", "cat")
            run("set-option", "-g", "remain-on-exit", "on")
            try:
                run("set-option", "-g", "buffer-limit", "100")
                for index in range(95):
                    run("set-buffer", f"preserved-{index}")
                run("set-option", "-g", "buffer-limit", "50")
                before_buffers = run(
                    "list-buffers",
                    "-F",
                    "#{buffer_name}|#{buffer_size}|#{buffer_full}",
                ).stdout
                pane_id, raw_pid = (
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        "guard:0.0",
                        "#{pane_id}|#{pane_pid}",
                    )
                    .stdout.strip()
                    .split("|", 1)
                )
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()
                original_sequence = subject.guarded_tmux_sequence

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                def failed_capture_sequence(
                    target: str,
                    expected_pane_id: str,
                    commands: list[list[str]],
                    expected_pane_pid: int = 0,
                ) -> str:
                    mutated = [list(command) for command in commands]
                    capture_command = f"capture-pane -J -N -t {pane_id}"
                    failed_command = "capture-pane -J -N -t %999999999"
                    self.assertEqual(1, mutated[0][5].count(capture_command))
                    mutated[0][5] = mutated[0][5].replace(capture_command, failed_command)
                    return original_sequence(
                        target,
                        expected_pane_id,
                        mutated,
                        expected_pane_pid,
                    )

                with (
                    patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "guarded_tmux_sequence", side_effect=failed_capture_sequence),
                    self.assertRaisesRegex(TaskFrontmatterError, "did not complete"),
                ):
                    subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "C-c"],
                        validate_before=lambda: ("status_input", capture),
                        validate_after=lambda: ("status_input", capture),
                    )
                self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                self.assertEqual(
                    before_buffers,
                    run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                )
                self.assertNotIn(
                    "@omo-disposition-",
                    run("show-options", "-p", "-t", pane_id).stdout,
                )
                self.assertNotIn("@omo-disposition-", run("show-options", "-s").stdout)
                self.assertEqual(
                    "0|cat",
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        pane_id,
                        "#{pane_dead}|#{pane_current_command}",
                    ).stdout.strip(),
                )
            finally:
                run("kill-server", check=False)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the race integration test")
    def test_capture_guard_rejects_real_output_race_without_key_or_buffer_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run("new-session", "-d", "-s", "guard", "-x", "80", "-y", "24", "cat")
            run("set-option", "-g", "remain-on-exit", "on")
            try:
                pane_id, raw_pid = (
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        "guard:0.0",
                        "#{pane_id}|#{pane_pid}",
                    )
                    .stdout.strip()
                    .split("|", 1)
                )
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()
                original_sequence = subject.guarded_tmux_sequence

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                def raced_sequence(
                    target: str,
                    expected_pane_id: str,
                    commands: list[list[str]],
                    expected_pane_pid: int = 0,
                ) -> str:
                    run("send-keys", "-t", pane_id, "-l", "capture-race")
                    run("send-keys", "-t", pane_id, "Enter")
                    time.sleep(0.05)
                    self.assertNotEqual(
                        capture,
                        run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode(),
                    )
                    return original_sequence(
                        target,
                        expected_pane_id,
                        commands,
                        expected_pane_pid,
                    )

                with (
                    patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "guarded_tmux_sequence", side_effect=raced_sequence),
                    self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
                ):
                    subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "C-c"],
                        validate_before=lambda: ("status_input", capture),
                        validate_after=lambda: ("ready", capture),
                    )
                time.sleep(0.05)
                self.assertEqual(
                    "0|cat",
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        pane_id,
                        "#{pane_dead}|#{pane_current_command}",
                    ).stdout.strip(),
                )
                self.assertEqual("", run("list-buffers", "-F", "#{buffer_name}").stdout)
            finally:
                run("kill-server", check=False)

    def test_prior_close_control_paths_are_reserved_for_prepare_and_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            prior_complete = tmp / "close.json"
            prior_prepared = tmp / "close.json.prepared"
            controls = subject.prior_close_control_paths(prior_complete, prior_prepared)
            self.assertEqual(
                {
                    prior_complete,
                    prior_prepared,
                    tmp / ".close.json.prepared.owner-close-started",
                    tmp / ".close.json.prepared.owner-stopped",
                },
                controls,
            )
            for path in controls:
                with self.subTest(path=path, role="packet"), self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                    subject.validate_disposition_output_paths(path, tmp / "disposition.json", controls)
                with self.subTest(path=path, role="audit"), self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                    subject.validate_disposition_output_paths(tmp / "packet.json", path, controls)
            with self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                subject.validate_disposition_output_paths(
                    tmp / "disposition.json.prepared",
                    tmp / "disposition.json",
                    controls,
                )

            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )
            for path in controls:
                packet = disposition_packet(tmp)
                packet["prepared_close_audit"] = str(prior_prepared)
                packet["audit"] = str(path)
                with (
                    self.subTest(path=path, role="execute"),
                    patch.object(subject, "read_bound", return_value=b"packet"),
                    patch.object(subject, "validate_packet", return_value=packet),
                    patch.object(subject, "validate_review"),
                    patch.object(subject, "publish_or_validate") as publish,
                    self.assertRaisesRegex(TaskFrontmatterError, "overlap"),
                ):
                    subject.execute(args)
                publish.assert_not_called()

    def test_complete_audit_retains_prior_prepared_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet = disposition_packet(Path(directory))
            record = json.loads(subject.complete_disposition_audit(packet, "8" * 64, "9" * 64))
        self.assertEqual(packet["prepared_close_audit"], record["prepared_close_audit"])
        self.assertEqual(packet["prepared_close_audit_sha256"], record["prepared_close_audit_sha256"])
        self.assertEqual("ready", record["final_state"])
        self.assertIn("prior close packet superseded", record["result"])


if __name__ == "__main__":
    unittest.main()
