from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from multiprocessing.connection import Connection
from unittest.mock import patch

from omo_manager import omo_guest_agent as agent
from omo_manager.omo_email_config import GuestHeesOwner, ensure_guest_hees_reply_obligation
from omo_manager.omo_task import manager_delegation, runat_goal_tree_error

TARGET = "guest_hees:3"
PRIMARY_TARGET = "agent_managers:1"


def install_task(root: Path, name: str = agent.TASK_NAME, *, target: str = TARGET, status: str = "running", pending: str = "[]", section: str = "current") -> Path:
    task = root / name
    blocked = "blocked_on: paused by human\n" if status == "blocked" else ""
    task.write_text(
        f"""---
version: v1.0.0
status: {status}
runat: {target}
tool: codex
managerat: {PRIMARY_TARGET}
is_manager: true
{blocked}pending_task_items: {pending}
---
""",
        encoding="utf-8",
    )
    (root / "TODO.md").write_text(f"{section}:\n{name} {target}\n", encoding="utf-8")
    return task


class GuestAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "work_logs"
        self.root.mkdir()
        self.state_dir = self.base / "state"
        workdir = self.base / "guest_hees"
        workdir.mkdir()
        (workdir / "AGENTS.md").write_text("Approved guest instructions.\n", encoding="utf-8")
        (self.root / "TODO.md").write_text("current:\n", encoding="utf-8")
        self.commands: list[list[str]] = []
        self.enterContext(patch("omo_manager.omo_tmux_send.require_sendable_codex_target", return_value=None))
        self.enterContext(patch.object(agent, "exact_tail", return_value=(False, [])))
        self.enterContext(patch.object(agent.subprocess, "run", side_effect=self.run_command))

    def run_command(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[0] == "tmux":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if Path(command[1]).name == "omo_task_status.py":
            install_task(self.root, status=command[-1], section="previous" if command[-1] == "done" else "current")
        else:
            install_task(self.root)
        return subprocess.CompletedProcess(command, 0, stdout="started", stderr="")

    def ensure(self) -> GuestHeesOwner:
        return agent.ensure_guest_hees_agent(self.root, self.state_dir, PRIMARY_TARGET)

    def launched_record(self) -> None:
        self.ensure()
        record_path = self.state_dir / agent.STATE_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["attempted_at_s"] = time.time() - agent.IDLE_GRACE_S - 10
        agent._private_write(record_path, json.dumps(record))

    def test_supported_launch_and_second_request_reuses_exact_owner(self) -> None:
        owner = self.ensure()
        self.assertEqual(GuestHeesOwner(self.root / agent.TASK_NAME, TARGET), owner)
        launch = self.commands[-1]
        self.assertEqual(
            ["--root", str(self.root), "--task-file", agent.TASK_NAME, "--tmux", "--tmux-session", "guest_hees", "--allow-new-tmux-session",
             "--tool", "codex", "--is-manager", "--workdir", str(self.base / "guest_hees"), "--prompt-file", str(self.state_dir / "guest-hees-agent.prompt"),
             "--manager-target", PRIMARY_TARGET],
            launch[2:],
        )
        self.assertEqual(owner, self.ensure())
        self.assertEqual(2, len(self.commands))
        self.assertEqual(0o600, (self.state_dir / agent.STATE_NAME).stat().st_mode & 0o777)
        prompt = (self.state_dir / "guest-hees-agent.prompt").read_text(encoding="utf-8")
        for required in ("AGENTS.md", "email_me.py", "46496337@qq.com", "Sent Mail", "60 seconds", "omo_task_status.py", "--completion-key"):
            self.assertIn(required, prompt)

    def test_reuses_older_owner_without_changing_its_lifecycle(self) -> None:
        task = install_task(self.root, "guest_existing_mgr.md", status="long_running", pending="\n  - unrelated existing work")
        self.assertEqual(GuestHeesOwner(task, TARGET), self.ensure())
        self.assertEqual([], self.commands)
        self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))

    def test_duplicate_and_unsendable_current_owners_never_launch(self) -> None:
        install_task(self.root)
        install_task(self.root, "second.md", target="guest_hees:4")
        (self.root / "TODO.md").write_text(f"current:\n{agent.TASK_NAME} {TARGET}\nsecond.md guest_hees:4\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            self.ensure()
        install_task(self.root)
        with patch("omo_manager.omo_tmux_send.require_sendable_codex_target", side_effect=RuntimeError("missing pane")):
            with self.assertRaisesRegex(RuntimeError, "not sendable"):
                self.ensure()
        self.assertEqual([], self.commands)

    def test_noncurrent_active_indexed_owner_prevents_duplicate_launch(self) -> None:
        install_task(self.root, "guest_existing_mgr.md", section="previous")
        with self.assertRaisesRegex(RuntimeError, "indexed guest custody"):
            self.ensure()
        self.assertEqual([], self.commands)

    def test_untracked_live_guest_codex_prevents_launch(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="guest_hees:8.0\n", stderr="")
        with patch.object(agent.subprocess, "run", return_value=result), patch.object(agent, "exact_tail", return_value=(True, ["Codex"])), patch.object(agent, "target_status", return_value="running"):
            with self.assertRaisesRegex(RuntimeError, "untracked guest pane"):
                self.ensure()
        self.assertFalse((self.state_dir / agent.STATE_NAME).exists())

    def test_paused_legacy_task_is_preserved_when_its_agent_is_absent(self) -> None:
        legacy = install_task(self.root, "guest1269_mgr.md", status="blocked", pending="\n  - unrelated existing work", section="human pending")
        original = legacy.read_bytes()
        self.ensure()
        self.assertEqual(original, legacy.read_bytes())

    def test_paused_legacy_live_codex_prevents_launch(self) -> None:
        install_task(self.root, "guest1269_mgr.md", status="blocked", section="human pending")
        with patch.object(agent, "exact_tail", return_value=(True, ["Codex"])), patch.object(agent, "target_status", return_value="ready"):
            with self.assertRaisesRegex(RuntimeError, "indexed live guest owner"):
                self.ensure()
        self.assertEqual([], self.commands)

    def test_cannot_probe_tmux_is_not_permission_to_launch(self) -> None:
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="permission denied")
        with patch.object(agent.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "cannot verify absent guest panes"):
                self.ensure()
        self.assertFalse((self.state_dir / agent.STATE_NAME).exists())

    def test_failed_launch_and_timeout_cannot_be_blindly_retried(self) -> None:
        for failure in (subprocess.CompletedProcess([], 1, stdout="", stderr="failed"), subprocess.TimeoutExpired("launcher", 180)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as state:
                state_dir = Path(state)

                def fail(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    if command[0] == "tmux":
                        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                with patch.object(agent.subprocess, "run", side_effect=fail) as runner:
                    with self.assertRaisesRegex(RuntimeError, "unverified; no automatic retry"):
                        agent.ensure_guest_hees_agent(self.root, state_dir, PRIMARY_TARGET)
                    with self.assertRaisesRegex(RuntimeError, "prior guest launch is uncertain"):
                        agent.ensure_guest_hees_agent(self.root, state_dir, PRIMARY_TARGET)
                    self.assertEqual(1, sum(call.args[0][0] != "tmux" for call in runner.call_args_list))

    def test_successful_process_without_owner_is_still_uncertain(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="started", stderr="")
        with patch.object(agent.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "unverified"):
                self.ensure()
        with self.assertRaisesRegex(RuntimeError, "prior guest launch is uncertain"):
            self.ensure()

    def test_ready_codex_without_verified_initial_prompt_cannot_bypass_failed_launch(self) -> None:
        def fail_handoff(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            if command[0] != "tmux":
                install_task(self.root, status="long_running")
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="could not capture the new Codex session id; no prompt was sent")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(agent.subprocess, "run", side_effect=fail_handoff):
            with self.assertRaisesRegex(RuntimeError, "unverified; no automatic retry"):
                self.ensure()
            with self.assertRaisesRegex(RuntimeError, "reconcile prompt handoff"):
                self.ensure()
        self.assertEqual(1, sum(command[0] != "tmux" for command in self.commands))

    def test_concurrent_requests_launch_once(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            owners = list(executor.map(lambda _: self.ensure(), range(2)))
        self.assertEqual(owners[0], owners[1])
        self.assertEqual(1, sum(command[0] != "tmux" for command in self.commands))

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires fork")
    def test_separate_watcher_processes_share_one_launch_lock(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        launched = self.base / "single-launch"

        def launch_once(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] != "tmux":
                launched.mkdir()
                time.sleep(0.05)
                install_task(self.root)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        def request(connection: Connection) -> None:
            start.wait(5)
            try:
                connection.send(self.ensure())
            except Exception as exc:
                connection.send(str(exc))
            finally:
                connection.close()

        with patch.object(agent.subprocess, "run", side_effect=launch_once):
            pipes = [context.Pipe(duplex=False) for _ in range(2)]
            workers = [context.Process(target=request, args=(sender,)) for _, sender in pipes]
            for worker in workers:
                worker.start()
            start.set()
            try:
                owners = []
                for receiver, _ in pipes:
                    self.assertTrue(receiver.poll(10))
                    owners.append(receiver.recv())
                self.assertEqual([GuestHeesOwner(self.root / agent.TASK_NAME, TARGET)] * 2, owners)
            finally:
                for worker in workers:
                    worker.join(10)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(5)
                for receiver, sender in pipes:
                    receiver.close()
                    sender.close()
        self.assertTrue(launched.is_dir())

    def test_approved_guest_instructions_required_before_launch(self) -> None:
        (self.base / "guest_hees" / "AGENTS.md").unlink()
        with self.assertRaisesRegex(RuntimeError, "approved guest AGENTS.md is missing"):
            self.ensure()
        self.assertEqual(1, len(self.commands))

    def test_generated_prompt_satisfies_real_launcher_goal_tree_validation(self) -> None:
        delegation = manager_delegation(agent._prompt(self.root, self.state_dir), PRIMARY_TARGET)
        task = f"""---
version: v1.0.0
status: long_running
blocked_on: persistent manager role
runat: {TARGET}
tool: codex
managerat: {PRIMARY_TARGET}
is_manager: true
pending_task_items: []
---
{delegation}
"""
        self.assertEqual("", runat_goal_tree_error(task))

    def test_reaper_leaves_inflight_launch_and_delivered_mail_alone(self) -> None:
        self.ensure()
        with patch.object(agent, "require_codex_target") as ready:
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
            ready.assert_not_called()
        self.launched_record()
        receipts = self.state_dir / "guest-hees-intake-delivered"
        receipts.mkdir()
        (receipts / "fresh.receipt").write_text("fresh intake", encoding="utf-8")
        with patch.object(agent, "require_codex_target") as ready:
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
            ready.assert_not_called()

    def test_reaper_preserves_queues_pending_markers_and_reply_obligations(self) -> None:
        self.launched_record()
        task = self.root / agent.TASK_NAME
        original = task.read_text(encoding="utf-8")
        for text in (original.replace("pending_task_items: []", "pending_task_items:\n  - guest work"), original + "\n(pending)\n"):
            task.write_text(text, encoding="utf-8")
            with patch.object(agent, "require_codex_target") as ready:
                self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
                ready.assert_not_called()
        task.write_text(original, encoding="utf-8")
        self.assertTrue(ensure_guest_hees_reply_obligation(self.state_dir, "mail/inbound.txt", "<guest@example.test>"))
        with patch.object(agent, "require_codex_target") as ready:
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
            ready.assert_not_called()

    def test_reaper_preserves_active_children(self) -> None:
        self.launched_record()
        child = self.root / "child.md"
        child.write_text((self.root / agent.TASK_NAME).read_text(encoding="utf-8").replace(f"managerat: {PRIMARY_TARGET}", f"managerat: {TARGET}").replace("is_manager: true", "is_manager: false"), encoding="utf-8")
        with patch.object(agent, "require_codex_target") as ready:
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
            ready.assert_not_called()

    def test_reaper_uses_supported_done_without_sending_guest_or_primary_mail(self) -> None:
        self.launched_record()
        original = (self.root / agent.TASK_NAME).read_bytes()
        with patch.object(agent, "require_codex_target", return_value="ready"):
            self.assertTrue(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
        self.assertNotEqual(original, (self.root / agent.TASK_NAME).read_bytes())
        command = self.commands[-1]
        self.assertEqual("omo_task_status.py", Path(command[1]).name)
        self.assertEqual([agent.TASK_NAME, "done"], command[-2:])
        self.assertIn("--completion-key", command)
        self.assertEqual(64, len(command[command.index("--completion-key") + 1]))

    def test_completed_launch_reconciles_previous_index_through_supported_helper(self) -> None:
        for status in ("running", "long_running"):
            with self.subTest(status=status):
                self.launched_record()
                with patch.object(agent, "require_codex_target", return_value="ready"):
                    self.assertTrue(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))

                    def restart(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                        self.commands.append(command)
                        if command[0] == "tmux":
                            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                        if Path(command[1]).name == "omo_task.py":
                            install_task(self.root, status=status, section="previous")
                        else:
                            install_task(self.root, status=status)
                        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

                    with patch.object(agent.subprocess, "run", side_effect=restart):
                        self.assertEqual(GuestHeesOwner(self.root / agent.TASK_NAME, TARGET), self.ensure())
                self.assertEqual("omo_task_status.py", Path(self.commands[-1][1]).name)
                self.assertEqual(status, self.commands[-1][-1])

    def test_failed_reaping_preserves_owner_and_does_not_allow_duplicate_launch(self) -> None:
        self.launched_record()
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="stop refused")
        with patch.object(agent, "require_codex_target", return_value="ready"), patch.object(agent.subprocess, "run", return_value=result), self.assertLogs(agent.LOGGER, level="WARNING"):
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
        self.assertEqual(GuestHeesOwner(self.root / agent.TASK_NAME, TARGET), self.ensure())
        self.assertEqual(1, sum(command[0] != "tmux" for command in self.commands))

    def test_supported_closure_relaunch_ignores_coarse_or_old_mtime(self) -> None:
        self.launched_record()
        with patch.object(agent, "require_codex_target", return_value="ready"):
            self.assertTrue(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
        task = self.root / agent.TASK_NAME
        os.utime(task, (1, 1))
        self.assertEqual(GuestHeesOwner(task, TARGET), self.ensure())

    def test_done_task_without_a_launch_snapshot_requires_reconciliation(self) -> None:
        self.launched_record()
        with patch.object(agent, "require_codex_target", return_value="ready"):
            self.assertTrue(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
        state_path = self.state_dir / agent.STATE_NAME
        record = json.loads(state_path.read_text(encoding="utf-8"))
        record.pop("launch_task_sha256")
        agent._private_write(state_path, json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "no changed done-task snapshot"):
            self.ensure()

    def test_reuse_resets_grace_before_pending_transport_can_arrive(self) -> None:
        self.launched_record()
        self.ensure()
        with patch.object(agent, "require_codex_target") as ready:
            self.assertFalse(agent.reap_idle_guest_hees_agent(self.root, self.state_dir))
            ready.assert_not_called()

    def test_corrupt_and_public_state_cannot_authorize_launch(self) -> None:
        self.state_dir.mkdir()
        state = self.state_dir / agent.STATE_NAME
        state.write_text('{"version":1,"status":"uncertain","attempted_at_s":"bad"}', encoding="utf-8")
        os.chmod(state, 0o600)
        with self.assertRaisesRegex(RuntimeError, "requires reconciliation"):
            self.ensure()
        os.chmod(state, 0o644)
        with self.assertRaisesRegex(RuntimeError, "not a private regular file"):
            self.ensure()
        self.assertFalse(any(command[0] != "tmux" for command in self.commands))


if __name__ == "__main__":
    unittest.main()
