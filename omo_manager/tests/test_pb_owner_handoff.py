from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager import omo_pb_owner_handoff as handoff
from omo_manager.omo_task_metadata import parse_task_metadata

SESSION_ID = "01a0bb49-9d63-76b2-be18-2bd2897c5270"
COMMIT = "f9893b5eeb4cefa74d147d0642915db251b3bc6d"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_text(
    *,
    status: str,
    manager: str,
    queue: tuple[str, ...],
    blocker: str = "",
    session_id: str = "",
) -> str:
    blocked_on = f"blocked_on: {blocker}\n" if blocker else ""
    pending = "pending_task_items: []\n" if not queue else "pending_task_items:\n" + "".join(f"  - {json.dumps(item)}\n" for item in queue)
    session = f"session_id: {session_id}\n" if session_id else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocked_on}runat: pb:0\ntool: codex\nmanagerat: {manager}\nis_manager: false\n{pending}{session}---\ntask body\n"


class PbOwnerHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "work_logs"
        self.repo = base / "personal_browser_setup"
        self.private = base / "private"
        self.data = base / "data"
        self.root.mkdir(mode=0o700)
        self.repo.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        self.lock = self.data / handoff.PB_HANDOFF_LOCK_NAME
        self.lock.touch(mode=0o600)
        (self.repo / "scripts/news").mkdir(parents=True)
        self.old = task_text(status="blocked", blocker=handoff.SUCCESSOR_TASK, manager=handoff.OLD_MANAGER, queue=())
        self.successor = (
            task_text(
                status="blocked",
                blocker=handoff.SUCCESSOR_BLOCKER,
                manager=handoff.SUCCESSOR_MANAGER,
                queue=("verify service",),
                session_id=SESSION_ID,
            )
            + handoff.SOURCE1982_ENVELOPE
            + "\n"
        )
        self.todo = "current:\nnews_service.md pb:0\npb_news_live.md pb:0\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"
        self.environment = (
            f"PB_WATCHER_DATA_ROOT={self.data}\n"
            f"PB_WATCHER_AGENT_TASK_FILE={self.root / handoff.OLD_TASK}\n"
            "PB_WATCHER_AGENT_TMUX=pb:0.0\n"
            "PB_WATCHER_AGENT_TOOL=codex\n"
            "PB_WATCHER_AGENT_MODEL=gpt-5.6-terra\n"
            "PB_WATCHER_AGENT_REASONING_EFFORT=medium\n"
        )
        self.write(self.root / handoff.OLD_TASK, self.old)
        self.write(self.root / handoff.SUCCESSOR_TASK, self.successor)
        self.write(self.root / "TODO.md", self.todo)
        self.write(self.repo / handoff.ENV_NAME, self.environment)
        authority = self.root / handoff.SOURCE1982_REF
        authority.parent.mkdir(parents=True)
        authority.write_bytes(handoff.SOURCE1982_BYTES)
        wake = self.repo / handoff.PREFLIGHT
        self.write(wake, "#!/bin/sh\nexit 0\n")
        wake.chmod(0o755)
        self.audit = self.private / "handoff.json"
        self.worker = handoff.PanePin(handoff.TARGET, "%10", 1010, 2010)
        self.loop = handoff.PanePin(handoff.LOOP_TARGET, "%11", 1011, 2011)
        self.args = handoff.Args(
            self.root,
            self.repo,
            "execute",
            digest(self.old.encode()),
            digest(self.successor.encode()),
            digest(self.todo.encode()),
            digest(self.environment.encode()),
            COMMIT,
            self.worker,
            SESSION_ID,
            self.loop,
            self.audit,
        )
        self.patches = (
            patch.object(handoff, "TRUSTED_ROOT_DEVICE", self.root.stat().st_dev),
            patch.object(handoff, "TRUSTED_ROOT_INODE", self.root.stat().st_ino),
            patch.object(handoff, "TRUSTED_REPO_DEVICE", self.repo.stat().st_dev),
            patch.object(handoff, "TRUSTED_REPO_INODE", self.repo.stat().st_ino),
            patch.object(handoff, "TRUSTED_DATA_ROOT", self.data),
            patch.object(handoff, "TRUSTED_DATA_DEVICE", self.data.stat().st_dev),
            patch.object(handoff, "TRUSTED_DATA_INODE", self.data.stat().st_ino),
            patch.object(handoff, "OLD_PRE_SHA256", digest(self.old.encode())),
            patch.object(handoff, "SUCCESSOR_PRE_SHA256", digest(self.successor.encode())),
            patch.object(handoff, "ENVIRONMENT_PRE_SHA256", digest(self.environment.encode())),
            patch.object(handoff, "repository_commit", return_value=COMMIT),
            patch.object(handoff, "target_identity", side_effect=self.target_identity),
            patch.object(handoff, "root_session_from_process", return_value=SESSION_ID),
        )
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def target_identity(self, target: str) -> tuple[str, int, int]:
        pin = self.worker if target == handoff.TARGET else self.loop
        return pin.pane_id, pin.pane_pid, pin.pane_start_ticks

    @staticmethod
    def preflight(
        returncode: int = 0,
        stdout: str = handoff.PB_PREFLIGHT_STDOUT,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        if returncode and not stderr:
            stderr = "owner preflight failed\n"
        return subprocess.CompletedProcess([], returncode, stdout if returncode == 0 else "", stderr)

    def prepared(self) -> handoff.Plan:
        return handoff.plan(self.args)

    def test_execute_rebinds_exact_owner_without_runtime_mutation_and_is_idempotent(self) -> None:
        with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            handoff.execute(self.args)
            handoff.execute(self.args)
        old = parse_task_metadata((self.root / handoff.OLD_TASK).read_text(), self.root)
        successor = parse_task_metadata((self.root / handoff.SUCCESSOR_TASK).read_text(), self.root)
        self.assertIsNotNone(old)
        self.assertIsNotNone(successor)
        assert old is not None and successor is not None
        self.assertEqual("done", old.status)
        self.assertEqual(handoff.TARGET, old.runat)
        self.assertEqual((), old.pending_task_items)
        self.assertEqual("long_running", successor.status)
        self.assertEqual(("verify service",), successor.pending_task_items)
        self.assertEqual(SESSION_ID, successor.session_id)
        self.assertEqual(handoff.SUCCESSOR_MANAGER, successor.managerat)
        self.assertEqual(
            self.successor.replace("status: blocked\n", "status: long_running\n").replace(f"blocked_on: {handoff.SUCCESSOR_BLOCKER}\n", ""),
            (self.root / handoff.SUCCESSOR_TASK).read_text(),
        )
        self.assertIn(handoff.HANDOFF_NOTE, (self.root / handoff.OLD_TASK).read_text())
        self.assertEqual(
            "current:\nnews_service.md pb:0\n\nhuman pending:\n\nlow priority:\n\nprevious:\npb_news_live.md pb:0\n",
            (self.root / "TODO.md").read_text(),
        )
        expected_env = self.environment.replace(str(self.root / handoff.OLD_TASK), str(self.root / handoff.SUCCESSOR_TASK))
        self.assertEqual(expected_env, (self.repo / handoff.ENV_NAME).read_text())
        audit = json.loads(self.audit.read_text())
        self.assertEqual("complete", audit["state"])
        self.assertEqual(handoff.binding_id(audit), audit["binding_id"])

    def test_final_preflight_is_owner_check_without_restart_arguments(self) -> None:
        with patch.object(handoff.subprocess, "run", return_value=self.preflight()) as run:
            handoff.execute(self.args)
        run.assert_called_once_with(
            [str(self.repo / handoff.PREFLIGHT), "--owner-preflight"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_final_preflight_failure_preserves_forward_only_images(self) -> None:
        with patch.object(handoff.subprocess, "run", return_value=self.preflight(1)):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("commit-intent", json.loads(self.audit.read_text())["state"])
        prepared = handoff.recover_plan(self.args, json.loads(self.audit.read_text()))
        self.assertEqual(prepared.old_after, (self.root / handoff.OLD_TASK).read_bytes())
        self.assertEqual(prepared.successor_after, (self.root / handoff.SUCCESSOR_TASK).read_bytes())
        self.assertEqual(prepared.todo_after, (self.root / "TODO.md").read_bytes())
        self.assertEqual(prepared.environment_after, (self.repo / handoff.ENV_NAME).read_bytes())
        with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            handoff.execute(self.args)
        self.assertEqual("complete", json.loads(self.audit.read_text())["state"])

    def test_final_runtime_drift_after_intent_never_rolls_back(self) -> None:
        require_runtime = handoff.require_runtime
        calls = 0

        def fail_last_runtime_check(args: handoff.Args) -> None:
            nonlocal calls
            calls += 1
            if calls == 8:
                raise handoff.TaskFrontmatterError("PB runtime changed after final repository check")
            require_runtime(args)

        with patch.object(handoff, "require_runtime", side_effect=fail_last_runtime_check), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("commit-intent", json.loads(self.audit.read_text())["state"])
        prepared = handoff.recover_plan(self.args, json.loads(self.audit.read_text()))
        self.assertEqual(prepared.old_after, (self.root / handoff.OLD_TASK).read_bytes())
        self.assertEqual(prepared.successor_after, (self.root / handoff.SUCCESSOR_TASK).read_bytes())
        self.assertEqual(prepared.todo_after, (self.root / "TODO.md").read_bytes())
        self.assertEqual(prepared.environment_after, (self.repo / handoff.ENV_NAME).read_bytes())

    def test_wrong_success_stdout_preserves_forward_only_images(self) -> None:
        wrong = self.preflight(stdout="owner preflight passed\n")
        with patch.object(handoff.subprocess, "run", return_value=wrong):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("commit-intent", json.loads(self.audit.read_text())["state"])
        prepared = handoff.recover_plan(self.args, json.loads(self.audit.read_text()))
        self.assertEqual(prepared.old_after, (self.root / handoff.OLD_TASK).read_bytes())
        self.assertEqual(prepared.successor_after, (self.root / handoff.SUCCESSOR_TASK).read_bytes())
        self.assertEqual(prepared.todo_after, (self.root / "TODO.md").read_bytes())
        self.assertEqual(prepared.environment_after, (self.repo / handoff.ENV_NAME).read_bytes())

    def test_audit_failure_after_commit_never_rolls_back_and_retry_finishes_forward(self) -> None:
        publish = handoff.durable_replace

        def fail_completion(path: Path, data: bytes, before: object) -> object:
            if path == self.audit and b'"state":"complete"' in data:
                raise OSError("audit storage unavailable")
            return publish(path, data, before)  # type: ignore[arg-type]

        with patch.object(handoff, "durable_replace", side_effect=fail_completion), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])
        prepared = handoff.recover_plan(self.args, json.loads(self.audit.read_text()))
        self.assertEqual(prepared.old_after, (self.root / handoff.OLD_TASK).read_bytes())
        self.assertEqual(prepared.successor_after, (self.root / handoff.SUCCESSOR_TASK).read_bytes())
        self.assertEqual(prepared.todo_after, (self.root / "TODO.md").read_bytes())
        self.assertEqual(prepared.environment_after, (self.repo / handoff.ENV_NAME).read_bytes())
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])

        with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            handoff.execute(self.args)
        self.assertEqual("complete", json.loads(self.audit.read_text())["state"])

    def test_committed_audit_failure_then_failing_preflight_never_rolls_back(self) -> None:
        sync = handoff.fsync_parent
        audit_syncs = 0

        def fail_committed_sync(path: Path) -> None:
            nonlocal audit_syncs
            if path == self.audit:
                audit_syncs += 1
                if audit_syncs == 2:
                    raise OSError("audit directory sync unavailable")
            sync(path)

        with patch.object(handoff, "fsync_parent", side_effect=fail_committed_sync), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])
        with patch.object(handoff.subprocess, "run", return_value=self.preflight(1)):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])
        prepared = handoff.recover_plan(self.args, json.loads(self.audit.read_text()))
        self.assertEqual(prepared.old_after, (self.root / handoff.OLD_TASK).read_bytes())
        self.assertEqual(prepared.successor_after, (self.root / handoff.SUCCESSOR_TASK).read_bytes())
        self.assertEqual(prepared.todo_after, (self.root / "TODO.md").read_bytes())
        self.assertEqual(prepared.environment_after, (self.repo / handoff.ENV_NAME).read_bytes())
        with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            handoff.execute(self.args)
        self.assertEqual("complete", json.loads(self.audit.read_text())["state"])

    def test_audit_path_cannot_overlap_any_locked_managed_file(self) -> None:
        lock_paths = (
            handoff.task_file_lock_path(self.root / ".omo-task-membership.lock"),
            handoff.task_target_lock_path(self.root, handoff.TARGET),
            handoff.tmux_input_lock_path(handoff.TARGET),
        )
        audits = (
            self.root / handoff.SOURCE1982_REF,
            self.root / ".omo-task-membership.lock",
            self.data / handoff.PB_HANDOFF_LOCK_NAME,
            handoff.task_file_lock_path(self.root / handoff.OLD_TASK),
            handoff.task_file_lock_path(self.root / handoff.SUCCESSOR_TASK),
            handoff.task_file_lock_path(self.root / "TODO.md"),
            handoff.task_file_lock_path(self.root / handoff.SOURCE1982_REF),
            handoff.task_file_lock_path(self.repo / handoff.ENV_NAME),
            *lock_paths,
            *(path.parent.parent / "." / path.parent.name / path.name for path in lock_paths),
        )
        for audit in audits:
            with self.subTest(audit=audit):
                changed = replace(self.args, audit=audit)
                with self.assertRaisesRegex(Exception, "overlaps managed state"):
                    handoff.execute(changed)

    def test_complete_audit_directory_sync_failure_is_retried(self) -> None:
        sync = handoff.fsync_parent
        audit_syncs = 0

        def fail_complete_audit_sync(path: Path) -> None:
            nonlocal audit_syncs
            if path == self.audit:
                audit_syncs += 1
                if audit_syncs == 3:
                    raise OSError("audit directory sync unavailable")
            sync(path)

        with patch.object(handoff, "fsync_parent", side_effect=fail_complete_audit_sync), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            with self.assertRaisesRegex(Exception, "forward-only recovery"):
                handoff.execute(self.args)
            self.assertEqual("complete", json.loads(self.audit.read_text())["state"])
            handoff.execute(self.args)
        self.assertEqual(4, audit_syncs)

    def test_execute_recovers_each_monotonic_crash_prefix(self) -> None:
        for prefix in (1, 2, 3, 4):
            with self.subTest(prefix=prefix):
                self.write(self.root / handoff.OLD_TASK, self.old)
                self.write(self.root / handoff.SUCCESSOR_TASK, self.successor)
                self.write(self.root / "TODO.md", self.todo)
                self.write(self.repo / handoff.ENV_NAME, self.environment)
                self.audit.unlink(missing_ok=True)
                prepared = self.prepared()
                self.audit.write_bytes(handoff.canonical_json(handoff.audit_record(prepared, "prepared")))
                self.audit.chmod(0o600)
                if prefix >= 1:
                    (self.root / handoff.OLD_TASK).write_bytes(prepared.old_after)
                if prefix >= 2:
                    (self.root / "TODO.md").write_bytes(prepared.todo_after)
                if prefix >= 3:
                    (self.root / handoff.SUCCESSOR_TASK).write_bytes(prepared.successor_after)
                if prefix >= 4:
                    (self.repo / handoff.ENV_NAME).write_bytes(prepared.environment_after)
                with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
                    handoff.execute(self.args)
                self.assertEqual("complete", json.loads(self.audit.read_text())["state"])

    def test_nonmonotonic_recovery_state_is_rejected_without_mutation(self) -> None:
        prepared = self.prepared()
        self.audit.write_bytes(handoff.canonical_json(handoff.audit_record(prepared, "prepared")))
        self.audit.chmod(0o600)
        (self.repo / handoff.ENV_NAME).write_bytes(prepared.environment_after)
        before = tuple(path.read_bytes() for path in (prepared.old.path, prepared.todo.path, prepared.environment.path))
        with patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            with self.assertRaisesRegex(Exception, "non-monotonic mixed state"):
                handoff.execute(self.args)
        after = tuple(path.read_bytes() for path in (prepared.old.path, prepared.todo.path, prepared.environment.path))
        self.assertEqual(before, after)

    def test_drift_after_audit_reservation_is_never_overwritten(self) -> None:
        reserve = handoff.reserve_private_audit
        managed = (
            self.root / handoff.OLD_TASK,
            self.root / "TODO.md",
            self.root / handoff.SUCCESSOR_TASK,
            self.repo / handoff.ENV_NAME,
        )
        for path in managed:
            with self.subTest(path=path):
                self.write(self.root / handoff.OLD_TASK, self.old)
                self.write(self.root / handoff.SUCCESSOR_TASK, self.successor)
                self.write(self.root / "TODO.md", self.todo)
                self.write(self.repo / handoff.ENV_NAME, self.environment)
                self.audit.unlink(missing_ok=True)

                def reserve_with_drift(audit: Path, text: str) -> None:
                    reserve(audit, text)
                    path.write_text("third-party drift\n")

                with patch.object(handoff, "reserve_private_audit", side_effect=reserve_with_drift), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
                    with self.assertRaisesRegex(Exception, "left the prepared transaction"):
                        handoff.execute(self.args)
                self.assertEqual("third-party drift\n", path.read_text())

    def test_each_publication_fsyncs_its_parent_directory(self) -> None:
        synced: list[Path] = []
        with patch.object(handoff, "fsync_parent", side_effect=lambda path: synced.append(path.parent)), patch.object(handoff.subprocess, "run", return_value=self.preflight()):
            handoff.execute(self.args)
        self.assertEqual(
            [self.root, self.root, self.root, self.repo, self.private, self.private, self.private],
            synced,
        )

    def test_lock_order_is_pb_handoff_then_input_then_lifecycle_then_files(self) -> None:
        events: list[str] = []

        @contextmanager
        def recorded(name: str):
            events.append(f"enter:{name}")
            try:
                yield
            finally:
                events.append(f"exit:{name}")

        with (
            patch.object(handoff, "pb_handoff_lock", side_effect=lambda: recorded("pb-handoff")),
            patch.object(handoff, "tmux_input_lock", side_effect=lambda target: recorded(f"input:{target}")),
            patch.object(handoff, "root_membership_lock", side_effect=lambda root: recorded("membership")),
            patch.object(handoff, "task_target_lock", side_effect=lambda root, target: recorded(f"target:{target}")),
            patch.object(handoff, "task_file_lock", side_effect=lambda path: recorded(f"file:{path}")),
            patch.object(handoff.subprocess, "run", return_value=self.preflight()),
        ):
            handoff.execute(self.args)
        self.assertEqual(
            ["enter:pb-handoff", "enter:input:pb:0", "enter:membership", "enter:target:pb:0"],
            events[:4],
        )
        first_file = next(index for index, event in enumerate(events) if event.startswith("enter:file:"))
        self.assertGreaterEqual(first_file, 4)

    def test_digest_drift_rejects_before_audit_or_mutation(self) -> None:
        changed = replace(self.args, todo_sha256="0" * 64)
        with self.assertRaisesRegex(Exception, "TODO does not match"):
            handoff.execute(changed)
        self.assertFalse(self.audit.exists())
        self.assertEqual(self.old, (self.root / handoff.OLD_TASK).read_text())

    def test_invalid_predecessor_or_extra_owner_is_rejected(self) -> None:
        (self.root / handoff.OLD_TASK).write_text(self.old.replace("pending_task_items: []", "pending_task_items:\n  - retained work"))
        changed = replace(self.args, old_sha256=digest((self.root / handoff.OLD_TASK).read_bytes()))
        with patch.object(handoff, "OLD_PRE_SHA256", changed.old_sha256):
            with self.assertRaisesRegex(Exception, "empty blocked predecessor"):
                handoff.execute(changed)
        self.assertFalse(self.audit.exists())

        (self.root / handoff.OLD_TASK).write_text(self.old)
        self.write(
            self.root / "other.md",
            task_text(status="running", manager=handoff.SUCCESSOR_MANAGER, queue=("other",)),
        )
        with self.assertRaisesRegex(Exception, "exact predecessor/successor pair"):
            handoff.execute(self.args)
        self.assertFalse(self.audit.exists())

    def test_malformed_environment_and_todo_fail_before_audit(self) -> None:
        env_path = self.repo / handoff.ENV_NAME
        env_path.write_text(self.environment + f"{handoff.ENV_TASK_KEY}={self.root / handoff.OLD_TASK}\n")
        changed = replace(self.args, env_sha256=digest(env_path.read_bytes()))
        with patch.object(handoff, "ENVIRONMENT_PRE_SHA256", changed.env_sha256):
            with self.assertRaisesRegex(Exception, "exact singleton owner binding"):
                handoff.execute(changed)
        self.assertFalse(self.audit.exists())

        env_path.write_text(self.environment)
        todo_path = self.root / "TODO.md"
        todo_path.write_text(self.todo.replace("current:\n", "current:\ncurrent:\n", 1))
        changed = replace(self.args, todo_sha256=digest(todo_path.read_bytes()))
        with self.assertRaisesRegex(Exception, "exact predecessor/successor current rows"):
            handoff.execute(changed)
        self.assertFalse(self.audit.exists())

    def test_authority_and_exact_successor_prestate_are_required(self) -> None:
        authority = self.root / handoff.SOURCE1982_REF
        authority.write_bytes(handoff.SOURCE1982_BYTES + b"\n")
        with self.assertRaisesRegex(Exception, "authority bytes changed"):
            handoff.execute(self.args)
        self.assertFalse(self.audit.exists())

        authority.write_bytes(handoff.SOURCE1982_BYTES)
        successor = self.root / handoff.SUCCESSOR_TASK
        successor.write_text(self.successor.replace("status: blocked\n", "status: long_running\n").replace(f"blocked_on: {handoff.SUCCESSOR_BLOCKER}\n", ""))
        changed = replace(self.args, successor_sha256=digest(successor.read_bytes()))
        with patch.object(handoff, "SUCCESSOR_PRE_SHA256", changed.successor_sha256):
            with self.assertRaisesRegex(Exception, "exact active consolidated owner"):
                handoff.execute(changed)
        self.assertFalse(self.audit.exists())

    def test_root_session_binding_rejects_multiple_top_level_rollouts(self) -> None:
        self.patches[-1].stop()
        session_root = self.private / "sessions"
        session_root.mkdir()
        first = "01a0bb49-9d63-76b2-be18-2bd2897c5270"
        second = "01a0bb50-9d63-76b2-be18-2bd2897c5271"
        first_path = session_root / f"rollout-2026-09-19T13-09-18-{first}.jsonl"
        second_path = session_root / f"rollout-2026-09-19T13-09-19-{second}.jsonl"
        tree = (SimpleNamespace(pid=self.worker.pane_pid, start_ticks=self.worker.pane_start_ticks),)
        held = {
            (1, 1): (first_path, self.worker.pane_pid, 10),
            (1, 2): (second_path, self.worker.pane_pid, 11),
        }

        def metadata(path: Path, _identity: tuple[int, int]):
            session = first if path == first_path else second
            value = {
                "timestamp": "2026-09-19T20:09:18Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {
                    "session_id": session,
                    "id": session,
                    "timestamp": "2026-09-19T20:09:18Z",
                    "cwd": str(self.repo),
                    "originator": "codex-tui",
                    "source": "cli",
                    "thread_source": "user",
                },
            }
            return b"{}\n", value

        with (
            patch.object(handoff, "strict_reconciliation_process_tree", return_value=tree),
            patch.object(handoff, "process_held_rollouts", return_value=held),
            patch.object(handoff, "held_rollout_metadata", side_effect=metadata),
        ):
            with self.assertRaisesRegex(Exception, "exactly one top-level Codex rollout"):
                handoff.root_session_from_process(self.worker, self.repo, session_root)

    def test_cli_rejects_any_pb_commit_other_than_reviewed_f989(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            handoff.parse_args(
                [
                    "execute",
                    "--root",
                    str(self.root),
                    "--repo",
                    str(self.repo),
                    "--old-sha256",
                    self.args.old_sha256,
                    "--successor-sha256",
                    self.args.successor_sha256,
                    "--todo-sha256",
                    self.args.todo_sha256,
                    "--env-sha256",
                    self.args.env_sha256,
                    "--repo-commit",
                    "0" * 40,
                    "--pane-id",
                    self.worker.pane_id,
                    "--pane-pid",
                    str(self.worker.pane_pid),
                    "--pane-start-ticks",
                    str(self.worker.pane_start_ticks),
                    "--session-id",
                    SESSION_ID,
                    "--loop-pane-id",
                    self.loop.pane_id,
                    "--loop-pane-pid",
                    str(self.loop.pane_pid),
                    "--loop-pane-start-ticks",
                    str(self.loop.pane_start_ticks),
                    "--audit",
                    str(self.audit),
                ]
            )


if __name__ == "__main__":
    unittest.main()
