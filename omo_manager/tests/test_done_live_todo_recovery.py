from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from omo_manager import omo_done_live_todo_recovery as subject
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_task_status import DoneLiveCloseAudit
from omo_manager.omo_task_status import render_done_live_close_audit


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DoneLiveTodoRecoveryTests(unittest.TestCase):
    @staticmethod
    def old_todo_text() -> str:
        return "current:\nother.md dw:9\n\nhuman pending:\n\nlow priority:\n\nprevious:\ndw_bodyswap_pr.md dw8:1\nlast.md dw:7\n"

    def fixture(self, tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
        root = tmp / "work_logs"
        private = tmp / "private"
        root.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        task = root / subject.TASK_NAME
        todo = root / "TODO.md"
        audit = private / "original.json"
        packet = private / "packet.json"
        rebound = private / "rebound.json"
        task_text = "---\nversion: v1.0.0\nstatus: done\nrunat: dw8:1\ntool: codex\nmanagerat: dw:15\nis_manager: false\npending_task_items: []\n---\nbody\n"
        old_todo_text = self.old_todo_text()
        todo_text = old_todo_text.replace("other.md dw:9\n", "other.md dw:9\nnew-unrelated.md dw:8\n")
        task.write_text(task_text, encoding="utf-8")
        todo.write_text(todo_text, encoding="utf-8")
        os.chmod(task, 0o600)
        os.chmod(todo, 0o600)
        with self.constants(root, task_text, old_todo_text, audit):
            old_args = subject.status_args(subject.OLD_TODO_SHA256, audit)
            audit.write_text(render_done_live_close_audit(old_args, task, DoneLiveCloseAudit("prepared")), encoding="utf-8")
        os.chmod(audit, 0o600)
        return task, todo, audit, packet, rebound

    def constants(self, root: Path, task_text: str, todo_text: str, audit: Path):
        terminal = patch.multiple(
            subject,
            ROOT=root,
            TASK_SHA256=sha(task_text.encode()),
            OLD_TODO_SHA256=sha(todo_text.encode()),
            ORIGINAL_AUDIT=audit,
        )
        return terminal

    def prepare(self, tmp: Path) -> tuple[dict[str, object], Path, Path, Path, Path, Path]:
        task, todo, audit, packet, rebound = self.fixture(tmp)
        task_text = task.read_text(encoding="utf-8")
        audit_sha = sha(audit.read_bytes())
        with self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", audit_sha):
            subject.prepare(argparse.Namespace(packet_output=packet, rebound_audit_output=rebound))
        return json.loads(packet.read_text()), task, todo, audit, packet, rebound

    def test_prepare_binds_owned_row_and_every_unrelated_todo_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, _task, todo, _audit, _packet_path, _rebound = self.prepare(tmp)
            before, row, after = subject.decode_partition(packet)
            todo_input = cast(dict[str, object], packet["todo_input"])
            self.assertEqual(todo.read_bytes(), before + row + after)
            self.assertEqual(sha(todo.read_bytes()), todo_input["sha256"])
            self.assertNotEqual(subject.OLD_TODO_SHA256, todo_input["sha256"])
            self.assertEqual(b"dw_bodyswap_pr.md dw8:1\n", row)
            self.assertIn(b"other.md dw:9", before)
            self.assertIn(b"last.md dw:7", after)

    def test_prepare_rejects_owned_row_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            task, todo, audit, packet, rebound = self.fixture(tmp)
            task_text = task.read_text()
            original_todo = todo.read_text()
            todo.write_text(original_todo.replace("dw_bodyswap_pr.md dw8:1", "dw_bodyswap_pr.md dw8:2"))
            with (
                self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit),
                patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())),
                self.assertRaisesRegex(TaskFrontmatterError, "canonical previous TODO row"),
            ):
                subject.prepare(argparse.Namespace(packet_output=packet, rebound_audit_output=rebound))

    def test_review_rejects_unrelated_todo_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, _rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            todo.write_text(todo.read_text() + "later.md dw:6\n")
            with self.constants(tmp / "work_logs", task.read_text(), self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())):
                with self.assertRaisesRegex(TaskFrontmatterError, "changed after preparation"):
                    subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=tmp / "private" / "review.json"))

    def test_prepare_rejects_audit_and_output_sidecar_collisions(self) -> None:
        for collision in ("original", "packet"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                tmp = Path(directory)
                task, todo, audit, packet, _rebound = self.fixture(tmp)
                task_text = task.read_text()
                rebound = audit.with_name(f".{audit.name}.owner-stopped") if collision == "original" else packet.with_name(f".{packet.name}.owner-stopped")
                with (
                    self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit),
                    patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())),
                    self.assertRaisesRegex(TaskFrontmatterError, "overlap"),
                ):
                    subject.prepare(argparse.Namespace(packet_output=packet, rebound_audit_output=rebound))

    def test_execute_rejects_read_to_identity_race_before_rebound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            task_text = task.read_text()
            todo_text = todo.read_text()
            audit_sha = sha(audit.read_bytes())
            with self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", audit_sha):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                real_read = subject.assert_snapshot
                changed = False

                def race(snapshot: subject.Snapshot, label: str, *, private: bool = False) -> bytes:
                    nonlocal changed
                    data = real_read(snapshot, label, private=private)
                    if label == "TODO" and not changed:
                        changed = True
                        todo.write_text(todo_text + "race.md dw:5\n")
                    return data

                with patch.object(subject, "assert_snapshot", side_effect=race), self.assertRaisesRegex(TaskFrontmatterError, "TODO changed after preparation"):
                    subject.execute(
                        argparse.Namespace(
                            packet=packet_path,
                            packet_sha256=packet_sha,
                            review_report=review_path,
                            review_sha256=sha(review_path.read_bytes()),
                        )
                    )
            self.assertFalse(rebound.exists())

    def test_execute_rejects_audit_drift_and_review_mismatch(self) -> None:
        for defect in ("audit", "review"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                tmp = Path(directory)
                packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
                packet_sha = sha(packet_path.read_bytes())
                review_path = tmp / "private" / "review.json"
                task_text = task.read_text()
                original_audit_sha = sha(audit.read_bytes())
                with self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", original_audit_sha):
                    subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                    review_sha = sha(review_path.read_bytes())
                    if defect == "audit":
                        audit.write_text(audit.read_text() + "\n")
                    else:
                        review_sha = "0" * 64
                    with self.assertRaises(TaskFrontmatterError):
                        subject.execute(
                            argparse.Namespace(
                                packet=packet_path,
                                packet_sha256=packet_sha,
                                review_report=review_path,
                                review_sha256=review_sha,
                            )
                        )
                self.assertFalse(rebound.exists())

    def test_execute_rejects_capture_race_before_rebound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            with self.constants(tmp / "work_logs", task.read_text(), self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                with (
                    patch.object(subject, "validate_done_live_ownership"),
                    patch.object(subject, "done_live_pane_state", return_value="live"),
                    patch.object(subject, "validate_exited_codex_shell", side_effect=["c" * 64, "d" * 64]),
                    patch.object(subject, "close_done_live_no_mail") as close,
                    self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
                ):
                    subject.execute(
                        argparse.Namespace(
                            packet=packet_path,
                            packet_sha256=packet_sha,
                            review_report=review_path,
                            review_sha256=sha(review_path.read_bytes()),
                        )
                    )
            self.assertFalse(rebound.exists())
            close.assert_not_called()

    def test_execute_rejects_codex_stop_source_drift_before_rebound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            with self.constants(tmp / "work_logs", task.read_text(), self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                real_assert = subject.assert_snapshot

                def drift(snapshot: subject.Snapshot, label: str, *, private: bool = False) -> bytes:
                    if label == "Codex stop helper":
                        raise TaskFrontmatterError("Codex stop helper changed after preparation.")
                    return real_assert(snapshot, label, private=private)

                with patch.object(subject, "assert_snapshot", side_effect=drift), self.assertRaisesRegex(TaskFrontmatterError, "Codex stop helper changed"):
                    subject.execute(
                        argparse.Namespace(
                            packet=packet_path,
                            packet_sha256=packet_sha,
                            review_report=review_path,
                            review_sha256=sha(review_path.read_bytes()),
                        )
                    )
            self.assertFalse(rebound.exists())

    def test_execute_rejects_task_lock_source_drift_before_rebound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            with self.constants(tmp / "work_logs", task.read_text(), self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                real_assert = subject.assert_snapshot

                def drift(snapshot: subject.Snapshot, label: str, *, private: bool = False) -> bytes:
                    if label == "task lock helper":
                        raise TaskFrontmatterError("task lock helper changed after preparation.")
                    return real_assert(snapshot, label, private=private)

                with patch.object(subject, "assert_snapshot", side_effect=drift), self.assertRaisesRegex(TaskFrontmatterError, "task lock helper changed"):
                    subject.execute(
                        argparse.Namespace(
                            packet=packet_path,
                            packet_sha256=packet_sha,
                            review_report=review_path,
                            review_sha256=sha(review_path.read_bytes()),
                        )
                    )
            self.assertFalse(rebound.exists())

    def test_execute_rejects_hardlinked_existing_rebound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            task_text = task.read_text()
            todo_text = todo.read_text()
            with self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(audit.read_bytes())):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                rebound.write_text(render_done_live_close_audit(subject.status_args(sha(todo_text.encode()), rebound), task, DoneLiveCloseAudit("prepared")))
                os.chmod(rebound, 0o600)
                os.link(rebound, tmp / "private" / "rebound-hardlink.json")
                with self.assertRaisesRegex(TaskFrontmatterError, "unsafe file identity"):
                    subject.execute(
                        argparse.Namespace(
                            packet=packet_path,
                            packet_sha256=packet_sha,
                            review_report=review_path,
                            review_sha256=sha(review_path.read_bytes()),
                        )
                    )

    def test_execute_creates_only_rebound_audit_then_delegates_canonical_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet, task, todo, audit, packet_path, rebound = self.prepare(tmp)
            packet_sha = sha(packet_path.read_bytes())
            review_path = tmp / "private" / "review.json"
            task_text = task.read_text()
            todo_text = todo.read_text()
            original_audit = audit.read_bytes()
            with self.constants(tmp / "work_logs", task_text, self.old_todo_text(), audit), patch.object(subject, "ORIGINAL_AUDIT_SHA256", sha(original_audit)):
                subject.review(argparse.Namespace(packet=packet_path, packet_sha256=packet_sha, review_output=review_path))
                with (
                    patch.object(subject, "validate_done_live_ownership"),
                    patch.object(subject, "done_live_pane_state", return_value="live"),
                    patch.object(subject, "validate_exited_codex_shell", return_value="c" * 64) as shell,
                    patch.object(subject, "close_done_live_no_mail", return_value=(subject.TARGET, subject.SESSION_ID)) as close,
                ):
                    execute_args = argparse.Namespace(
                        packet=packet_path,
                        packet_sha256=packet_sha,
                        review_report=review_path,
                        review_sha256=sha(review_path.read_bytes()),
                    )
                    subject.execute(execute_args)
                    subject.execute(execute_args)
            self.assertEqual(original_audit, audit.read_bytes())
            self.assertEqual(todo_text, todo.read_text())
            self.assertEqual(task_text, task.read_text())
            self.assertEqual(2, close.call_count)
            self.assertEqual(2, shell.call_count)
            called_args = close.call_args.args[0]
            self.assertEqual(sha(todo_text.encode()), called_args.expected_todo_sha256)
            self.assertEqual("prepared", json.loads(rebound.read_text())["state"])


if __name__ == "__main__":
    unittest.main()
