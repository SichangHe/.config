from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task_status import DONE_CLOSE_IN_PROGRESS
from omo_manager.omo_webconf_exited_shell_close import Args
from omo_manager.omo_webconf_exited_shell_close import MANAGER_TARGET
from omo_manager.omo_webconf_exited_shell_close import INCIDENT_CAPTURE_SHA256
from omo_manager.omo_webconf_exited_shell_close import INCIDENT_TASK_SHA256
from omo_manager.omo_webconf_exited_shell_close import INCIDENT_TODO_SHA256
from omo_manager.omo_webconf_exited_shell_close import REPORT_COMMITMENT_SHA256
from omo_manager.omo_webconf_exited_shell_close import REPORT_COMMITMENT_PATH
from omo_manager.omo_webconf_exited_shell_close import REPORT_REPLAY_ID
from omo_manager.omo_webconf_exited_shell_close import SESSION_ID
from omo_manager.omo_webconf_exited_shell_close import SOURCE_LOCATOR
from omo_manager.omo_webconf_exited_shell_close import SOURCE_TEXT
from omo_manager.omo_webconf_exited_shell_close import TARGET
from omo_manager.omo_webconf_exited_shell_close import TASK_REF
from omo_manager.omo_webconf_exited_shell_close import parse_args
from omo_manager.omo_webconf_exited_shell_close import run
from omo_manager.omo_webconf_exited_shell_close import validate_report_commitment
from omo_manager.omo_webconf_exited_shell_close import validate_close_authority_file


class WebconfExitedShellCloseTests(unittest.TestCase):
    SOURCE_BYTES = base64.b64decode(
        "U3ViamVjdDogUmU6IHNhbXBsZSB0aGV3ZWJjb25mIHBhcGVycwoKRW1haWwgbWUgdGhlIHBhcGVyIHRpdGxlLCBhdXRob3JzLCB5ZWFyLCBvZmZpY2lhbCBVUkwsIHRoZW4gVVJMIHRoYXQgc2VhcmNoZXMgaXQgaW4gR29vZ2xlIFNjaG9sYXIsIGluIGEgbGlzdA0K"
    )

    def fixture(self, root: Path, *, pending: bool = False) -> tuple[Args, Path, Path]:
        root.chmod(0o700)
        task = root / TASK_REF
        todo = root / "TODO.md"
        queue = "pending_task_items:\n  - still open" if pending else "pending_task_items: []"
        task_text = (
            "---\nversion: v1.0.0\nstatus: blocked\n"
            f"blocked_on: {DONE_CLOSE_IN_PROGRESS}\nrunat: {TARGET}\ntool: codex\nmanagerat: {MANAGER_TARGET}\nis_manager: false\n{queue}\n---\n"
            f'<human_instruction authoritative="true" source="{SOURCE_LOCATOR}">\n{SOURCE_TEXT}</human_instruction>\n'
            f"(verified removed pending item: Completed and routed private packet via omo_report.sh done receipt {REPORT_REPLAY_ID}; reviewed.)\n"
        )
        task.write_text(task_text, encoding="utf-8")
        todo.write_text(f"current:\n{TASK_REF} {TARGET}\n\nhuman pending:\n\nlow priority:\n\nprevious:\n", encoding="utf-8")
        manager_mail = root / "manager_mail"
        manager_mail.mkdir(mode=0o700)
        source = manager_mail / "85c5dff58359-1524.txt"
        source.write_bytes(self.SOURCE_BYTES)
        source.chmod(0o600)
        args = Args(root, TASK_REF, hashlib.sha256(task.read_bytes()).hexdigest(), hashlib.sha256(todo.read_bytes()).hexdigest(), "%42", 5252, 7001, "c" * 64, REPORT_COMMITMENT_PATH, REPORT_COMMITMENT_SHA256, root / "close.audit")
        return args, task, todo

    def test_closes_exact_shell_and_finishes_without_mail_or_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo = self.fixture(root)
            closed = False

            def pane(value: str) -> str:
                return "" if closed else ("%42" if value in {TARGET, "%42"} else "")

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", side_effect=pane),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", side_effect=lambda _pid: None if closed else 7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=close) as close_call,
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", side_effect=lambda *_args: closed),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
            ):
                self.assertEqual(0, run(args))
            close_call.assert_called_once()
            self.assertIn("status: done\nrunat: dw:19", task.read_text())
            self.assertIn(f"session_id: `{SESSION_ID}`", task.read_text())
            self.assertIn(f"previous:\n{TASK_REF} {TARGET}\n", todo.read_text())
            self.assertIn('"state":"complete"', args.audit_output.read_text())
            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=None),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=True),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target") as duplicate_close,
            ):
                self.assertEqual(0, run(args))
            duplicate_close.assert_not_called()

    def test_retries_after_close_before_audit_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, _todo = self.fixture(root)
            closed = False
            module = __import__("omo_manager.omo_webconf_exited_shell_close", fromlist=["replace_private_audit"])
            original_replace = module.replace_private_audit

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", side_effect=lambda _value: "" if closed else "%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", side_effect=lambda _pid: None if closed else 7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if closed and "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=close),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", side_effect=lambda *_args: closed),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("omo_manager.omo_webconf_exited_shell_close.replace_private_audit", side_effect=OSError("crash")),
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=None),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=True),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("omo_manager.omo_webconf_exited_shell_close.replace_private_audit", side_effect=original_replace),
            ):
                self.assertEqual(0, run(args))
            self.assertIn("status: done", task.read_text())

    def test_recovers_transaction_first_initialization_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, _todo = self.fixture(root)
            module = __import__("omo_manager.omo_webconf_exited_shell_close", fromlist=["reserve_private_audit"])
            original_reserve = module.reserve_private_audit
            calls = 0

            def interrupt_second(path: Path, text: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("authority reserve interrupted")
                original_reserve(path, text)

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value="%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.reserve_private_audit", side_effect=interrupt_second),
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            self.assertTrue(args.audit_output.exists())
            self.assertFalse(args.audit_output.with_name(f".{args.audit_output.name}.close-authority").exists())
            closed = False

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", side_effect=lambda _value: "" if closed else "%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", side_effect=lambda _pid: None if closed else 7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=close),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", side_effect=lambda *_args: closed),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
            ):
                self.assertEqual(0, run(args))
            self.assertIn("status: done", task.read_text())

    def test_validates_canonical_report_commitment_and_report_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            task = root / TASK_REF
            task.write_text("task\n", encoding="utf-8")
            report = root / "report.md"
            report.write_text("Completed the manager-routed addition to the existing Web Conference paper-list lane; exact.\n", encoding="utf-8")
            report.chmod(0o600)
            report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
            commitment = root / "commitment"
            record = {
                "allocation": {"file": str(report), "file_at_submission": {"sha256": report_sha}},
                "replay_id": REPORT_REPLAY_ID,
                "schema": "omo-report-transaction-commitment/v2",
                "transfer": {"authority": {"kind": "agent-originated", "producer_target": TARGET, "source_task": str(task)}},
            }
            commitment.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            commitment.chmod(0o600)
            digest = hashlib.sha256(commitment.read_bytes()).hexdigest()
            args = Args(root, TASK_REF, "a" * 64, "b" * 64, "%42", 5252, 7001, "c" * 64, commitment, digest, root / "audit")
            with patch("omo_manager.omo_webconf_exited_shell_close.REPORT_COMMITMENT_SHA256", digest), patch("omo_manager.omo_webconf_exited_shell_close.REPORT_SHA256", report_sha):
                validate_report_commitment(args, task)
            report.write_text("wrong\n", encoding="utf-8")
            with patch("omo_manager.omo_webconf_exited_shell_close.REPORT_COMMITMENT_SHA256", digest), patch("omo_manager.omo_webconf_exited_shell_close.REPORT_SHA256", report_sha), self.assertRaises(Exception):
                validate_report_commitment(args, task)

    def test_recovers_todo_first_partial_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo = self.fixture(root)
            closed = False

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            def partial(_root: Path, _task: Path, _text: str, _before: object, **kwargs: object) -> None:
                todo.write_text(str(kwargs["prepared_todo"]), encoding="utf-8")
                raise OSError("task publish interrupted")

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", side_effect=lambda _value: "" if closed else "%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", side_effect=lambda _pid: None if closed else 7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if closed and "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=close),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=True),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("omo_manager.omo_webconf_exited_shell_close.finish_done_transaction", side_effect=partial),
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=None),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=True),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
            ):
                self.assertEqual(0, run(args))
            self.assertIn("status: done", task.read_text())

    def test_rejects_symlinked_source_and_unsafe_audit_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, _todo = self.fixture(root)
            real = root / "real-mail"
            (root / "manager_mail").rename(real)
            (root / "manager_mail").symlink_to(real, target_is_directory=True)
            with patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"), patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root), patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(2, run(args))
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            args = Args(**{**args.__dict__, "audit_output": unsafe / "audit"})
            with patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"), patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(2, run(args))
            self.assertNotIn("status: done", task.read_text())

    def test_rejects_crlf_drift_after_audit_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, _todo = self.fixture(root)

            def drift(*_args: object, **_kwargs: object) -> None:
                task.write_bytes(task.read_bytes().replace(b"\n", b"\r\n"))
                identity_is_current = _args[1]
                self.assertFalse(identity_is_current())
                raise RuntimeError("bound evidence drifted")

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value="%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", side_effect=["c" * 64, "c" * 64]),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=drift) as close,
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=False),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            close.assert_called_once()
            self.assertNotIn("status: done", task.read_text())

    def test_bound_child_revalidates_terminal_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, _task, _todo = self.fixture(root)
            module = __import__("omo_manager.omo_webconf_exited_shell_close", fromlist=["close_authority_text"])
            args = Args(**{
                **args.__dict__,
                "expected_task_sha256": INCIDENT_TASK_SHA256,
                "expected_todo_sha256": INCIDENT_TODO_SHA256,
                "expected_capture_sha256": INCIDENT_CAPTURE_SHA256,
            })
            secret = "a" * 64
            commitment = hashlib.sha256(secret.encode()).hexdigest()
            authority = module.close_authority_text(args, commitment)
            authority_path = root / "authority"
            authority_path.write_text(authority, encoding="utf-8")
            authority_path.chmod(0o600)
            digest = hashlib.sha256(authority.encode()).hexdigest()
            with patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="0" * 64), self.assertRaises(RuntimeError):
                validate_close_authority_file(authority_path, commitment, TARGET, "%42", 5252, 7001, digest)

    def test_complete_audit_rejects_reverted_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, _todo = self.fixture(root)
            closed = False

            def close(*_args: object, **_kwargs: object) -> None:
                nonlocal closed
                closed = True

            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", side_effect=lambda _value: "" if closed else "%42"),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", side_effect=lambda _pid: None if closed else 7001),
                patch("omo_manager.omo_webconf_exited_shell_close.validate_exited_codex_shell_with_consumed_report", return_value="c" * 64),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.close_bound_tmux_target", side_effect=close),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", side_effect=lambda *_args: closed),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
            ):
                self.assertEqual(0, run(args))
            record = json.loads(args.audit_output.read_text())
            task.write_text(record["source_task"], encoding="utf-8")
            with (
                patch("omo_manager.omo_webconf_exited_shell_close.validate_report_commitment"),
                patch("omo_manager.omo_webconf_exited_shell_close.EXPECTED_ROOT", root),
                patch("omo_manager.omo_webconf_exited_shell_close.SOURCE_SHA256", hashlib.sha256(self.SOURCE_BYTES).hexdigest()),
                patch("omo_manager.omo_webconf_exited_shell_close.pane_id", return_value=""),
                patch("omo_manager.omo_webconf_exited_shell_close.process_start_ticks", return_value=None),
                patch("omo_manager.omo_webconf_exited_shell_close.bound_close_secret", side_effect=lambda path, *_args: "a" * 64 if "owner-stopped" in str(path) else ""),
                patch("omo_manager.omo_webconf_exited_shell_close.has_bound_close_proof", return_value=True),
                patch("omo_manager.omo_webconf_exited_shell_close.path_entry_exists", return_value=False),
                patch("sys.stderr", new=io.StringIO()),
            ):
                self.assertEqual(2, run(args))

    def test_parser_requires_exact_registered_shape(self) -> None:
        argv = ["--root", "/tmp/work", "--expected-task-sha256", INCIDENT_TASK_SHA256, "--expected-todo-sha256", INCIDENT_TODO_SHA256, "--expected-pane-id", "%42", "--expected-pane-pid", "5252", "--expected-pane-start-ticks", "7001", "--expected-capture-sha256", INCIDENT_CAPTURE_SHA256, "--report-commitment", str(REPORT_COMMITMENT_PATH), "--report-commitment-sha256", REPORT_COMMITMENT_SHA256, "--audit-output", "/tmp/audit", str(TASK_REF)]
        self.assertEqual(TASK_REF, parse_args(argv).task_file)
        with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
            parse_args([*argv[:-1], "other.md"])


if __name__ == "__main__":
    unittest.main()
