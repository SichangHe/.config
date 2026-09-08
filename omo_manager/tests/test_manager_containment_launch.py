from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch

from omo_manager.omo_manager_containment_launch import ActiveSuccessor
from omo_manager.omo_manager_containment_launch import Args
from omo_manager.omo_manager_containment_launch import BridgeError
from omo_manager.omo_manager_containment_launch import ContainedLive
from omo_manager.omo_manager_containment_launch import ContainmentReceipt
from omo_manager.omo_manager_containment_launch import ProtectedBinding
from omo_manager.omo_manager_containment_launch import SessionHandle
from omo_manager.omo_manager_containment_launch import active_successor
from omo_manager.omo_manager_containment_launch import bridge_locks
from omo_manager.omo_manager_containment_launch import containment_receipt
from omo_manager.omo_manager_containment_launch import finalize_success
from omo_manager.omo_manager_containment_launch import fresh_launch_command
from omo_manager.omo_manager_containment_launch import fresh_session_handle
from omo_manager.omo_manager_containment_launch import guarded_fresh_launch
from omo_manager.omo_manager_containment_launch import launch_contained_successor
from omo_manager.omo_manager_containment_launch import main
from omo_manager.omo_manager_containment_launch import parse_args
from omo_manager.omo_manager_containment_launch import process_has_failed_successor_identity
from omo_manager.omo_manager_containment_launch import task_binding_matches
from omo_manager.omo_manager_rotate import PaneIdentity
from omo_manager.omo_manager_rotation_contain import CONTAINMENT_VERSION
from omo_manager.omo_manager_rotation_contain import Args as ContainmentArgs
from omo_manager.omo_manager_rotation_contain import FileEvidence
from omo_manager.omo_manager_rotation_contain import ProcessIdentity
from omo_manager.omo_manager_rotation_contain import TaskBinding
from omo_manager.omo_manager_rotation_contain import WatcherProof
from omo_manager.omo_manager_rotation_contain import audit_binding
from omo_manager.omo_manager_rotation_contain import queue_sha256
from omo_manager.omo_manager_rotation_contain import session_evidence
from omo_manager.omo_manager_rotation_contain import sha256
from omo_manager.omo_manager_rotation_contain import task_binding


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes, mode: int = 0o600) -> str:
    _ = path.write_bytes(data)
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


class ManagerContainmentLaunchTests(unittest.TestCase):
    old_session_id: str = "11111111-1111-7111-8111-111111111111"
    failed_session_id: str = "22222222-2222-7222-8222-222222222222"
    fresh_session_id: str = "33333333-3333-7333-8333-333333333333"

    def fixture(self, base: Path) -> tuple[Args, ContainmentReceipt]:
        root = base / "work_logs"
        cwd = base / "manager-work"
        state = base / "state"
        rotations = state / "rotations"
        session_root = base / "sessions"
        session_day = session_root / "2026" / "09" / "07"
        for directory in (root, cwd, state, rotations, session_day):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        task = root / "manager.md"
        task_text = """---
version: v1.0.0
status: blocked
blocked_on: repair.md
runat: manager:0
tool: codex
managerat: upper:0
is_manager: true
pending_task_items: []
---
manager task
"""
        task_sha = write_bytes(task, task_text.encode(), 0o644)
        todo = root / "TODO.md"
        _ = write_bytes(todo, b"current:\nmanager.md manager:0\n\nhuman pending:\n\nlow priority:\n\nprevious:\n", 0o644)
        prompt = rotations / "manager-prompt.txt"
        prompt_text = "fresh manager prompt\n"
        prompt_sha = write_bytes(prompt, prompt_text.encode())
        old_prompt = "old manager prompt"
        old_transcript = session_day / f"rollout-old-{self.old_session_id}.jsonl"
        old_transcript_sha = write_bytes(
            old_transcript,
            transcript(self.old_session_id, cwd, "2026-09-07T22:00:00Z", old_prompt),
        )
        failed_transcript = session_day / f"rollout-failed-{self.failed_session_id}.jsonl"
        failed_transcript_sha = write_bytes(
            failed_transcript,
            transcript(self.failed_session_id, cwd, "2026-09-07T23:02:37Z", prompt_text.rstrip("\n")),
        )
        failure_log = state / "pending-watch.log"
        failure_log_text = (
            f"omo_pending_watch: pending watcher already running for root: {root}\n"
            "2026-09-07 16:02:42 -0700 pending watcher duplicate-root refusal; stopping supervisor\n"
        )
        failure_log_sha = write_bytes(failure_log, failure_log_text.encode())
        bin_dir = base / "bin"
        bin_dir.mkdir(mode=0o700)
        resolved_executable = bin_dir / "bun"
        _ = write_bytes(resolved_executable, b"#!/bin/sh\n", 0o700)
        executable = bin_dir / "bunx"
        executable.symlink_to(resolved_executable)
        audit = rotations / "manager-rotation.json"
        old_launch_argv = (
            str(executable),
            "@openai/codex@latest",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'model_reasoning_effort="low"',
            old_prompt,
        )
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
                "launch_argv": list(old_launch_argv),
                "launch_pid": 102,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "low",
                "source": "inferred",
            },
            "fresh_command": f"exec bunx @openai/codex@latest --model gpt-5.6-sol \"$(cat -- {prompt})\"",
            "prior_pane_output": "prior output\n",
        }
        audit_sha = write_bytes(audit, (json.dumps(audit_payload, sort_keys=True) + "\n").encode())
        containment_args = ContainmentArgs(
            root,
            task,
            "manager:0",
            audit,
            audit_sha,
            failure_log,
            failure_log_sha,
            prompt_sha,
            old_transcript,
            old_transcript_sha,
            self.old_session_id,
            failed_transcript,
            self.failed_session_id,
            task_sha,
            "repair.md",
            "upper:0",
            (),
            "%15",
            "@15",
            101,
            102,
            201,
            "bunx",
            ("protected:0",),
            rotations / "containment.receipt",
            True,
        )
        audit_binding_value, old_session = audit_binding(containment_args)
        failed_session = session_evidence(
            failed_transcript,
            self.failed_session_id,
            prompt_text.rstrip("\n"),
            "failed transcript",
            failed_transcript_sha,
        )
        recorded_task = task_binding(containment_args)
        failed_pane = PaneIdentity("manager:0.0", "%15", "@15", 201, cwd)
        failed_argv = (
            str(executable),
            "@openai/codex@latest",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'model_reasoning_effort="low"',
            prompt_text.rstrip("\n"),
        )
        failed_process = ProcessIdentity(201, 1, "S", 201, 201, 20, 300, sha256("\0".join(failed_argv).encode()))
        sleep_path = str(Path(shutil.which("sleep") or "").resolve())
        sentinel = ProcessIdentity(301, 1, "S", 301, 301, 21, 400, sha256(f"{sleep_path}\0infinity".encode()))
        watcher = WatcherProof(str(root), str(root), str(state / "watcher.lock"), 1, 2, 501, 600, "f" * 64)
        containment_payload: dict[str, object] = {
            "version": CONTAINMENT_VERSION,
            "operation": "contain-failed-manager-rotation",
            "state": "complete",
            "claim": "legacy-audit-not-reconciled-post-failure-work-not-accepted",
            "recorded_at": "2026-09-07T23:03:00+00:00",
            "root": str(root),
            "target": "manager:0",
            "protected_targets": ["protected:0"],
            "protected_targets_sha256": queue_sha256(("protected:0",)),
            "failed_audit": asdict(audit_binding_value),
            "old_session": asdict(old_session),
            "successor_pane": asdict(failed_pane),
            "successor_process": asdict(failed_process),
            "successor_process_tree": [asdict(failed_process)],
            "successor_session": asdict(failed_session),
            "successor_session_final_file": asdict(failed_session.file),
            "task": asdict(recorded_task),
            "watcher": asdict(watcher),
            "contained_process": asdict(sentinel),
            "dispatch_state": "stopped-no-codex-process",
            "error": "",
        }
        failed_audit_payload = cast(dict[str, object], containment_payload["failed_audit"])
        old_pane_payload = cast(dict[str, object], failed_audit_payload["old_pane"])
        old_pane_payload["working_directory"] = str(cwd)
        successor_pane_payload = cast(dict[str, object], containment_payload["successor_pane"])
        successor_pane_payload["working_directory"] = str(cwd)
        receipt_path = rotations / "containment.receipt"
        receipt_sha = write_bytes(receipt_path, (json.dumps(containment_payload, sort_keys=True) + "\n").encode())
        args = Args(
            root,
            task,
            "manager:0",
            receipt_path,
            receipt_sha,
            session_root,
            "%15",
            "@15",
            301,
            400,
            sentinel.argv_sha256,
            "bunx",
            task_sha,
            "repair.md",
            "upper:0",
            (),
            501,
            600,
            ("protected:0",),
            rotations / "containment-successor-ownership.receipt",
            1.0,
            0.01,
            False,
        )
        return args, containment_receipt(args)

    def live_fixture(self, receipt: ContainmentReceipt) -> ContainedLive:
        protected_pane = PaneIdentity("protected:0.0", "%20", "@20", 401, receipt.failed_successor_pane.working_directory)
        protected_process = ProcessIdentity(401, 1, "S", 401, 401, 30, 500, "a" * 64)
        protected = ProtectedBinding("protected:0", protected_pane, "zsh", protected_process)
        pane = replace(receipt.failed_successor_pane, pane_pid=receipt.contained_process.pid)
        return ContainedLive(pane, receipt.contained_process, receipt.task, receipt.watcher, (protected,))

    def active_fixture(self, args: Args, receipt: ContainmentReceipt, live: ContainedLive) -> ActiveSuccessor:
        session_path = args.session_root / "2026" / "09" / "07" / f"rollout-fresh-{self.fresh_session_id}.jsonl"
        prompt = Path(receipt.audit.prompt.path).read_text(encoding="utf-8").rstrip("\n")
        _ = write_bytes(session_path, transcript(self.fresh_session_id, live.pane.working_directory, "2026-09-07T23:04:00Z", prompt))
        session = session_evidence(session_path, self.fresh_session_id, prompt, "fresh transcript")
        process = ProcessIdentity(601, 1, "S", 601, 601, 40, 700, "b" * 64)
        pane = replace(live.pane, pane_pid=process.pid)
        handle = SessionHandle(session, 602, 701, 42)
        return ActiveSuccessor(pane, process, (process,), handle, "running", "c" * 64, live.task, live.watcher, live.protected)

    def test_complete_receipt_reconstructs_all_historical_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))

        self.assertEqual(args.containment_receipt_sha256, receipt.file.sha256)
        self.assertEqual(args.expected_contained_pid, receipt.contained_process.pid)
        self.assertEqual(args.expected_task_sha256, receipt.task.file.sha256)
        self.assertEqual(args.expected_watcher_pid, receipt.watcher.pid)
        self.assertEqual(Path(tmp) / "bin" / "bunx", receipt.launch_argv0)
        self.assertEqual(str((Path(tmp) / "bin" / "bun").resolve()), receipt.launch_executable.path)

    def test_receipt_rejects_semantic_failure_even_with_recomputed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _ = self.fixture(Path(tmp))
            payload = json.loads(args.containment_receipt.read_text(encoding="utf-8"))
            payload["state"] = "prepared"
            changed = (json.dumps(payload, sort_keys=True) + "\n").encode()
            changed_sha = write_bytes(args.containment_receipt, changed)

            with self.assertRaisesRegex(BridgeError, "successful fail-closed containment"):
                containment_receipt(replace(args, containment_receipt_sha256=changed_sha))

    def test_task_binding_tolerates_only_identity_rewrite_with_equal_bytes(self) -> None:
        file = FileEvidence("/work/task.md", 1, 2, 3, 4, 0o644, os.getuid(), "a" * 64)
        todo = FileEvidence("/work/TODO.md", 1, 3, 4, 5, 0o644, os.getuid(), "b" * 64)
        task = TaskBinding(file, "blocked", "repair.md", "manager:0", "upper:0", (), queue_sha256(()), todo, "current", "task.md manager:0", "c" * 64)
        rewritten = replace(task, file=replace(file, inode=8, mtime_ns=9), todo=replace(todo, inode=10, sha256="d" * 64))

        self.assertTrue(task_binding_matches(rewritten, task))
        self.assertFalse(task_binding_matches(replace(rewritten, file=replace(rewritten.file, sha256="e" * 64)), task))

    def test_parse_args_rejects_equivalent_protected_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _ = self.fixture(Path(tmp))
            argv = [
                "--launch-contained-successor",
                "--root", str(args.root),
                "--task-file", str(args.task_file),
                "--target", args.target,
                "--containment-receipt", str(args.containment_receipt),
                "--containment-receipt-sha256", args.containment_receipt_sha256,
                "--session-root", str(args.session_root),
                "--expected-contained-pane-id", args.expected_contained_pane_id,
                "--expected-contained-window-id", args.expected_contained_window_id,
                "--expected-contained-pid", str(args.expected_contained_pid),
                "--expected-contained-start-ticks", str(args.expected_contained_start_ticks),
                "--expected-contained-argv-sha256", args.expected_contained_argv_sha256,
                "--expected-failed-successor-command", args.expected_failed_successor_command,
                "--expected-task-sha256", args.expected_task_sha256,
                "--expected-blocker", args.expected_blocker,
                "--expected-manager-target", args.expected_manager_target,
                "--expect-empty-queue",
                "--expected-watcher-pid", str(args.expected_watcher_pid),
                "--expected-watcher-start-ticks", str(args.expected_watcher_start_ticks),
                "--protected-target", "protected:0",
                "--protected-target", "protected:0.0",
                "--ownership-receipt", str(args.ownership_receipt),
            ]

            with self.assertRaises(SystemExit):
                parse_args(argv)

    def test_reused_tty_is_not_failed_successor_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, receipt = self.fixture(Path(tmp))
            failed = receipt.failed_successor_process
            unrelated = replace(
                failed,
                pid=901,
                process_group=901,
                session=901,
                start_ticks=failed.start_ticks + 1,
            )

        self.assertFalse(process_has_failed_successor_identity(unrelated, receipt))
        self.assertTrue(process_has_failed_successor_identity(replace(unrelated, session=failed.session), receipt))
        self.assertTrue(process_has_failed_successor_identity(replace(unrelated, process_group=failed.process_group), receipt))

    def test_dry_run_revalidates_without_launch_or_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            args = replace(args, dry_run=True)
            live = self.live_fixture(receipt)
            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.containment_receipt",
                    side_effect=(receipt, receipt),
                ),
                patch("omo_manager.omo_manager_containment_launch.bridge_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_launch.contained_live", return_value=live),
                patch(
                    "omo_manager.omo_manager_containment_launch.fresh_launch_command",
                    return_value=("launch", ("/bin/bunx",)),
                ),
                patch("omo_manager.omo_manager_containment_launch.revalidate_contained", return_value=live),
                patch("omo_manager.omo_manager_containment_launch.guarded_fresh_launch") as guarded,
                patch("omo_manager.omo_manager_containment_launch.session_inventory") as inventory,
            ):
                result, output = launch_contained_successor(args)

            self.assertEqual("successor-launch-ready", result)
            self.assertIsNone(output)
            self.assertFalse(args.ownership_receipt.exists())
            guarded.assert_not_called()
            inventory.assert_not_called()

    def test_bridge_locks_membership_then_all_targets_then_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            events: list[str] = []

            @contextmanager
            def recorded(name: str):
                events.append(name)
                yield

            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.manager_rotation_lock",
                    side_effect=lambda _state: recorded("rotation"),
                ),
                patch(
                    "omo_manager.omo_manager_containment_launch.task_target_lock",
                    side_effect=lambda _root, target: recorded(f"target:{target}"),
                ),
                patch(
                    "omo_manager.omo_manager_containment_launch.task_file_lock",
                    side_effect=lambda path: recorded(f"file:{path.name}"),
                ),
            ):
                stack = bridge_locks(args, Path(receipt.audit.state_dir))
                stack.close()

        self.assertEqual(
            ["rotation", "file:.omo-task-membership.lock", "target:manager:0", "target:protected:0"],
            events[:4],
        )

    def test_fresh_command_uses_receipt_executable_and_disables_update_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            command, argv = fresh_launch_command(args, receipt)

        self.assertEqual(str(receipt.launch_argv0), argv[0])
        self.assertIn("check_for_update_on_startup=false", argv)
        self.assertNotIn("resume", argv)
        self.assertIn("OMO_WORK_LOGS_ROOT", command)
        self.assertIn(shlex.quote(receipt.launch_executable.path), command)
        self.assertNotIn("$(cat", command)
        self.assertNotRegex(command, r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{23}")

    def test_fresh_command_is_bound_to_verified_prompt_and_resolved_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            command, _ = fresh_launch_command(args, receipt)
            Path(receipt.audit.prompt.path).write_text("replacement prompt\n", encoding="utf-8")
            replacement = Path(tmp) / "replacement-bun"
            _ = write_bytes(replacement, b"#!/bin/sh\n", 0o700)
            receipt.launch_argv0.unlink()
            receipt.launch_argv0.symlink_to(replacement)

        self.assertIn(shlex.quote("fresh manager prompt"), command)
        self.assertIn(shlex.quote(receipt.launch_executable.path), command)
        self.assertNotIn(str(replacement), command)

    def test_fresh_command_rejects_changed_resolved_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            _ = write_bytes(Path(receipt.launch_executable.path), b"#!/bin/sh\nexit 1\n", 0o700)

            with self.assertRaisesRegex(BridgeError, "executable changed"):
                fresh_launch_command(args, receipt)

    def test_active_successor_accepts_resolved_bun_command_with_bunx_argv0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            active = self.active_fixture(args, receipt, live)
            _, executable_argv = fresh_launch_command(args, receipt)
            prompt = Path(receipt.audit.prompt.path).read_text(encoding="utf-8").rstrip("\n")
            process = active.process
            expected_environment = {
                "OMO_AGENT_TMUX_TARGET": live.pane.canonical_target,
                "OMO_MANAGER_TMUX_TARGET": live.pane.canonical_target,
                "OMO_MANAGER_STATE_DIR": receipt.audit.state_dir,
                "OMO_WORK_LOGS_ROOT": str(args.root),
            }
            with (
                patch("omo_manager.omo_manager_containment_launch.resolve_exact_pane", return_value=active.pane),
                patch("omo_manager.omo_manager_containment_launch.current_command", return_value="bun"),
                patch("omo_manager.omo_manager_containment_launch.process_stat", return_value=process),
                patch(
                    "omo_manager.omo_manager_containment_launch.process_argv",
                    return_value=(*executable_argv, prompt),
                ),
                patch("omo_manager.omo_manager_containment_launch.relevant_environment", return_value=(expected_environment, "c" * 64)),
                patch("omo_manager.omo_manager_containment_launch.process_snapshot", return_value={process.pid: process}),
                patch("omo_manager.omo_manager_containment_launch.process_tree", return_value=(process,)),
                patch("omo_manager.omo_manager_containment_launch.status_classification", return_value="running"),
                patch("omo_manager.omo_manager_containment_launch.fresh_session_handle", return_value=active.session_handle),
                patch("omo_manager.omo_manager_containment_launch.current_task", return_value=live.task),
                patch("omo_manager.omo_manager_containment_launch.watcher_proof", return_value=live.watcher),
                patch("omo_manager.omo_manager_containment_launch.protected_binding", return_value=live.protected[0]),
            ):
                observed = active_successor(
                    args,
                    receipt,
                    live,
                    executable_argv,
                    frozenset(),
                    datetime(2026, 9, 7, 23, 3, tzinfo=timezone.utc),
                )

        self.assertEqual(process, observed.process)

    def test_guarded_launch_uses_one_exact_server_side_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            commands: list[list[str]] = []

            def completed(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                accepted = command[-2].rsplit("display-message -p ", 1)[1]
                return subprocess.CompletedProcess(command, 0, accepted + "\n", "")

            with patch("omo_manager.omo_manager_containment_launch.subprocess.run", side_effect=completed):
                guarded_fresh_launch(live, "exec /bin/bunx")

        self.assertEqual(1, len(commands))
        self.assertEqual(["tmux", "if-shell", "-F", "-t", "manager:0.0"], commands[0][:5])
        self.assertIn("#{==:#{pane_pid},301}", commands[0][5])
        self.assertIn("#{==:#{pane_current_command},sleep}", commands[0][5])
        self.assertIn("respawn-pane -k -t %15", commands[0][6])

    def test_uuid_proof_comes_from_new_transcript_held_by_exact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            active = self.active_fixture(args, receipt, live)
            path = Path(active.session_handle.session.file.path)
            holder = ProcessIdentity(602, 601, "S", 601, 601, 40, 701, "d" * 64)
            prompt = Path(receipt.audit.prompt.path).read_text(encoding="utf-8").rstrip("\n")
            info = path.stat()
            with patch(
                "omo_manager.omo_manager_containment_launch.held_session_paths",
                return_value={(info.st_dev, info.st_ino): (path, holder.pid, 42)},
            ):
                handle = fresh_session_handle(
                    (holder,),
                    args.session_root,
                    frozenset(),
                    prompt,
                    live.pane.working_directory,
                    datetime(2026, 9, 7, 23, 3, tzinfo=timezone.utc),
                    frozenset((self.old_session_id, self.failed_session_id)),
                )

        self.assertIsNotNone(handle)
        assert handle is not None
        self.assertEqual(self.fresh_session_id, handle.session.session_id)
        self.assertEqual(holder.pid, handle.holder_pid)

    def test_uuid_proof_rejects_transcript_reopened_on_a_different_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            active = self.active_fixture(args, receipt, live)
            path = Path(active.session_handle.session.file.path)
            holder = ProcessIdentity(602, 601, "S", 601, 601, 40, 701, "d" * 64)
            prompt = Path(receipt.audit.prompt.path).read_text(encoding="utf-8").rstrip("\n")
            info = path.stat()
            different_identity = (info.st_dev, info.st_ino + 1)
            with patch(
                "omo_manager.omo_manager_containment_launch.held_session_paths",
                return_value={different_identity: (path, holder.pid, 42)},
            ):
                handle = fresh_session_handle(
                    (holder,),
                    args.session_root,
                    frozenset(),
                    prompt,
                    live.pane.working_directory,
                    datetime(2026, 9, 7, 23, 3, tzinfo=timezone.utc),
                    frozenset((self.old_session_id, self.failed_session_id)),
                )

        self.assertIsNone(handle)

    def test_success_writes_complete_sole_owner_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            active = self.active_fixture(args, receipt, live)
            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.containment_receipt",
                    side_effect=(receipt, receipt),
                ),
                patch("omo_manager.omo_manager_containment_launch.bridge_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_launch.contained_live", return_value=live),
                patch(
                    "omo_manager.omo_manager_containment_launch.fresh_launch_command",
                    return_value=("launch", ("/bin/bunx",)),
                ),
                patch("omo_manager.omo_manager_containment_launch.session_inventory", return_value=frozenset()),
                patch("omo_manager.omo_manager_containment_launch.revalidate_contained", return_value=live),
                patch("omo_manager.omo_manager_containment_launch.guarded_fresh_launch"),
                patch("omo_manager.omo_manager_containment_launch.active_successor", return_value=active),
                patch("omo_manager.omo_manager_containment_launch.revalidate_active", return_value=active),
            ):
                result, output = launch_contained_successor(args)

            payload = json.loads(args.ownership_receipt.read_text(encoding="utf-8"))

        self.assertEqual("successor-launched", result)
        self.assertEqual(args.ownership_receipt, output)
        self.assertEqual("complete", payload["state"])
        self.assertEqual(asdict(receipt.launch_executable), payload["launch_executable"])
        self.assertEqual(self.fresh_session_id, payload["fresh_session"]["session_id"])
        self.assertEqual(1, payload["authoritative_owner_count"])
        self.assertEqual(1, payload["authorized_successor_count"])
        self.assertEqual("sole-fresh-successor", payload["ownership_state"])

    def test_post_commit_identity_drift_rolls_back_and_replaces_complete_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            active = self.active_fixture(args, receipt, live)
            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.containment_receipt",
                    side_effect=(receipt, receipt),
                ),
                patch("omo_manager.omo_manager_containment_launch.bridge_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_launch.contained_live", return_value=live),
                patch(
                    "omo_manager.omo_manager_containment_launch.fresh_launch_command",
                    return_value=("launch", ("/bin/bunx",)),
                ),
                patch("omo_manager.omo_manager_containment_launch.session_inventory", return_value=frozenset()),
                patch("omo_manager.omo_manager_containment_launch.revalidate_contained", return_value=live),
                patch("omo_manager.omo_manager_containment_launch.guarded_fresh_launch"),
                patch("omo_manager.omo_manager_containment_launch.active_successor", return_value=active),
                patch(
                    "omo_manager.omo_manager_containment_launch.revalidate_active",
                    side_effect=(active, BridgeError("successor exited after receipt commit")),
                ),
                patch(
                    "omo_manager.omo_manager_containment_launch.rollback_unverified",
                    return_value=live.sentinel,
                ) as rollback,
                self.assertRaisesRegex(BridgeError, "successor exited after receipt commit"),
            ):
                launch_contained_successor(args)

            payload = json.loads(args.ownership_receipt.read_text(encoding="utf-8"))

        rollback.assert_called_once_with(args, receipt, live)
        self.assertEqual("failed", payload["state"])
        self.assertEqual("rolled-back-to-inert", payload["dispatch_state"])
        self.assertEqual(0, payload["authorized_successor_count"])

    def test_uncertain_launch_result_rolls_back_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, receipt = self.fixture(Path(tmp))
            live = self.live_fixture(receipt)
            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.containment_receipt",
                    side_effect=(receipt, receipt),
                ),
                patch("omo_manager.omo_manager_containment_launch.bridge_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_launch.contained_live", return_value=live),
                patch(
                    "omo_manager.omo_manager_containment_launch.fresh_launch_command",
                    return_value=("launch", ("/bin/bunx",)),
                ),
                patch("omo_manager.omo_manager_containment_launch.session_inventory", return_value=frozenset()),
                patch("omo_manager.omo_manager_containment_launch.revalidate_contained", return_value=live),
                patch(
                    "omo_manager.omo_manager_containment_launch.guarded_fresh_launch",
                    side_effect=subprocess.TimeoutExpired("tmux", 10),
                ),
                patch(
                    "omo_manager.omo_manager_containment_launch.rollback_unverified",
                    return_value=live.sentinel,
                ) as rollback,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                launch_contained_successor(args)

            payload = json.loads(args.ownership_receipt.read_text(encoding="utf-8"))

        rollback.assert_called_once_with(args, receipt, live)
        self.assertEqual("failed", payload["state"])
        self.assertEqual("rolled-back-to-inert", payload["dispatch_state"])
        self.assertEqual(0, payload["authorized_successor_count"])

    def test_success_finalization_accepts_exact_complete_bytes_after_reported_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt"
            complete = b"complete\n"
            evidence = FileEvidence(str(path), 1, 2, len(complete), 3, 0o600, os.getuid(), digest(complete))
            with (
                patch(
                    "omo_manager.omo_manager_containment_launch.replace_private_exact",
                    side_effect=OSError("directory fsync failed"),
                ),
                patch(
                    "omo_manager.omo_manager_containment_launch.read_regular_file",
                    return_value=(complete, evidence),
                ),
            ):
                self.assertTrue(finalize_success(path, b"prepared\n", complete))

    def test_main_reports_bridge_error_without_traceback(self) -> None:
        error = StringIO()
        with (
            patch("omo_manager.omo_manager_containment_launch.parse_args"),
            patch(
                "omo_manager.omo_manager_containment_launch.launch_contained_successor",
                side_effect=BridgeError("sentinel drifted"),
            ),
            redirect_stderr(error),
        ):
            result = main([])

        self.assertEqual(2, result)
        self.assertEqual("omo_manager_containment_launch: sentinel drifted\n", error.getvalue())


if __name__ == "__main__":
    unittest.main()
