from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import omo_manager.omo_webconf_exited_shell_recovery as subject
import omo_manager.omo_webconf_exited_shell_close as close_subject
from omo_manager.omo_task_status import TaskFrontmatterError


class WebconfExitedShellRecoveryTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        root.chmod(0o700)
        task = root / subject.TASK_REF
        task.write_text(
            "---\nversion: v1.0.0\nstatus: blocked\n"
            "blocked_on: done_close_in_progress: manager is closing the agent before marking done\n"
            f"runat: {subject.TARGET}\ntool: codex\nmanagerat: {subject.MANAGER_TARGET}\nis_manager: false\npending_task_items: []\n---\n",
            encoding="utf-8",
        )
        todo = root / "TODO.md"
        todo.write_text(
            f"rules: retained\n\ncurrent:\nother.md dw:7\n{subject.TASK_REF} {subject.TARGET}\n\nhuman pending:\n\nlow priority:\n\nprevious:\nold.md dw:2\n",
            encoding="utf-8",
        )
        return task, todo

    @contextmanager
    def patches(self, root: Path, task: Path):
        with (
            patch.object(subject, "EXPECTED_ROOT", root),
            patch.object(subject, "INCIDENT_TASK_SHA256", hashlib.sha256(task.read_bytes()).hexdigest()),
            patch.object(subject, "validate_report_commitment"),
            patch.object(subject, "validate_source_task"),
            patch.object(subject, "pane_id", side_effect=lambda value: subject.RECOVERY_PANE_ID if value in {subject.TARGET, subject.RECOVERY_PANE_ID} else ""),
            patch.object(subject, "process_start_ticks", return_value=subject.RECOVERY_PANE_START_TICKS),
            patch.object(subject, "validate_exited_codex_shell_with_consumed_report", return_value=subject.FAILED_OBSERVED_CAPTURE_SHA256),
        ):
            yield

    def prepare(self, root: Path, task: Path) -> tuple[Path, Path, Path]:
        packet = root / "packet.json"
        audit = root / "close.json"
        review = root / "review.json"
        with self.patches(root, task):
            subject.prepare(packet, audit)
            packet_sha256 = hashlib.sha256(packet.read_bytes()).hexdigest()
            subject.review(packet, packet_sha256, review)
        return packet, audit, review

    def test_prepare_and_review_bind_current_whole_todo_and_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo = self.fixture(root)
            packet, audit, review = self.prepare(root, task)
            record = json.loads(packet.read_text())
            self.assertEqual(hashlib.sha256(todo.read_bytes()).hexdigest(), record["todo_input"]["sha256"])
            self.assertEqual(str(audit), record["close_audit_output"])
            packet_sha256 = hashlib.sha256(packet.read_bytes()).hexdigest()
            review_sha256 = hashlib.sha256(review.read_bytes()).hexdigest()
            with self.patches(root, task):
                subject.validate_recovery_files(
                    packet,
                    packet_sha256,
                    review,
                    review_sha256,
                    expected_task_sha256=record["task_input"]["sha256"],
                    expected_todo_sha256=record["todo_input"]["sha256"],
                    expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                    expected_pane_id=subject.RECOVERY_PANE_ID,
                    expected_pane_pid=subject.RECOVERY_PANE_PID,
                    expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                    audit_output=audit,
                    assert_current_inputs=True,
                )

    def test_prepare_rejects_changed_owned_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo = self.fixture(root)
            todo.write_text(todo.read_text().replace(str(subject.TASK_REF), "other-task.md"), encoding="utf-8")
            with self.patches(root, task), self.assertRaisesRegex(TaskFrontmatterError, "one exact TODO row"):
                subject.prepare(root / "packet", root / "audit")

    def test_validation_rejects_unrelated_todo_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo = self.fixture(root)
            packet, audit, review = self.prepare(root, task)
            record = json.loads(packet.read_text())
            todo.write_text(todo.read_text().replace("other.md", "changed.md"), encoding="utf-8")
            with self.patches(root, task), self.assertRaisesRegex(TaskFrontmatterError, "changed after recovery preparation"):
                subject.validate_recovery_files(
                    packet,
                    hashlib.sha256(packet.read_bytes()).hexdigest(),
                    review,
                    hashlib.sha256(review.read_bytes()).hexdigest(),
                    expected_task_sha256=record["task_input"]["sha256"],
                    expected_todo_sha256=record["todo_input"]["sha256"],
                    expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                    expected_pane_id=subject.RECOVERY_PANE_ID,
                    expected_pane_pid=subject.RECOVERY_PANE_PID,
                    expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                    audit_output=audit,
                    assert_current_inputs=True,
                )

    def test_validation_rejects_capture_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _todo = self.fixture(root)
            packet, audit, review = self.prepare(root, task)
            record = json.loads(packet.read_text())
            with (
                patch.object(subject, "EXPECTED_ROOT", root),
                patch.object(subject, "INCIDENT_TASK_SHA256", hashlib.sha256(task.read_bytes()).hexdigest()),
                patch.object(subject, "validate_report_commitment"),
                patch.object(subject, "validate_source_task"),
                patch.object(subject, "pane_id", return_value=subject.RECOVERY_PANE_ID),
                patch.object(subject, "process_start_ticks", return_value=subject.RECOVERY_PANE_START_TICKS),
                patch.object(subject, "validate_exited_codex_shell_with_consumed_report", return_value="0" * 64),
                self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
            ):
                subject.validate_recovery_files(
                    packet,
                    hashlib.sha256(packet.read_bytes()).hexdigest(),
                    review,
                    hashlib.sha256(review.read_bytes()).hexdigest(),
                    expected_task_sha256=record["task_input"]["sha256"],
                    expected_todo_sha256=record["todo_input"]["sha256"],
                    expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                    expected_pane_id=subject.RECOVERY_PANE_ID,
                    expected_pane_pid=subject.RECOVERY_PANE_PID,
                    expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                    audit_output=audit,
                    assert_current_inputs=True,
                )

    def test_validation_rejects_review_or_invocation_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _todo = self.fixture(root)
            packet, audit, review = self.prepare(root, task)
            packet_sha256 = hashlib.sha256(packet.read_bytes()).hexdigest()
            review_sha256 = hashlib.sha256(review.read_bytes()).hexdigest()
            record = json.loads(packet.read_text())
            with self.patches(root, task), self.assertRaisesRegex(TaskFrontmatterError, "invocation does not match"):
                subject.validate_recovery_files(
                    packet,
                    packet_sha256,
                    review,
                    review_sha256,
                    expected_task_sha256=record["task_input"]["sha256"],
                    expected_todo_sha256="0" * 64,
                    expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                    expected_pane_id=subject.RECOVERY_PANE_ID,
                    expected_pane_pid=subject.RECOVERY_PANE_PID,
                    expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                    audit_output=audit,
                    assert_current_inputs=False,
                )
            review.write_text(review.read_text().replace('"result":"PASS"', '"result":"FAIL"'), encoding="utf-8")
            with self.patches(root, task), self.assertRaisesRegex(TaskFrontmatterError, "digest is invalid"):
                subject.validate_recovery_files(
                    packet,
                    packet_sha256,
                    review,
                    review_sha256,
                    expected_task_sha256=record["task_input"]["sha256"],
                    expected_todo_sha256=record["todo_input"]["sha256"],
                    expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                    expected_pane_id=subject.RECOVERY_PANE_ID,
                    expected_pane_pid=subject.RECOVERY_PANE_PID,
                    expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                    audit_output=audit,
                    assert_current_inputs=False,
                )

    def test_validation_rejects_task_metadata_helper_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _todo = self.fixture(root)
            metadata_helper = root / "task-metadata.py"
            metadata_helper.write_text("original\n", encoding="utf-8")
            sources = subject.source_paths()
            sources["task_metadata_input"] = metadata_helper
            with patch.object(subject, "source_paths", return_value=sources):
                packet, audit, review = self.prepare(root, task)
                record = json.loads(packet.read_text())
                metadata_helper.write_text("changed\n", encoding="utf-8")
                with self.patches(root, task), self.assertRaisesRegex(TaskFrontmatterError, "changed after recovery preparation"):
                    subject.validate_recovery_files(
                        packet,
                        hashlib.sha256(packet.read_bytes()).hexdigest(),
                        review,
                        hashlib.sha256(review.read_bytes()).hexdigest(),
                        expected_task_sha256=record["task_input"]["sha256"],
                        expected_todo_sha256=record["todo_input"]["sha256"],
                        expected_capture_sha256=subject.FAILED_OBSERVED_CAPTURE_SHA256,
                        expected_pane_id=subject.RECOVERY_PANE_ID,
                        expected_pane_pid=subject.RECOVERY_PANE_PID,
                        expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                        audit_output=audit,
                        assert_current_inputs=False,
                    )

    def test_closer_run_uses_reviewed_recovery_to_prepare_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo = self.fixture(root)
            packet, audit, review = self.prepare(root, task)
            task_sha256 = hashlib.sha256(task.read_bytes()).hexdigest()
            todo_sha256 = hashlib.sha256(todo.read_bytes()).hexdigest()
            args = close_subject.Args(
                root,
                close_subject.TASK_REF,
                task_sha256,
                todo_sha256,
                subject.RECOVERY_PANE_ID,
                subject.RECOVERY_PANE_PID,
                subject.RECOVERY_PANE_START_TICKS,
                subject.FAILED_OBSERVED_CAPTURE_SHA256,
                close_subject.REPORT_COMMITMENT_PATH,
                close_subject.REPORT_COMMITMENT_SHA256,
                audit,
                packet,
                hashlib.sha256(packet.read_bytes()).hexdigest(),
                review,
                hashlib.sha256(review.read_bytes()).hexdigest(),
            )
            closed = False

            def pane(value: str) -> str:
                return "" if closed else (subject.RECOVERY_PANE_ID if value in {subject.TARGET, subject.RECOVERY_PANE_ID} else "")

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            with (
                patch.object(subject, "EXPECTED_ROOT", root),
                patch.object(subject, "INCIDENT_TASK_SHA256", task_sha256),
                patch.object(subject, "validate_report_commitment"),
                patch.object(subject, "validate_source_task"),
                patch.object(subject, "pane_id", side_effect=pane),
                patch.object(subject, "process_start_ticks", side_effect=lambda _pid: None if closed else subject.RECOVERY_PANE_START_TICKS),
                patch.object(subject, "validate_exited_codex_shell_with_consumed_report", return_value=subject.FAILED_OBSERVED_CAPTURE_SHA256),
                patch.object(close_subject, "EXPECTED_ROOT", root),
                patch.object(close_subject, "INCIDENT_TASK_SHA256", task_sha256),
                patch.object(close_subject, "validate_report_commitment"),
                patch.object(close_subject, "validate_source_task"),
                patch.object(close_subject, "pane_id", side_effect=pane),
                patch.object(close_subject, "process_start_ticks", side_effect=lambda _pid: None if closed else subject.RECOVERY_PANE_START_TICKS),
                patch.object(close_subject, "validate_exited_codex_shell_with_consumed_report", return_value=subject.FAILED_OBSERVED_CAPTURE_SHA256),
                patch.object(close_subject, "bound_close_secret", return_value=""),
                patch.object(close_subject, "close_bound_tmux_target", side_effect=close) as close_call,
                patch.object(close_subject, "has_bound_close_proof", side_effect=lambda *_args: closed),
                patch.object(close_subject, "path_entry_exists", return_value=False),
            ):
                self.assertEqual(0, close_subject.run(args))
            close_call.assert_called_once()
            transaction = json.loads(audit.read_text())
            self.assertEqual("complete", transaction["state"])
            self.assertEqual(args.recovery_packet_sha256, transaction["recovery_packet_sha256"])
            self.assertIn("status: done", task.read_text())

    def test_closer_parser_and_child_bind_exact_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            private.chmod(0o700)
            packet = private / "packet"
            review = private / "review"
            audit = private / "audit"
            packet.write_text("packet\n", encoding="utf-8")
            review.write_text("review\n", encoding="utf-8")
            packet.chmod(0o600)
            review.chmod(0o600)
            todo_sha256 = "1" * 64
            capture_sha256 = "2" * 64
            argv = [
                "--root",
                str(subject.EXPECTED_ROOT),
                "--expected-task-sha256",
                close_subject.INCIDENT_TASK_SHA256,
                "--expected-todo-sha256",
                todo_sha256,
                "--expected-pane-id",
                subject.RECOVERY_PANE_ID,
                "--expected-pane-pid",
                str(subject.RECOVERY_PANE_PID),
                "--expected-pane-start-ticks",
                str(subject.RECOVERY_PANE_START_TICKS),
                "--expected-capture-sha256",
                capture_sha256,
                "--report-commitment",
                str(close_subject.REPORT_COMMITMENT_PATH),
                "--report-commitment-sha256",
                close_subject.REPORT_COMMITMENT_SHA256,
                "--audit-output",
                str(audit),
                "--recovery-packet",
                str(packet),
                "--recovery-packet-sha256",
                "3" * 64,
                "--recovery-review",
                str(review),
                "--recovery-review-sha256",
                "4" * 64,
                str(close_subject.TASK_REF),
            ]
            args = close_subject.parse_args(argv)
            secret = "s" * 64
            commitment = hashlib.sha256(secret.encode()).hexdigest()
            authority_text = close_subject.close_authority_text(args, commitment)
            authority = private / "authority"
            authority.write_text(authority_text, encoding="utf-8")
            authority.chmod(0o600)
            with (
                patch.object(subject, "validate_recovery_files") as validate,
                patch.object(close_subject, "validate_exited_codex_shell_with_consumed_report", return_value=capture_sha256),
            ):
                close_subject.validate_close_authority_file(
                    authority,
                    commitment,
                    close_subject.TARGET,
                    subject.RECOVERY_PANE_ID,
                    subject.RECOVERY_PANE_PID,
                    subject.RECOVERY_PANE_START_TICKS,
                    hashlib.sha256(authority_text.encode()).hexdigest(),
                )
            validate.assert_called_once_with(
                packet,
                "3" * 64,
                review,
                "4" * 64,
                expected_task_sha256=close_subject.INCIDENT_TASK_SHA256,
                expected_todo_sha256=todo_sha256,
                expected_capture_sha256=capture_sha256,
                expected_pane_id=subject.RECOVERY_PANE_ID,
                expected_pane_pid=subject.RECOVERY_PANE_PID,
                expected_pane_start_ticks=subject.RECOVERY_PANE_START_TICKS,
                audit_output=audit,
                assert_current_inputs=True,
            )

    def test_closer_rejects_partial_recovery_binding(self) -> None:
        argv = [
            "--root",
            str(subject.EXPECTED_ROOT),
            "--expected-task-sha256",
            close_subject.INCIDENT_TASK_SHA256,
            "--expected-todo-sha256",
            "1" * 64,
            "--expected-pane-id",
            subject.RECOVERY_PANE_ID,
            "--expected-pane-pid",
            str(subject.RECOVERY_PANE_PID),
            "--expected-pane-start-ticks",
            str(subject.RECOVERY_PANE_START_TICKS),
            "--expected-capture-sha256",
            "2" * 64,
            "--report-commitment",
            str(close_subject.REPORT_COMMITMENT_PATH),
            "--report-commitment-sha256",
            close_subject.REPORT_COMMITMENT_SHA256,
            "--audit-output",
            "/tmp/audit",
            "--recovery-packet",
            "/tmp/packet",
            str(close_subject.TASK_REF),
        ]
        with self.assertRaises(SystemExit):
            close_subject.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
