from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager import omo_task_status as status
from omo_manager.omo_task_metadata import TaskFrontmatterError


@contextmanager
def source2002_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        task = root / "source2002_usage.md"
        todo = root / "TODO.md"
        authority = root / "manager_mail" / "85c5dff58359-2002.txt"
        authority.parent.mkdir()
        transcript = root / "rollout-source2002.jsonl"
        authority_text = "Human correction.\n"
        task_before = (
            "---\nversion: v1.0.0\nstatus: blocked\n"
            "blocked_on: watcher_repair.md: supported no-duplicate exited-shell lifecycle reconciliation\nrunat: config:46\n"
            "tool: codex\nmanagerat: config:27\nis_manager: false\n"
            "pending_task_items: []\nsession_id: 01a0bce1-130b-75b2-8c18-1d6cc531b13b\n---\n"
            '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-2002.txt:3-4">\n'
            f"{authority_text}</human_instruction>\n"
            "Message-ID: <completion-2002@example.test>\n"
            "(verified removed pending item: corrected result sent)\n"
        ).encode()
        todo_before = b"current:\n\nhuman pending:\nsource2002_usage.md config:46\n\nlow priority:\n\nprevious:\n"
        task.write_bytes(task_before)
        todo.write_bytes(todo_before)
        authority.write_bytes((authority_text + "source context\n").encode())
        authority.chmod(0o600)
        transcript_payload = (json.dumps({"type": "session_meta", "payload": {"session_id": "01a0bce1-130b-75b2-8c18-1d6cc531b13b"}}) + "\n").encode()
        transcript.write_bytes(transcript_payload)
        task_after = status.update_frontmatter_status(task_before.decode(), "done", "", root).encode()
        todo_after = status.reconcile_todo_text(root, task, todo_before.decode(), "config:46", "previous", ("human pending",)).encode()
        pane = ("%3761", 1234, "fish", "/tmp", "config:46.0", 77)
        values = {
            "SOURCE1998_ROOT": root,
            "SOURCE2002_TASK": "source2002_usage.md",
            "SOURCE2002_TARGET": "config:46",
            "SOURCE2002_MANAGER": "config:27",
            "SOURCE2002_BLOCKER": "watcher_repair.md: supported no-duplicate exited-shell lifecycle reconciliation",
            "SOURCE2002_CLOSE_KEY": "c" * 64,
            "SOURCE2002_COMPLETION_MESSAGE_ID": "completion-2002@example.test",
            "SOURCE2002_AUTHORITY": "manager_mail/85c5dff58359-2002.txt:3-4",
            "SOURCE2002_AUTHORITY_SHA256": hashlib.sha256(authority.read_bytes()).hexdigest(),
            "SOURCE2002_AUTHORITY_TEXT": authority_text,
            "SOURCE2002_TASK_SHA256": hashlib.sha256(task_before).hexdigest(),
            "SOURCE2002_TODO_SHA256": hashlib.sha256(todo_before).hexdigest(),
            "SOURCE2002_TASK_AFTER_SHA256": hashlib.sha256(task_after).hexdigest(),
            "SOURCE2002_TODO_AFTER_SHA256": hashlib.sha256(todo_after).hexdigest(),
            "SOURCE2002_SESSION_ID": "01a0bce1-130b-75b2-8c18-1d6cc531b13b",
            "SOURCE2002_TRANSCRIPT": transcript,
            "SOURCE2002_TRANSCRIPT_SHA256": hashlib.sha256(transcript_payload).hexdigest(),
            "SOURCE2002_PANE_ID": "%3761",
            "SOURCE2002_PANE_PID": 1234,
            "SOURCE2002_PANE_COMMAND": "fish",
            "SOURCE2002_PANE_CWD": "/tmp",
            "SOURCE2002_PANE_START_TICKS": 77,
            "SOURCE2002_TRANSACTION": ".omo-source2002-reconcile.json",
        }
        with patch.multiple(status, **values), patch.object(status, "source2002_pane_snapshot", return_value=pane):
            args = SimpleNamespace(
                root=root,
                task_file=task,
                completion_key="c" * 64,
                session_id="01a0bce1-130b-75b2-8c18-1d6cc531b13b",
                session_transcript=transcript,
                session_transcript_sha256=hashlib.sha256(transcript_payload).hexdigest(),
                expected_task_sha256=hashlib.sha256(task_before).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo_before).hexdigest(),
            )
            yield root, task, todo, args, task_before, task_after, todo_before, todo_after


class Source2002ReconciliationTests(unittest.TestCase):
    def test_fresh_replay_and_post_success_replay_are_idempotent(self) -> None:
        with source2002_fixture() as (root, task, todo, args, task_before, task_after, todo_before, todo_after):
            status.reconcile_source2002_done(args, task, task_before.decode(), task.stat())
            self.assertEqual(task_after, task.read_bytes())
            self.assertEqual(todo_after, todo.read_bytes())
            self.assertFalse((root / status.SOURCE2002_TRANSACTION).exists())
            status.reconcile_source2002_done(args, task, task_after.decode(), task.stat())

        with source2002_fixture() as (root, task, todo, args, task_before, task_after, todo_before, todo_after):
            record = status.source2002_transaction_record(task_before, task_after, todo_before, todo_after)
            marker = root / status.SOURCE2002_TRANSACTION
            status.source2002_write_transaction(marker, record)
            todo.write_bytes(todo_after)
            status.reconcile_source2002_done(args, task, task_before.decode(), task.stat())
            self.assertEqual(task_after, task.read_bytes())
            self.assertEqual(todo_after, todo.read_bytes())
            self.assertFalse(marker.exists())

    def test_direct_reconciliation_rejects_digest_drift_before_each_state_path(self) -> None:
        with source2002_fixture() as (_root, task, _todo, args, task_before, _task_after, _todo_before, _todo_after):
            args.expected_task_sha256 = "0" * 64
            with self.assertRaisesRegex(TaskFrontmatterError, "fixed task, shell, and transcript"):
                status.reconcile_source2002_done(args, task, task_before.decode(), task.stat())

    def test_parser_requires_the_fixed_preimages_and_transcript(self) -> None:
        with patch.object(status, "SOURCE1998_ROOT", Path("/ssd1/sichangheagent/work_logs")):
            parsed = status.parse_args(
                [
                    "--root",
                    "/ssd1/sichangheagent/work_logs",
                    "--completion-key",
                    status.SOURCE2002_CLOSE_KEY,
                    "--session-transcript",
                    str(status.SOURCE2002_TRANSCRIPT),
                    "--session-transcript-sha256",
                    status.SOURCE2002_TRANSCRIPT_SHA256,
                    "--expected-task-sha256",
                    status.SOURCE2002_TASK_SHA256,
                    "--expected-todo-sha256",
                    status.SOURCE2002_TODO_SHA256,
                    "--reconcile-source-2002-done",
                    "/ssd1/sichangheagent/work_logs/source2002_usage.md",
                ]
            )
        self.assertTrue(parsed.reconcile_source2002_done)
        with self.assertRaises(SystemExit):
            status.parse_args(
                [
                    "--root",
                    "/ssd1/sichangheagent/work_logs",
                    "--completion-key",
                    status.SOURCE2002_CLOSE_KEY,
                    "--session-transcript",
                    str(status.SOURCE2002_TRANSCRIPT),
                    "--session-transcript-sha256",
                    status.SOURCE2002_TRANSCRIPT_SHA256,
                    "--expected-task-sha256",
                    status.SOURCE2002_TASK_SHA256,
                    "--expected-todo-sha256",
                    "0" * 64,
                    "--reconcile-source-2002-done",
                    "/ssd1/sichangheagent/work_logs/source2002_usage.md",
                ]
            )

    def test_all_todo_sections_are_checked_for_duplicate_target_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            todo = "\n".join(
                (
                    "current:",
                    "a.md config:46",
                    "human pending:",
                    "b.md config:46",
                    "low priority:",
                    "c.md config:46",
                    "previous:",
                    "d.md config:46",
                    "",
                )
            )
            self.assertEqual(
                tuple(root / name for name in ("a.md", "b.md", "c.md", "d.md")),
                status.source2002_all_todo_claims(root, "config:46", todo),
            )

    def test_authority_snapshot_rejects_symlink_even_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "mail.txt"
            target.write_text("reviewed", encoding="utf-8")
            link = root / "authority.txt"
            link.symlink_to(target)
            with patch.object(status, "SOURCE2002_AUTHORITY", "authority.txt:1-1"), patch.object(
                status, "SOURCE2002_AUTHORITY_SHA256", hashlib.sha256(b"reviewed").hexdigest()
            ), patch.object(status, "SOURCE2002_AUTHORITY_TEXT", "reviewed"):
                with self.assertRaisesRegex(TaskFrontmatterError, "owner-private"):
                    status.source2002_authority_snapshot(root)

    def test_transcript_snapshot_requires_one_bound_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "rollout-session.jsonl"
            payload = (json.dumps({"type": "session_meta", "payload": {"session_id": "session-2002"}}) + "\n").encode()
            transcript.write_bytes(payload)
            args = SimpleNamespace(session_transcript=transcript)
            with patch.object(status, "SOURCE2002_TRANSCRIPT", transcript), patch.object(
                status, "SOURCE2002_TRANSCRIPT_SHA256", hashlib.sha256(payload).hexdigest()
            ), patch.object(status, "SOURCE2002_SESSION_ID", "session-2002"):
                self.assertEqual(transcript.stat().st_ino, status.source2002_transcript_snapshot(args).st_ino)

    def test_inverse_partial_state_is_rejected_before_any_write(self) -> None:
        before_task = b"task-before"
        after_task = b"task-after"
        before_todo = b"todo-before"
        after_todo = b"todo-after"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "source2002_usage.md"
            todo = root / "TODO.md"
            task.write_bytes(after_task)
            todo.write_bytes(before_todo)
            payload = (before_task, after_task, before_todo, after_todo)
            with patch.object(status, "SOURCE2002_TASK_SHA256", hashlib.sha256(before_task).hexdigest()), patch.object(
                status, "SOURCE2002_TASK_AFTER_SHA256", hashlib.sha256(after_task).hexdigest()
            ), patch.object(status, "SOURCE2002_TODO_SHA256", hashlib.sha256(before_todo).hexdigest()), patch.object(
                status, "SOURCE2002_TODO_AFTER_SHA256", hashlib.sha256(after_todo).hexdigest()
            ), patch.object(status, "source2002_all_todo_claims", return_value=()), patch.object(
                status, "authoritative_active_target_task_paths", return_value=()
            ), patch.object(status, "source2002_authority_snapshot", return_value=(b"authority", task.stat())), patch.object(
                status, "source2002_pane_snapshot", return_value=("%1", 1, "fish", "/tmp", "config:46.0", 1)
            ), patch.object(status, "source2002_transcript_snapshot", return_value=task.stat()):
                with self.assertRaisesRegex(TaskFrontmatterError, "impossible"):
                    status.source2002_final_evidence_pass(SimpleNamespace(), task, todo, payload, allow_partial=True)
            self.assertEqual(after_task, task.read_bytes())
            self.assertEqual(before_todo, todo.read_bytes())
