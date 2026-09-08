from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_rotate import PaneIdentity
from omo_manager.omo_manager_rotate import RotationError
from omo_manager.omo_manager_rotation_contain import Args
from omo_manager.omo_manager_rotation_contain import AuditBinding
from omo_manager.omo_manager_rotation_contain import ContainmentError
from omo_manager.omo_manager_rotation_contain import FileEvidence
from omo_manager.omo_manager_rotation_contain import LiveBinding
from omo_manager.omo_manager_rotation_contain import ProcessIdentity
from omo_manager.omo_manager_rotation_contain import SessionEvidence
from omo_manager.omo_manager_rotation_contain import TaskBinding
from omo_manager.omo_manager_rotation_contain import WatcherProof
from omo_manager.omo_manager_rotation_contain import audit_binding
from omo_manager.omo_manager_rotation_contain import contain
from omo_manager.omo_manager_rotation_contain import containment_locks
from omo_manager.omo_manager_rotation_contain import guarded_inert_respawn
from omo_manager.omo_manager_rotation_contain import main
from omo_manager.omo_manager_rotation_contain import read_regular_file
from omo_manager.omo_manager_rotation_contain import stable_process_identity
from omo_manager.omo_manager_rotation_contain import verify_contained
from omo_manager.omo_manager_rotation_contain import watcher_proof


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes, mode: int = 0o600) -> str:
    path.write_bytes(data)
    path.chmod(mode)
    return digest(data)


def transcript(session_id: str, cwd: Path, timestamp: str, prompt: str) -> bytes:
    records = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd), "source": "cli", "originator": "codex-tui"},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
    ]
    return b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)


class ManagerRotationContainTests(unittest.TestCase):
    old_session_id = "11111111-1111-7111-8111-111111111111"
    current_session_id = "22222222-2222-7222-8222-222222222222"

    def audit_fixture(self, base: Path) -> tuple[Args, AuditBinding, SessionEvidence]:
        root = base / "work_logs"
        cwd = base / "manager-work"
        state_dir = base / "state"
        rotations = state_dir / "rotations"
        root.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        rotations.mkdir(mode=0o700)
        task = root / "manager.md"
        task.write_text("placeholder\n", encoding="utf-8")
        prompt = rotations / "manager-prompt.txt"
        prompt_text = "fresh manager prompt\n"
        prompt_sha = write_bytes(prompt, prompt_text.encode())
        old_prompt = "old manager prompt"
        old_transcript = base / f"rollout-old-{self.old_session_id}.jsonl"
        old_transcript_sha = write_bytes(
            old_transcript,
            transcript(self.old_session_id, cwd, "2026-09-07T22:00:00Z", old_prompt),
        )
        current_transcript = base / f"rollout-new-{self.current_session_id}.jsonl"
        write_bytes(
            current_transcript,
            transcript(self.current_session_id, cwd, "2026-09-07T23:02:37Z", prompt_text.rstrip("\n")),
        )
        failure_log = state_dir / "pending-watch.log"
        log_text = (
            f"omo_pending_watch: pending watcher already running for root: {root}\n"
            "2026-09-07 16:02:42 -0700 pending watcher duplicate-root refusal; stopping supervisor\n"
        )
        failure_log_sha = write_bytes(failure_log, log_text.encode())
        audit = rotations / "manager-rotation.json"
        audit_payload = {
            "recorded_at": "2026-09-07T23:02:44+00:00",
            "outcome": "failed",
            "status": "",
            "error": f"watcher setup failed after fresh Codex startup: pending watcher supervisor exited; see {failure_log}",
            "target": "manager:0.0",
            "pane": {
                "canonical_target": "manager:0.0",
                "pane_id": "%15",
                "pane_pid": 101,
                "window_id": "@15",
                "working_directory": str(cwd),
            },
            "root": str(root),
            "prompt_path": str(prompt),
            "launch": {
                "launch_argv": [
                    "/bin/bunx",
                    "@openai/codex@latest",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--model",
                    "gpt-5.6-sol",
                    "--config",
                    'model_reasoning_effort="low"',
                    old_prompt,
                ],
                "launch_pid": 102,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "low",
                "source": "inferred",
            },
            "fresh_command": f"exec bunx @openai/codex@latest --model gpt-5.6-sol \"$(cat -- {prompt})\"",
            "prior_pane_output": "prior output\n",
        }
        audit_sha = write_bytes(audit, (json.dumps(audit_payload, sort_keys=True) + "\n").encode())
        args = Args(
            root=root,
            task_file=task,
            target="manager:0",
            failed_audit=audit,
            failed_audit_sha256=audit_sha,
            watcher_failure_log=failure_log,
            watcher_failure_log_sha256=failure_log_sha,
            fresh_prompt_sha256=prompt_sha,
            old_session_transcript=old_transcript,
            old_session_transcript_sha256=old_transcript_sha,
            expected_old_session_id=self.old_session_id,
            current_session_transcript=current_transcript,
            expected_current_session_id=self.current_session_id,
            expected_task_sha256="a" * 64,
            expected_blocker="repair.md",
            expected_manager_target="upper:0",
            expected_pending_items=(),
            expected_old_pane_id="%15",
            expected_old_window_id="@15",
            expected_old_pane_pid=101,
            expected_old_launch_pid=102,
            expected_current_pane_pid=201,
            expected_current_command="bunx",
            protected_targets=("protected:0",),
            receipt_output=rotations / "containment.receipt",
            dry_run=False,
        )
        binding, old_session = audit_binding(args)
        return args, binding, old_session

    def live_fixture(self, args: Args, audit: AuditBinding) -> LiveBinding:
        _, current_file = read_regular_file(args.current_session_transcript, "current transcript")
        pane = PaneIdentity("manager:0.0", "%15", "@15", 201, Path(audit.old_pane.working_directory))
        process = ProcessIdentity(201, 1, "S", 201, 201, 2, 300, "b" * 64)
        session = SessionEvidence(
            current_file,
            self.current_session_id,
            "2026-09-07T23:02:37Z",
            str(pane.working_directory),
            args.fresh_prompt_sha256,
        )
        task_file = FileEvidence(str(args.task_file), 1, 2, 3, 4, 0o600, os.getuid(), args.expected_task_sha256)
        todo_file = FileEvidence(str(args.root / "TODO.md"), 1, 3, 4, 5, 0o600, os.getuid(), "c" * 64)
        task = TaskBinding(
            task_file,
            "blocked",
            args.expected_blocker,
            args.target,
            args.expected_manager_target,
            (),
            "d" * 64,
            todo_file,
            "current",
            "manager.md manager:0",
            "e" * 64,
        )
        watcher = WatcherProof(str(args.root), str(args.root), "/tmp/root.lock", 1, 4, 300, 400, "f" * 64)
        return LiveBinding(pane, process, (process,), session, task, watcher)

    def test_audit_binding_proves_both_old_process_and_old_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, binding, old_session = self.audit_fixture(Path(tmp))

        self.assertEqual(args.failed_audit_sha256, binding.file.sha256)
        self.assertEqual(101, binding.old_pane.pane_pid)
        self.assertEqual(102, binding.old_launch_pid)
        self.assertEqual(self.old_session_id, old_session.session_id)
        self.assertEqual(args.watcher_failure_log_sha256, binding.watcher_failure_log.sha256)

    def test_audit_binding_rejects_unpinned_or_non_watcher_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _, _ = self.audit_fixture(Path(tmp))
            with self.assertRaisesRegex(ContainmentError, "SHA-256"):
                audit_binding(replace(args, failed_audit_sha256="0" * 64))
            write_bytes(args.watcher_failure_log, b"different failure\n")
            changed = replace(args, watcher_failure_log_sha256=digest(b"different failure\n"))
            with self.assertRaisesRegex(ContainmentError, "duplicate-root refusal"):
                audit_binding(changed)

    def test_process_identity_ignores_only_transient_scheduler_state(self) -> None:
        process = ProcessIdentity(201, 1, "S", 201, 201, 2, 300, "b" * 64)

        self.assertEqual(stable_process_identity(process), stable_process_identity(replace(process, state="R")))
        self.assertNotEqual(stable_process_identity(process), stable_process_identity(replace(process, start_ticks=301)))

    def test_dry_run_validates_without_receipt_or_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, old_session = self.audit_fixture(Path(tmp))
            args = replace(args, dry_run=True)
            live = self.live_fixture(args, audit)
            with (
                patch("omo_manager.omo_manager_rotation_contain.audit_binding", return_value=(audit, old_session)),
                patch("omo_manager.omo_manager_rotation_contain.containment_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_rotation_contain.live_binding", return_value=live),
                patch("omo_manager.omo_manager_rotation_contain.revalidate_live_binding", return_value=live),
                patch("omo_manager.omo_manager_rotation_contain.guarded_inert_respawn") as respawn,
            ):
                result, receipt = contain(args)

            self.assertEqual("containment-ready", result)
            self.assertIsNone(receipt)
            self.assertFalse(args.receipt_output.exists())
            respawn.assert_not_called()

    def test_containment_receipt_rejects_post_failure_work_and_records_inert_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, old_session = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            contained = ProcessIdentity(301, 1, "S", 301, 301, 2, 500, "1" * 64)
            with (
                patch("omo_manager.omo_manager_rotation_contain.audit_binding", return_value=(audit, old_session)),
                patch("omo_manager.omo_manager_rotation_contain.containment_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_rotation_contain.live_binding", return_value=live),
                patch("omo_manager.omo_manager_rotation_contain.revalidate_live_binding", return_value=live),
                patch("omo_manager.omo_manager_rotation_contain.guarded_inert_respawn") as respawn,
                patch("omo_manager.omo_manager_rotation_contain.verify_contained", return_value=contained),
                patch("omo_manager.omo_manager_rotation_contain.task_binding", return_value=live.task),
                patch("omo_manager.omo_manager_rotation_contain.watcher_proof", return_value=live.watcher),
            ):
                result, receipt = contain(args)

            self.assertEqual("contained", result)
            self.assertEqual(args.receipt_output, receipt)
            respawn.assert_called_once_with(live, args.expected_current_command)
            payload = json.loads(args.receipt_output.read_text(encoding="utf-8"))
            self.assertEqual("complete", payload["state"])
            self.assertEqual("legacy-audit-not-reconciled-post-failure-work-not-accepted", payload["claim"])
            self.assertEqual("stopped-no-codex-process", payload["dispatch_state"])
            self.assertEqual(301, payload["contained_process"]["pid"])

    def test_containment_preserves_primary_error_when_failure_receipt_cannot_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, old_session = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            error = StringIO()
            with (
                patch("omo_manager.omo_manager_rotation_contain.audit_binding", return_value=(audit, old_session)),
                patch("omo_manager.omo_manager_rotation_contain.containment_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_rotation_contain.live_binding", return_value=live),
                patch(
                    "omo_manager.omo_manager_rotation_contain.revalidate_live_binding",
                    side_effect=ContainmentError("live binding drifted"),
                ),
                patch(
                    "omo_manager.omo_manager_rotation_contain.replace_private_exact",
                    side_effect=OSError("receipt is read-only"),
                ),
                redirect_stderr(error),
                self.assertRaisesRegex(ContainmentError, "live binding drifted"),
            ):
                contain(args)

            self.assertEqual(
                "containment receipt finalization also failed: receipt is read-only\n",
                error.getvalue(),
            )

    def test_containment_preserves_primary_error_when_failure_diagnostic_cannot_write(self) -> None:
        class BrokenStderr:
            def write(self, _text: str) -> int:
                raise OSError("stderr is closed")

            def flush(self) -> None:
                return

        with tempfile.TemporaryDirectory() as tmp:
            args, audit, old_session = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            with (
                patch("omo_manager.omo_manager_rotation_contain.audit_binding", return_value=(audit, old_session)),
                patch("omo_manager.omo_manager_rotation_contain.containment_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_rotation_contain.live_binding", return_value=live),
                patch(
                    "omo_manager.omo_manager_rotation_contain.revalidate_live_binding",
                    side_effect=ContainmentError("live binding drifted"),
                ),
                patch(
                    "omo_manager.omo_manager_rotation_contain.replace_private_exact",
                    side_effect=OSError("receipt is read-only"),
                ),
                patch("omo_manager.omo_manager_rotation_contain.sys.stderr", BrokenStderr()),
                self.assertRaisesRegex(ContainmentError, "live binding drifted"),
            ):
                contain(args)

    def test_inert_respawn_uses_one_exact_tmux_server_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, _ = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            commands: list[list[str]] = []

            def completed(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                accepted = command[-2].rsplit("display-message -p ", 1)[1]
                return subprocess.CompletedProcess(command, 0, accepted + "\n", "")

            with (
                patch("omo_manager.omo_manager_rotation_contain.shutil.which", return_value="/usr/bin/sleep"),
                patch("omo_manager.omo_manager_rotation_contain.subprocess.run", side_effect=completed),
            ):
                guarded_inert_respawn(live, "bunx")

            self.assertEqual(1, len(commands))
            self.assertEqual(["tmux", "if-shell", "-F", "-t", "manager:0.0"], commands[0][:5])
            self.assertIn("#{==:#{pane_id},%15}", commands[0][5])
            self.assertIn("#{==:#{pane_pid},201}", commands[0][5])
            self.assertIn("respawn-pane -k -t %15", commands[0][6])
            self.assertIn("exec /usr/bin/sleep infinity", commands[0][6])

    def test_containment_lock_order_takes_membership_before_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, _ = self.audit_fixture(Path(tmp))
            events: list[str] = []

            @contextmanager
            def recorded(name: str) -> Iterator[None]:
                events.append(name)
                yield

            with (
                patch(
                    "omo_manager.omo_manager_rotation_contain.manager_rotation_lock",
                    side_effect=lambda _state: recorded("rotation"),
                ),
                patch(
                    "omo_manager.omo_manager_rotation_contain.task_target_lock",
                    side_effect=lambda _root, _target: recorded("target"),
                ),
                patch(
                    "omo_manager.omo_manager_rotation_contain.task_file_lock",
                    side_effect=lambda path: recorded(f"file:{path.name}"),
                ),
            ):
                stack = containment_locks(args, Path(audit.state_dir))
                stack.close()

        self.assertEqual(["rotation", "file:.omo-task-membership.lock", "target"], events[:3])

    def test_main_reports_pane_resolution_failure_without_traceback(self) -> None:
        error = StringIO()
        with patch("omo_manager.omo_manager_rotation_contain.parse_args"), patch(
            "omo_manager.omo_manager_rotation_contain.contain", side_effect=RotationError("pane drifted")
        ), redirect_stderr(error):
            result = main([])

        self.assertEqual(2, result)
        self.assertEqual("omo_manager_rotation_contain: pane drifted\n", error.getvalue())

    def test_verify_contained_rejects_surviving_old_dispatch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, _ = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            pane = replace(live.pane, pane_pid=301)
            contained = ProcessIdentity(301, 1, "S", 301, 301, 3, 500, "1" * 64)
            survivor = ProcessIdentity(302, 1, "S", live.pane_process.process_group, live.pane_process.session, 2, 501, "2" * 64)
            with (
                patch("omo_manager.omo_manager_rotation_contain.resolve_exact_pane", return_value=pane),
                patch("omo_manager.omo_manager_rotation_contain.current_command", return_value="sleep"),
                patch("omo_manager.omo_manager_rotation_contain.process_stat", return_value=contained),
                patch("omo_manager.omo_manager_rotation_contain.process_argv", return_value=("/usr/bin/sleep", "infinity")),
                patch(
                    "omo_manager.omo_manager_rotation_contain.process_snapshot",
                    return_value={contained.pid: contained, survivor.pid: survivor},
                ),
            ):
                with self.assertRaisesRegex(ContainmentError, "old successor process group/session/tty"):
                    verify_contained(args, live)

    def test_verify_contained_rejects_inert_sentinel_with_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, audit, _ = self.audit_fixture(Path(tmp))
            live = self.live_fixture(args, audit)
            pane = replace(live.pane, pane_pid=301)
            contained = ProcessIdentity(301, 1, "S", 301, 301, 3, 500, "1" * 64)
            child = ProcessIdentity(302, 301, "S", 301, 301, 3, 501, "2" * 64)
            with (
                patch("omo_manager.omo_manager_rotation_contain.resolve_exact_pane", return_value=pane),
                patch("omo_manager.omo_manager_rotation_contain.current_command", return_value="sleep"),
                patch("omo_manager.omo_manager_rotation_contain.process_stat", return_value=contained),
                patch("omo_manager.omo_manager_rotation_contain.process_argv", return_value=("/usr/bin/sleep", "infinity")),
                patch(
                    "omo_manager.omo_manager_rotation_contain.process_snapshot",
                    return_value={contained.pid: contained, child.pid: child},
                ),
            ):
                with self.assertRaisesRegex(ContainmentError, "unexpectedly has a live child"):
                    verify_contained(args, live)

    def write_fake_watcher_proc(self, proc_root: Path, pid: int, lock_path: Path, root_arg: Path) -> None:
        process_dir = proc_root / str(pid)
        fd_dir = process_dir / "fd"
        fd_dir.mkdir(parents=True)
        fields = ["S", "1", str(pid), str(pid), "0", *("0" for _ in range(14)), "12345"]
        (process_dir / "stat").write_text(f"{pid} (python) {' '.join(fields)}\n", encoding="utf-8")
        watcher_script = Path(__file__).resolve().parents[1] / "omo_pending_watch.py"
        (process_dir / "cmdline").write_bytes(
            b"/usr/bin/python3\0"
            + os.fsencode(watcher_script)
            + b"\0--root\0"
            + os.fsencode(root_arg)
            + b"\0"
        )
        os.link(lock_path, fd_dir / "4")
        info = lock_path.stat()
        lock_record = f"1: FLOCK ADVISORY WRITE {pid} {os.major(info.st_dev):x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
        (proc_root / "locks").write_text(lock_record, encoding="utf-8")

    def test_watcher_proof_requires_live_lock_holder_for_canonical_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            lock_dir = base / "locks"
            lock_dir.mkdir(mode=0o700)
            lock_path = lock_dir / "root.lock"
            lock_path.touch(mode=0o600)
            proc_root = base / "proc"
            proc_root.mkdir()
            pid = os.getpid()
            self.write_fake_watcher_proc(proc_root, pid, lock_path, root)
            lock_fd = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                proof = watcher_proof(root.resolve(), proc_root, lock_path)
                self.assertEqual(pid, proof.pid)
                self.assertEqual(str(root.resolve()), proof.declared_root)
                (proc_root / str(pid) / "cmdline").write_bytes(b"/usr/bin/python3\0/bad/watcher.py\0--root\0/unrelated\0")
                with self.assertRaisesRegex(ContainmentError, "canonical pending-watcher"):
                    watcher_proof(root.resolve(), proc_root, lock_path)
            finally:
                os.close(lock_fd)

    def test_watcher_proof_rejects_stale_unlocked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            lock_dir = base / "locks"
            lock_dir.mkdir(mode=0o700)
            lock_path = lock_dir / "root.lock"
            lock_path.touch(mode=0o600)
            proc_root = base / "proc"
            proc_root.mkdir()
            (proc_root / "locks").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(ContainmentError, "not held"):
                watcher_proof(root.resolve(), proc_root, lock_path)


if __name__ == "__main__":
    unittest.main()
