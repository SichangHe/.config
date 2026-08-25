from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml
from omo_manager.omo_codex_start import (
    Args,
    HUMAN_RESTART_AUTHORITY_FILE,
    HUMAN_RESTART_AUTHORITY_LINES,
    HUMAN_RESTART_TASK_FILE,
    Pane,
    RECOVERY_EVENT_DIRNAME,
    RECOVERY_RECEIPT_DIRNAME,
    StartError,
    TaskBinding,
    choose_resume_cwd_prompt,
    current_todo_entries,
    consume_recovery_receipt,
    delivery_event_path,
    finish_rotation_audit,
    is_codex_update_prompt,
    launch_command,
    main,
    parse_args,
    post_marker_lines,
    prompt_text,
    query_reconciliation_session_id,
    reconcile_rotation_audit,
    record_recovery_evidence,
    retire_recovery_receipt,
    recovery_issuance_path,
    require_human_restart_authority,
    require_recovery_target,
    require_resume_cwd_prompt,
    require_same_shell,
    require_update_prompt,
    reserve_reconciliation_receipt,
    reserve_rotation_audit,
    resolve_pane,
    resume_cwd_prompt,
    respawn_codex,
    skip_codex_update_prompt,
    start,
    task_path,
    validate_task,
    wait_resume_cwd_recovery,
)
from omo_manager.omo_codex_status import Report
from omo_manager.omo_pending_watch import record_terminal_delivery_failure, terminal_delivery_failure
from omo_manager.omo_pending_watch import PrePasteRejected, try_send_delivery_text


class CodexStartTests(unittest.TestCase):
    SESSION_ID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"

    def recovery_task(self, pane: Pane) -> TaskBinding:
        return TaskBinding(
            False,
            "codex",
            "blocked",
            pane.target,
            "cfg:1",
            ("preserve exact queue",),
            "model capacity",
            hashlib.sha256(b"task bytes").hexdigest(),
        )

    def args(self, root: Path, **changes: object) -> Args:
        values: dict[str, object] = {
            "root": root,
            "task_file": "worker.md",
            "target": "cfg:2",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
            "session_id": "019f670b-6a2f-7463-b9be-9aa6ff0cec43",
            "prompt_file": None,
            "startup_timeout_s": 45.0,
            "confirm_empty_shell": True,
            "dry_run": False,
        }
        values.update(changes)
        return Args(**values)  # type: ignore[arg-type]

    def rotation_args(self, root: Path, **changes: object) -> Args:
        task = root / "worker.md"
        values: dict[str, object] = {
            "session_id": "",
            "rotate_worker": True,
            "expected_task_sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
            "expected_status": "blocked",
            "expected_owner_target": "cfg:1",
            "expected_pending_items": ("preserve exact queue",),
            "protected_targets": ("protected:9",),
            "audit_output": root / "rotation.audit",
        }
        values.update(changes)
        return self.args(root, **values)

    def legacy_rotation_args(self, root: Path, **changes: object) -> Args:
        values: dict[str, object] = {
            "assert_legacy_missing_session_id": True,
            "expected_blocker": "model capacity",
        }
        values.update(changes)
        return self.rotation_args(root, **values)

    def write_failed_rotation_audit(self, root: Path, pane: Pane, **changes: str) -> Path:
        task = root / "worker.md"
        protected = ("protected:9",)
        fields = {
            "operation": "rotate-worker",
            "task-file": "worker.md",
            "target": pane.target,
            "pane-id": pane.pane_id,
            "window-id": pane.window_id,
            "old-pane-pid": "4242",
            "old-command": "bun",
            "old-session-id": self.SESSION_ID,
            "legacy-missing-session-id": "not-asserted",
            "task-sha256": hashlib.sha256(task.read_bytes()).hexdigest(),
            "status": "blocked",
            "blocker-sha256": hashlib.sha256(b"model capacity").hexdigest(),
            "manager-target": "cfg:1",
            "pending-items-sha256": hashlib.sha256(b"preserve exact queue").hexdigest(),
            "protected-target-count": str(len(protected)),
            "protected-targets-sha256": hashlib.sha256("\0".join(protected).encode()).hexdigest(),
            "is-manager": "false",
            "tool": "codex",
            "completion": "unknown-until-finalized",
            "replacement-observed": "true",
            "replacement-target": pane.target,
            "replacement-pane-id": pane.pane_id,
            "replacement-window-id": pane.window_id,
            "replacement-pane-pid": str(pane.pane_pid),
            "replacement-command": pane.command,
            "failure-kind": "post-respawn-new-session-id-capture-failed",
            "final-result": "failed",
        }
        fields.update(changes)
        audit = root / "rotation.audit"
        audit.write_text("\n".join(f"{key}: {value}" for key, value in fields.items()) + "\n", encoding="utf-8")
        audit.chmod(0o600)
        os.setxattr(audit, "user.omo_rotation_reconciliation_eligible_sha256", hashlib.sha256(audit.read_bytes()).hexdigest().encode())
        return audit

    def reconciliation_args(self, root: Path, pane: Pane, **changes: object) -> Args:
        audit = root / "rotation.audit"
        values: dict[str, object] = {
            "session_id": "",
            "confirm_empty_shell": False,
            "reconcile_rotation_audit": True,
            "rotation_audit": audit,
            "expected_rotation_audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
            "reconciliation_receipt": root / "reconciliation.receipt",
            "expected_task_sha256": hashlib.sha256((root / "worker.md").read_bytes()).hexdigest(),
            "expected_status": "blocked",
            "expected_blocker": "model capacity",
            "expected_owner_target": "cfg:1",
            "expected_pending_items": ("preserve exact queue",),
            "protected_targets": ("protected:9",),
            "expected_current_pane_pid": pane.pane_pid,
            "expected_current_command": pane.command,
        }
        values.update(changes)
        return self.args(root, **values)

    def update_prompt_lines(self, session_id: str = SESSION_ID) -> list[str]:
        return [
            f"exec bunx @openai/codex resume {session_id}",
            "✨\u200aUpdate available! 0.146.0 -> 0.146.1",
            "Release notes: https://github.com/openai/codex/releases/latest",
            "› 1. Update now (runs `bun install -g @openai/codex`)",
            "  2. Skip",
            "  3. Skip until next version",
            "Press enter to continue",
        ]

    def resume_cwd_prompt_lines(self, session_directory: Path, current_directory: Path) -> list[str]:
        return [
            "Choose working directory to resume this session",
            "Session = latest cwd recorded in the resumed session",
            "Current = your current working directory",
            f"› 1. Use session directory ({session_directory})",
            f"  2. Use current directory ({current_directory})",
            "  3. Always use session directory",
            "  4. Always use current directory",
            "Press enter to continue",
        ]

    def write_task(
        self,
        root: Path,
        *,
        runat: str = "cfg:2",
        status: str = "blocked",
        manager: bool = False,
        tool: str = "codex",
        pending: list[str] | None = None,
        task_file: str = "worker.md",
    ) -> None:
        fields = {
            "version": "v1.0.0",
            "status": status,
            "blocked_on": "model capacity" if status == "blocked" else None,
            "runat": runat,
            "tool": tool,
            "managerat": "cfg:1",
            "is_manager": manager,
            "pending_task_items": pending if pending is not None else [],
        }
        frontmatter = yaml.safe_dump({key: value for key, value in fields.items() if value is not None}, sort_keys=False)
        frontmatter = frontmatter.replace("pending_task_items:\n- ", "pending_task_items:\n  - ")
        text = "---\n" + frontmatter + "---\n\nGoal.\n"
        path = root / task_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        (root / "TODO.md").write_text(f"current:\n\n{task_file} {runat}\n", encoding="utf-8")

    def write_human_restart_authority(self, root: Path) -> Path:
        mail = root / "manager_mail"
        mail.mkdir(mode=0o700)
        mail.chmod(0o700)
        source = root / HUMAN_RESTART_AUTHORITY_FILE
        source.write_bytes(
            b"Subject: Re: human_task_planner.md: hwl:3 restart needs an email-native authorization\n\n"
            b"Why have you not restarted hwl:3? Do it now\r\n"
        )
        source.chmod(0o600)
        return source

    def write_recovery_receipt(self, root: Path, pane: Pane, lines: list[str], *, observed_at: datetime | None = None, digest_lines: list[str] | None = None) -> Path:
        if (root / "worker.md").is_file():
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                task = validate_task(self.args(root), pane)
        else:
            task = self.recovery_task(pane)
        event_number = len(list((root / RECOVERY_EVENT_DIRNAME).glob("*.event"))) + 1
        event_id = f"11111111-1111-4111-8111-{event_number:012d}"
        identity = subprocess.CompletedProcess([], 0, f"{pane.target}\t{pane.pane_id}\t{pane.window_id}\n", "")
        with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, lines)):
            record_terminal_delivery_failure(root, pane.target, event_id, "status=not_codex")
        receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
        receipt = receipt_dir / f"{event_id}.receipt"
        with patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)), patch("omo_manager.omo_codex_start.verify_same_pane"):
            record_recovery_evidence(root, pane, receipt, event_id, "worker.md", task)
        if observed_at is not None:
            text = receipt.read_text(encoding="utf-8")
            fields = dict(item.split("=", 1) for item in text.strip().split(";"))
            fields["observed_at"] = observed_at.isoformat()
            event_path = root / fields["event_file"]
            event_text = event_path.read_text(encoding="utf-8")
            event_fields = dict(item.split("=", 1) for item in event_text.strip().split(";"))
            event_fields["observed_at"] = observed_at.isoformat()
            updated_event = ";".join(f"{key}={value}" for key, value in event_fields.items()) + "\n"
            event_path.write_text(updated_event, encoding="utf-8")
            event_path.chmod(0o600)
            fields["event_sha256"] = hashlib.sha256(updated_event.encode("utf-8")).hexdigest()
            receipt.write_text(";".join(f"{key}={value}" for key, value in fields.items()) + "\n", encoding="utf-8")
            receipt.chmod(0o600)
        if digest_lines is not None:
            text = receipt.read_text(encoding="utf-8")
            fields = dict(item.split("=", 1) for item in text.strip().split(";"))
            digest = hashlib.sha256("\n".join(digest_lines).encode("utf-8")).hexdigest()
            fields["tail_sha256"] = digest
            receipt.write_text(";".join(f"{key}={value}" for key, value in fields.items()) + "\n", encoding="utf-8")
            receipt.chmod(0o600)
        issuance = recovery_issuance_path(receipt)
        issuance_fields = dict(item.split("=", 1) for item in issuance.read_text(encoding="utf-8").strip().split(";"))
        issuance_fields["receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
        issuance_fields["receipt_inode"] = str(receipt.stat().st_ino)
        issuance.write_text(";".join(f"{key}={value}" for key, value in issuance_fields.items()) + "\n", encoding="utf-8")
        issuance.chmod(0o600)
        return receipt

    def test_resolve_pane_accepts_exact_window_and_pane_targets(self) -> None:
        result = subprocess.CompletedProcess([], 0, "wl:18.0\t%18\t@18\tzsh\t/tmp\t4242\n", "")
        for target in ("wl:18", "wl:18.0"):
            with self.subTest(target=target), patch("omo_manager.omo_codex_start.run", return_value=result):
                self.assertEqual(Pane("wl:18.0", "%18", "@18", "zsh", Path("/tmp"), 4242), resolve_pane(target))

    def test_resolve_pane_rejects_ambiguous_identity_fallbacks(self) -> None:
        mismatches = (("wl:18", "wl:1.0"), ("wl:18", "other:18.0"), ("wl:18.1", "wl:18.0"))
        for requested, resolved in mismatches:
            with self.subTest(requested=requested, resolved=resolved):
                result = subprocess.CompletedProcess([], 0, f"{resolved}\t%18\t@18\tzsh\t/tmp\t4242\n", "")
                with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "does not exist exactly"):
                    resolve_pane(requested)

    def test_resolve_pane_reports_empty_tmux_expansion_as_missing_target(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\t\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "target does not exist: wl:18"):
            resolve_pane("wl:18")

    def test_resolve_pane_rejects_near_empty_tmux_expansion(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\t\n\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "invalid identity"):
            resolve_pane("wl:18")

    def test_validate_task_requires_active_exact_todo_and_same_pane(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                binding = validate_task(self.args(root), pane)
            self.assertFalse(binding.is_manager)
            self.assertEqual("codex", binding.tool)
            self.assertEqual((), binding.pending_task_items)
            (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                with self.assertRaisesRegex(StartError, "TODO `current`"):
                    validate_task(self.args(root), pane)

    def test_validate_task_accepts_relative_subdirectory_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, task_file="202607/worker.md")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                binding = validate_task(self.args(root, task_file="202607/worker.md"), pane)
            self.assertEqual("cfg:2", binding.runat)

    def test_task_path_rejects_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, tempfile.TemporaryDirectory() as raw_external:
            root = Path(raw_root)
            external = Path(raw_external)
            (external / "worker.md").write_text("outside", encoding="utf-8")
            (root / "linked").symlink_to(external, target_is_directory=True)
            cases = (
                "../outside.md",
                str(external / "worker.md"),
                "linked/worker.md",
            )
            for value in cases:
                with self.subTest(value=value), self.assertRaisesRegex(StartError, "under --root"):
                    task_path(root, value)

    def test_current_todo_entries_excludes_other_sections(self) -> None:
        text = "current:\nactive.md cfg:1\nhuman pending:\nhuman.md cfg:2\nprevious:\nold.md cfg:3\n"
        self.assertEqual({"active.md cfg:1"}, current_todo_entries(text))

    def test_validate_task_rejects_done_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="done")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with self.assertRaisesRegex(StartError, "not active"):
                validate_task(self.args(root), pane)

    def test_resume_command_preserves_target_model_effort_and_session(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
        command = launch_command(self.args(root), pane, None, "[marker]")
        self.assertIn("bunx @openai/codex@latest", command)
        self.assertIn("OMO_AGENT_TMUX_TARGET=cfg:2.0", command)
        self.assertIn("--model gpt-5.6-terra", command)
        self.assertIn("model_reasoning_effort=", command)
        self.assertIn("check_for_update_on_startup=false", command)
        self.assertIn("--cd '/tmp/work logs' resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)
        self.assertIn("cd '/tmp/work logs'", command)
        self.assertIn("printf '%s\\n' '[marker]'", command)

    def test_fresh_command_does_not_add_resume_cwd_override(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)

        command = launch_command(replace(self.args(root), session_id=""), pane, None, "[marker]")

        self.assertNotIn("--cd", command)

    def test_exact_codex_update_prompt_recognition(self) -> None:
        lines = self.update_prompt_lines()
        self.assertTrue(is_codex_update_prompt(lines))
        for index, replacement in ((4, "2. Update later"), (6, "Press any key to continue")):
            mismatched = [*lines]
            mismatched[index] = replacement
            with self.subTest(index=index):
                self.assertFalse(is_codex_update_prompt(mismatched))

    def test_update_prompt_mismatch_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        lines = [*self.update_prompt_lines()[:-1], "Press any key to continue"]
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
            patch("omo_manager.omo_codex_start.run") as run,
            self.assertRaisesRegex(StartError, "does not show the exact Codex startup update menu"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_not_called()

    def test_update_prompt_session_mismatch_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        other_session = "019f670b-6a2f-7463-b9be-aaaaaaaaaaaa"
        cases = (
            self.update_prompt_lines(other_session),
            [f"exec bunx @openai/codex resume {self.SESSION_ID}", *self.update_prompt_lines(other_session)],
        )
        for lines in cases:
            with (
                self.subTest(stale_expected=len(lines) > len(self.update_prompt_lines())),
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "not bound to the latest resumed session"),
            ):
                skip_codex_update_prompt(pane, self.SESSION_ID)
            run.assert_not_called()

    def test_update_prompt_pane_change_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        changed = Pane("cfg:2.0", "%9", "@9", "bunx", Path("/tmp"), 4242)
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", side_effect=[pane, changed]),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
            patch("omo_manager.omo_codex_start.run") as run,
            self.assertRaisesRegex(StartError, "identity changed"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_not_called()

    def test_update_prompt_atomic_process_mismatch_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        failed = subprocess.CompletedProcess([], 1, "", "process changed")
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
            patch("omo_manager.omo_codex_start.run", return_value=failed) as run,
            self.assertRaisesRegex(StartError, "failed to skip Codex update.*process changed"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_called_once()

    def test_update_prompt_same_command_different_pid_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        replaced = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 5252)
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", side_effect=[pane, replaced]),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
            patch("omo_manager.omo_codex_start.run") as run,
            self.assertRaisesRegex(StartError, "process identity changed"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_not_called()

    def test_update_prompt_lower_level_helpers_reject_human_target_without_probe(self) -> None:
        pane = Pane("hcfg:2.0", "%2", "@2", "bunx", Path("/tmp"), 4242)
        for helper in (require_update_prompt, skip_codex_update_prompt):
            with (
                self.subTest(helper=helper.__name__),
                patch("omo_manager.omo_codex_start.resolve_pane") as resolve,
                patch("omo_manager.omo_codex_start.exact_tail") as capture,
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "human-owned"),
            ):
                helper(pane, self.SESSION_ID)
            resolve.assert_not_called()
            capture.assert_not_called()
            run.assert_not_called()

    def test_update_prompt_recovery_continues_same_resumed_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root, 4242)
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
                patch("omo_manager.omo_codex_start.run", return_value=completed) as run,
                patch("omo_manager.omo_codex_start.wait_update_recovery", return_value="running") as wait,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.send_shell_command") as launch,
            ):
                result = start(self.args(root, confirm_empty_shell=False, recover_update_prompt=True))
            self.assertEqual("running", result)
            run.assert_called_once_with(
                [
                    "tmux",
                    "if-shell",
                    "-F",
                    "-t",
                    "%2",
                    "#{&&:#{==:#{window_id},@2},#{==:#{session_name}:#{window_index}.#{pane_index},cfg:2.0},#{==:#{pane_pid},4242},#{==:#{pane_current_command},bunx}}",
                    "send-keys -t %2 2 Enter",
                    "run-shell 'exit 1'",
                ]
            )
            wait.assert_called_once_with(pane, 45.0)
            respawn.assert_not_called()
            launch.assert_not_called()

    def test_exact_resume_cwd_prompt_recognition(self) -> None:
        session_directory = Path("/tmp/saved session")
        current_directory = Path("/tmp/current work")
        lines = self.resume_cwd_prompt_lines(session_directory, current_directory)
        self.assertEqual((0, session_directory, current_directory), resume_cwd_prompt(lines))
        cases = (
            (0, "Choose a directory"),
            (3, f"1. Use session directory ({session_directory})"),
            (5, "3. Use session directory forever"),
            (7, "Press any key to continue"),
        )
        for index, replacement in cases:
            mismatched = [*lines]
            mismatched[index] = replacement
            with self.subTest(index=index):
                self.assertIsNone(resume_cwd_prompt(mismatched))

    def test_resume_cwd_prompt_rejects_human_target_without_probe(self) -> None:
        pane = Pane("hcfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        for helper, args in (
            (require_resume_cwd_prompt, (pane, self.SESSION_ID, Path("/tmp/session"))),
            (choose_resume_cwd_prompt, (pane, self.SESSION_ID, Path("/tmp/session"), "current")),
        ):
            with (
                self.subTest(helper=helper.__name__),
                patch("omo_manager.omo_codex_start.resolve_pane") as resolve,
                patch("omo_manager.omo_codex_start.exact_tail") as capture,
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "human-owned"),
            ):
                helper(*args)
            resolve.assert_not_called()
            capture.assert_not_called()
            run.assert_not_called()

    def test_resume_cwd_prompt_requires_exact_paths_and_resumed_session(self) -> None:
        session_directory = Path("/tmp/session")
        current_directory = Path("/tmp/current")
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", current_directory, 4242)
        lines = self.resume_cwd_prompt_lines(session_directory, current_directory)
        cases = (
            (self.SESSION_ID, Path("/tmp/other"), current_directory, "saved session directory"),
            (self.SESSION_ID, session_directory, Path("/tmp/other"), "current working directory"),
            ("019f670b-6a2f-7463-b9be-aaaaaaaaaaaa", session_directory, current_directory, "not resuming"),
        )
        for process_session, expected_session, pane_workdir, error in cases:
            selected = replace(pane, workdir=pane_workdir)
            with (
                self.subTest(error=error),
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=selected),
                patch("omo_manager.omo_codex_start.pane_process_argv", return_value=("bunx", "@openai/codex", "resume", process_session)),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, error),
            ):
                choose_resume_cwd_prompt(selected, self.SESSION_ID, expected_session, "current")
            run.assert_not_called()

    def test_resume_cwd_prompt_rejects_non_codex_bunx_package(self) -> None:
        session_directory = Path("/tmp/session")
        current_directory = Path("/tmp/current")
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", current_directory, 4242)
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
            patch("omo_manager.omo_codex_start.pane_process_argv", return_value=("bunx", "some-other-package", "resume", self.SESSION_ID)),
            patch("omo_manager.omo_codex_start.exact_tail") as capture,
            patch("omo_manager.omo_codex_start.run") as run,
            self.assertRaisesRegex(StartError, "not the supported Codex package invocation"),
        ):
            choose_resume_cwd_prompt(pane, self.SESSION_ID, session_directory, "current")
        capture.assert_not_called()
        run.assert_not_called()

    def test_resume_cwd_prompt_atomically_selects_only_nonpersistent_choice(self) -> None:
        session_directory = Path("/tmp/session")
        current_directory = Path("/tmp/current")
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", current_directory, 4242)
        completed = subprocess.CompletedProcess([], 0, "", "")
        for choice, key in (("session", "1"), ("current", "2")):
            with (
                self.subTest(choice=choice),
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.pane_process_argv", return_value=("bunx", "@openai/codex", "resume", self.SESSION_ID)),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.resume_cwd_prompt_lines(session_directory, current_directory))),
                patch("omo_manager.omo_codex_start.run", return_value=completed) as run,
            ):
                choose_resume_cwd_prompt(pane, self.SESSION_ID, session_directory, choice)
            run.assert_called_once_with(
                [
                    "tmux",
                    "if-shell",
                    "-F",
                    "-t",
                    "%2",
                    "#{&&:#{==:#{window_id},@2},#{==:#{session_name}:#{window_index}.#{pane_index},cfg:2.0},#{==:#{pane_pid},4242},#{==:#{pane_current_command},bunx}}",
                    f"send-keys -t %2 {key} Enter",
                    "run-shell 'exit 1'",
                ]
            )

    def test_resume_cwd_recovery_continues_same_resumed_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            saved = root / "saved"
            saved.mkdir()
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root, 4242)
            args = self.args(
                root,
                confirm_empty_shell=False,
                recover_resume_cwd_prompt=True,
                resume_cwd_choice="current",
                expected_session_directory=saved,
            )
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.pane_process_argv", return_value=("bunx", "@openai/codex", "resume", self.SESSION_ID)),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.resume_cwd_prompt_lines(saved, root))),
                patch("omo_manager.omo_codex_start.run", return_value=subprocess.CompletedProcess([], 0, "", "")),
                patch("omo_manager.omo_codex_start.wait_resume_cwd_recovery", return_value="ready") as wait,
            ):
                self.assertEqual("ready", start(args))
            wait.assert_called_once_with(pane, self.SESSION_ID, 45.0)

    def test_resume_cwd_recovery_tolerates_transient_error_before_ready(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 0.25)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process") as verify,
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(False, [])),
            patch(
                "omo_manager.omo_codex_start.inspect",
                side_effect=(Report("error", ["Selected model is at capacity"]), Report("ready", ["› prompt"])),
            ),
        ):
            self.assertEqual("ready", wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0))
        self.assertEqual(5, verify.call_count)

    def test_resume_cwd_recovery_fails_if_error_persists_until_timeout(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process"),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(False, [])),
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("error", ["error"])),
            self.assertRaisesRegex(StartError, "remained in an error state until timeout"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_resume_cwd_recovery_reports_timeout_if_error_clears_without_ready(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 0.5, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process"),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(False, [])),
            patch("omo_manager.omo_codex_start.inspect", side_effect=(Report("error", ["error"]), Report("not_codex", ["starting"]))),
            self.assertRaisesRegex(StartError, "timed out waiting"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_resume_cwd_recovery_accepts_exact_idle_footer_despite_warning(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        lines = [
            "⚠ MCP client for `codex_apps` failed to start: MCP startup failed: Transport",
            "⚠ MCP startup incomplete (failed: codex_apps)",
            "",
            "› Use /skills to list available skills",
            "",
            "  gpt-5.6-terra medium · /tmp/current",
        ]
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0)),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process") as require_process,
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("error", ["MCP startup incomplete"])),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
        ):
            self.assertEqual("ready", wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0))
        self.assertEqual(3, require_process.call_count)

    def test_resume_cwd_recovery_does_not_mask_capacity_error_with_idle_footer(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        lines = [
            "⚠ Selected model is at capacity. Please try a different model.",
            "",
            "› Use /skills to list available skills",
            "",
            "  gpt-5.6-terra medium · /tmp/current",
        ]
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process"),
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("error", ["capacity"])),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
            self.assertRaisesRegex(StartError, "remained in an error state until timeout"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_resume_cwd_recovery_does_not_mask_unmarked_error_with_allowed_warnings(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        lines = [
            "⚠ MCP client for `codex_apps` failed to start: MCP startup failed: Transport",
            "⚠ MCP startup incomplete (failed: codex_apps)",
            "unrelated error after MCP startup",
            "",
            "› Use /skills to list available skills",
            "",
            "  gpt-5.6-terra medium · /tmp/current",
        ]
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process"),
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("error", ["error"])),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
            self.assertRaisesRegex(StartError, "remained in an error state until timeout"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_resume_cwd_recovery_rejects_unanchored_footer_text(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        lines = [
            "⚠ MCP client for `codex_apps` failed to start: MCP startup failed: Transport",
            "⚠ MCP startup incomplete (failed: codex_apps)",
            "",
            "› Use /skills to list available skills",
            "",
            "not a Codex footer  gpt-5.6-terra medium · /tmp/current",
        ]
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0, 1.0)),
            patch("omo_manager.omo_codex_start.time.sleep"),
            patch("omo_manager.omo_codex_start.require_resume_cwd_process"),
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("error", ["error"])),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)),
            self.assertRaisesRegex(StartError, "remained in an error state until timeout"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_resume_cwd_recovery_rechecks_identity_after_status_capture(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp/current"), 4242)
        with (
            patch("omo_manager.omo_codex_start.time.monotonic", side_effect=(0.0, 0.0)),
            patch(
                "omo_manager.omo_codex_start.require_resume_cwd_process",
                side_effect=(pane, StartError("identity changed after capture")),
            ),
            patch("omo_manager.omo_codex_start.inspect", return_value=Report("ready", ["ready"])),
            self.assertRaisesRegex(StartError, "identity changed after capture"),
        ):
            wait_resume_cwd_recovery(pane, self.SESSION_ID, 1.0)

    def test_fresh_manager_prompt_includes_defaults_manager_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            prompt = root / "prompt.txt"
            prompt.write_text("task prompt\n", encoding="utf-8")
            (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
            with patch("omo_manager.omo_codex_start.WORKER_DEFAULTS", prompt):
                text = prompt_text(self.args(root, session_id="", prompt_file=prompt), True)
            self.assertEqual("task prompt\n\nmanager instructions\n\ntask prompt\n", text)

    def test_fresh_command_quotes_prompt_substitution_as_one_argument(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
        prompt_path = Path("/tmp/prompt with spaces.txt")
        command = launch_command(self.args(root, session_id="", prompt_file=prompt_path), pane, prompt_path, "[marker]")
        self.assertIn("\"$(cat -- '/tmp/prompt with spaces.txt')\"", command)

    def test_launch_command_rejects_programmatic_bare_gpt_5_6_model(self) -> None:
        root = Path("/tmp/work")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
        with self.assertRaisesRegex(StartError, "use gpt-5.6-sol, gpt-5.6-terra, or gpt-5.6-luna"):
            launch_command(replace(self.args(root), model="gpt-5.6"), pane, None, "[marker]")

    def test_restart_running_needs_no_session_or_shell_confirmation(self) -> None:
        args = parse_args(
            [
                "--task-file",
                "worker.md",
                "--target",
                "cfg:2",
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "max",
                "--restart-running",
            ]
        )
        self.assertTrue(args.restart_running)
        self.assertEqual("", args.session_id)

    def test_bare_gpt_5_6_model_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "worker.md",
                    "--target",
                    "cfg:2",
                    "--model",
                    "gpt-5.6",
                    "--reasoning-effort",
                    "max",
                    "--restart-running",
                ]
            )

    def test_update_prompt_recovery_requires_session_without_shell_confirmation(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "max",
            "--recover-update-prompt",
        ]
        with self.assertRaises(SystemExit):
            parse_args(common)
        args = parse_args([*common, "--session-id", self.SESSION_ID])
        self.assertTrue(args.recover_update_prompt)
        self.assertFalse(args.confirm_empty_shell)

    def test_resume_cwd_recovery_requires_all_exact_assertions(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "max",
            "--session-id",
            self.SESSION_ID,
            "--recover-resume-cwd-prompt",
        ]
        with self.assertRaises(SystemExit):
            parse_args(common)
        args = parse_args([*common, "--resume-cwd-choice", "current", "--expected-session-directory", "/tmp/session"])
        self.assertTrue(args.recover_resume_cwd_prompt)
        self.assertEqual("current", args.resume_cwd_choice)
        self.assertEqual(Path("/tmp/session"), args.expected_session_directory)
        for option in ("--resume-cwd-choice", "--expected-session-directory"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parse_args([*common, option, "current" if option.endswith("choice") else "/tmp/session"])

    def test_restart_running_rejects_caller_supplied_session(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "worker.md",
                    "--target",
                    "cfg:2",
                    "--model",
                    "gpt-5.6-terra",
                    "--reasoning-effort",
                    "max",
                    "--restart-running",
                    "--session-id",
                    "019f670b-6a2f-7463-b9be-9aa6ff0cec43",
                ]
            )

    def test_rotate_worker_parse_requires_explicit_lifecycle_queue_protection_and_audit(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "max",
            "--rotate-worker",
        ]
        with self.assertRaises(SystemExit):
            parse_args(common)
        args = parse_args(
            [
                *common,
                "--expected-task-sha256",
                "a" * 64,
                "--expected-status",
                "blocked",
                "--expected-owner-target",
                "cfg:1",
                "--expected-pending-item",
                "preserve exact queue",
                "--protected-target",
                "protected:9",
                "--audit-output",
                "/tmp/rotation.audit",
            ]
        )
        self.assertTrue(args.rotate_worker)
        self.assertEqual(("preserve exact queue",), args.expected_pending_items)
        self.assertEqual(("protected:9",), args.protected_targets)

        legacy = parse_args(
            [
                *common,
                "--expected-task-sha256",
                "a" * 64,
                "--expected-status",
                "blocked",
                "--expected-owner-target",
                "cfg:1",
                "--expected-pending-item",
                "preserve exact queue",
                "--protected-target",
                "protected:9",
                "--audit-output",
                "/tmp/legacy.audit",
                "--assert-legacy-missing-session-id",
                "--expected-blocker",
                "model capacity",
            ]
        )
        self.assertTrue(legacy.assert_legacy_missing_session_id)
        self.assertEqual("model capacity", legacy.expected_blocker)
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    *common,
                    "--expected-task-sha256",
                    "a" * 64,
                    "--expected-status",
                    "blocked",
                    "--expected-owner-target",
                    "cfg:1",
                    "--expected-pending-item",
                    "preserve exact queue",
                    "--protected-target",
                    "protected:9",
                    "--audit-output",
                    "/tmp/legacy.audit",
                    "--assert-legacy-missing-session-id",
                ]
            )

    def test_rotate_worker_starts_fresh_in_same_pane_and_preserves_task_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            task_before = (root / "worker.md").read_bytes()
            initial = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
            rotated_pane = replace(initial, pane_pid=5252)
            rotated = False
            old_session = self.SESSION_ID
            new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"

            def resolve(_target: str) -> Pane:
                return rotated_pane if rotated else initial

            def respawn(_pane: Pane, command: str) -> None:
                nonlocal rotated
                self.assertNotIn(" resume ", command)
                rotated = True

            sessions = iter(((old_session, ""), (old_session, ""), (old_session, ""), (new_session, "")))
            args = self.rotation_args(root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=lambda *_args: next(sessions)),
                patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n") as prompt,
                patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn),
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
            ):
                self.assertEqual("running", start(args))

            self.assertTrue(rotated)
            prompt.assert_called_once()
            self.assertFalse(prompt.call_args.args[1])
            self.assertEqual(task_before, (root / "worker.md").read_bytes())
            audit = root / "rotation.audit"
            self.assertEqual(0o600, audit.stat().st_mode & 0o777)
            self.assertIn(f"new-session-id: {new_session}\nfinal-result: success\n", audit.read_text(encoding="utf-8"))

    def test_rotate_worker_checkpoints_after_wrapper_becomes_supported_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            initial = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
            wrapper = replace(initial, command="zsh", pane_pid=5252)
            replacement = replace(initial, pane_pid=5252)
            rotated = False
            post_respawn_resolutions = 0

            def resolve(_target: str) -> Pane:
                nonlocal post_respawn_resolutions
                if not rotated:
                    return initial
                post_respawn_resolutions += 1
                return wrapper if post_respawn_resolutions == 1 else replacement

            def respawn(_pane: Pane, _command: str) -> None:
                nonlocal rotated
                rotated = True

            sessions = iter(((self.SESSION_ID, ""), (self.SESSION_ID, ""), (self.SESSION_ID, ""), ("119f670b-6a2f-7463-b9be-9aa6ff0cec43", "")))
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=lambda *_args: next(sessions)),
                patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n"),
                patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn),
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
                patch("omo_manager.omo_codex_start.time.sleep"),
            ):
                self.assertEqual("running", start(self.rotation_args(root)))
            audit = (root / "rotation.audit").read_text(encoding="utf-8")
            self.assertIn("replacement-observed: true\nreplacement-target: cfg:2.0\n", audit)
            self.assertIn("replacement-pane-pid: 5252\nreplacement-command: bun\n", audit)

    def test_reconcile_rotation_audit_parser_requires_all_assertions_and_is_mutually_exclusive(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--reconcile-rotation-audit",
            "--rotation-audit",
            "/tmp/rotation.audit",
            "--expected-rotation-audit-sha256",
            "a" * 64,
            "--reconciliation-receipt",
            "/tmp/reconciliation.receipt",
            "--expected-task-sha256",
            "b" * 64,
            "--expected-status",
            "blocked",
            "--expected-blocker",
            "model capacity",
            "--expected-owner-target",
            "cfg:1",
            "--expected-pending-item",
            "preserve exact queue",
            "--protected-target",
            "protected:9",
            "--expected-current-pane-pid",
            "5252",
            "--expected-current-command",
            "bun",
        ]
        args = parse_args(common)
        self.assertTrue(args.reconcile_rotation_audit)
        self.assertEqual(("preserve exact queue",), args.expected_pending_items)
        self.assertEqual(("protected:9",), args.protected_targets)
        for missing in (
            "--rotation-audit",
            "--expected-rotation-audit-sha256",
            "--reconciliation-receipt",
            "--expected-task-sha256",
            "--expected-status",
            "--expected-blocker",
            "--expected-owner-target",
            "--expected-pending-item",
            "--protected-target",
            "--expected-current-pane-pid",
            "--expected-current-command",
        ):
            with self.subTest(missing=missing), self.assertRaises(SystemExit):
                index = common.index(missing)
                parse_args(common[:index] + common[index + 2 :])
        for incompatible in ("--rotate-worker", "--restart-running", "--record-recovery-evidence", "--recover-update-prompt", "--dry-run"):
            with self.subTest(incompatible=incompatible), self.assertRaises(SystemExit):
                parse_args([*common, incompatible])

    def test_reconcile_rotation_audit_records_later_uuid_without_launch_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
            audit = self.write_failed_rotation_audit(root, pane)
            args = self.reconciliation_args(root, pane)
            task_before = (root / "worker.md").read_bytes()
            audit_before = audit.read_bytes()
            audit_stat = audit.stat()
            new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.query_reconciliation_session_id", return_value=new_session) as query,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.send_shell_command") as send,
            ):
                self.assertEqual("rotation-audit-reconciled", reconcile_rotation_audit(args))
            query.assert_called_once()
            self.assertEqual(("%2", 5252, "bun", 240, 10.0), (query.call_args.args[0].pane_id, query.call_args.args[0].pane_pid, query.call_args.args[0].command, *query.call_args.args[1:]))
            respawn.assert_not_called()
            send.assert_not_called()
            self.assertEqual(task_before, (root / "worker.md").read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())
            self.assertEqual((audit_stat.st_dev, audit_stat.st_ino, audit_stat.st_size, audit_stat.st_mtime_ns), (audit.stat().st_dev, audit.stat().st_ino, audit.stat().st_size, audit.stat().st_mtime_ns))
            receipt = (root / "reconciliation.receipt").read_text(encoding="utf-8")
            self.assertIn(f"original-audit-path: {audit}\n", receipt)
            self.assertIn(f"original-audit-content-hex: {audit_before.hex()}\n", receipt)
            self.assertIn(f"old-session-id: {self.SESSION_ID}\ncurrent-pane-pid: 5252\n", receipt)
            self.assertIn(f"current-session-id: {new_session}\nfinal-result: success\n", receipt)

    def test_rotation_marks_only_empty_post_startup_uuid_capture_as_reconcilable(self) -> None:
        for case in ("empty UUID", "startup error", "task or pane error", "same old UUID", "unrelated error"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                initial = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                replacement = replace(initial, pane_pid=5252)
                rotated = False

                def resolve(_target: str) -> Pane:
                    return replacement if rotated else initial

                def respawn(_pane: Pane, _command: str) -> None:
                    nonlocal rotated
                    rotated = True

                final_session = "" if case == "empty UUID" else self.SESSION_ID
                sessions = iter(((self.SESSION_ID, ""), (self.SESSION_ID, ""), (self.SESSION_ID, ""), (final_session, "")))
                startup_error = StartError("startup failed after replacement")
                verification_error: Exception | None = (
                    StartError("task or pane changed after replacement")
                    if case == "task or pane error"
                    else RuntimeError("unrelated post-checkpoint failure")
                    if case == "unrelated error"
                    else None
                )
                with ExitStack() as stack:
                    stack.enter_context(patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve))
                    stack.enter_context(patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])))
                    stack.enter_context(patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=lambda *_args: next(sessions)))
                    stack.enter_context(patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n"))
                    stack.enter_context(patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn))
                    stack.enter_context(patch("omo_manager.omo_codex_start.wait_started", side_effect=startup_error if case == "startup error" else None, return_value="running"))
                    if verification_error is not None:
                        stack.enter_context(patch("omo_manager.omo_codex_start.verify_fresh_rotation", side_effect=verification_error))
                    with self.assertRaises(Exception):
                        start(self.rotation_args(root))

                audit = root / "rotation.audit"
                audit_before = audit.read_bytes()
                marker = b"failure-kind: post-respawn-new-session-id-capture-failed\n"
                self.assertEqual(case == "empty UUID", marker in audit_before)
                args = self.reconciliation_args(root, replacement)
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=replacement),
                    patch("omo_manager.omo_codex_start.query_reconciliation_session_id", return_value="119f670b-6a2f-7463-b9be-9aa6ff0cec43") as query,
                    patch("omo_manager.omo_codex_start.respawn_codex") as reconciliation_respawn,
                    patch("omo_manager.omo_codex_start.send_shell_command") as send,
                ):
                    if case == "empty UUID":
                        self.assertEqual("rotation-audit-reconciled", reconcile_rotation_audit(args))
                        query.assert_called_once()
                    else:
                        with self.assertRaises(StartError):
                            reconcile_rotation_audit(args)
                        query.assert_not_called()
                        self.assertFalse((root / "reconciliation.receipt").exists())
                reconciliation_respawn.assert_not_called()
                send.assert_not_called()
                self.assertEqual(audit_before, audit.read_bytes())

    def test_reconciliation_rejects_ambiguous_historical_failed_audit_without_kind(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
            audit = self.write_failed_rotation_audit(root, pane)
            audit.write_text(
                "\n".join(line for line in audit.read_text(encoding="utf-8").splitlines() if not line.startswith("failure-kind: ")) + "\n",
                encoding="utf-8",
            )
            audit_before = audit.read_bytes()
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.query_reconciliation_session_id") as query,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.send_shell_command") as send,
                self.assertRaises(StartError),
            ):
                reconcile_rotation_audit(self.reconciliation_args(root, pane))
            query.assert_not_called()
            respawn.assert_not_called()
            send.assert_not_called()
            self.assertFalse((root / "reconciliation.receipt").exists())
            self.assertEqual(audit_before, audit.read_bytes())

    def test_eligible_audit_directory_fsync_failure_rolls_back_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
            audit = self.write_failed_rotation_audit(root, pane)
            prepared = "\n".join(
                line
                for line in audit.read_text(encoding="utf-8").splitlines()
                if not line.startswith("failure-kind: ") and not line.startswith("final-result: ")
            ) + "\n"
            audit.write_text(prepared, encoding="utf-8")
            real_fsync = os.fsync
            n_fsync = 0

            def fail_post_replace_directory_fsync(fd: int) -> None:
                nonlocal n_fsync
                n_fsync += 1
                if n_fsync == 2:
                    raise OSError("directory fsync failed")
                real_fsync(fd)

            with patch("omo_manager.omo_codex_start.os.fsync", side_effect=fail_post_replace_directory_fsync), self.assertRaisesRegex(StartError, "could not finalize"):
                finish_rotation_audit(
                    audit,
                    prepared,
                    "failed",
                    failure_kind="post-respawn-new-session-id-capture-failed",
                )

            self.assertEqual(prepared, audit.read_text(encoding="utf-8"))
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.query_reconciliation_session_id") as query,
                self.assertRaises(StartError),
            ):
                reconcile_rotation_audit(self.reconciliation_args(root, pane))
            query.assert_not_called()
            self.assertFalse((root / "reconciliation.receipt").exists())

    def test_eligible_audit_rollback_and_removal_failures_still_lack_commit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
            audit = self.write_failed_rotation_audit(root, pane)
            prepared = "\n".join(
                line
                for line in audit.read_text(encoding="utf-8").splitlines()
                if not line.startswith("failure-kind: ") and not line.startswith("final-result: ")
            ) + "\n"
            audit.write_text(prepared, encoding="utf-8")
            real_fsync = os.fsync
            real_replace = os.replace
            real_unlink = Path.unlink
            n_fsync = 0
            n_replace = 0

            def fail_post_replace_directory_fsync(fd: int) -> None:
                nonlocal n_fsync
                n_fsync += 1
                if n_fsync == 2:
                    raise OSError("directory fsync failed")
                real_fsync(fd)

            def fail_rollback_replace(source: Path, destination: Path) -> None:
                nonlocal n_replace
                n_replace += 1
                if n_replace == 2:
                    raise OSError("rollback replace failed")
                real_replace(source, destination)

            def fail_audit_removal(path: Path, missing_ok: bool = False) -> None:
                if path == audit:
                    raise OSError("audit removal failed")
                real_unlink(path, missing_ok=missing_ok)

            with (
                patch("omo_manager.omo_codex_start.os.fsync", side_effect=fail_post_replace_directory_fsync),
                patch("omo_manager.omo_codex_start.os.replace", side_effect=fail_rollback_replace),
                patch.object(Path, "unlink", autospec=True, side_effect=fail_audit_removal),
                self.assertRaisesRegex(StartError, "could not finalize"),
            ):
                finish_rotation_audit(
                    audit,
                    prepared,
                    "failed",
                    failure_kind="post-respawn-new-session-id-capture-failed",
                )

            self.assertIn("failure-kind: post-respawn-new-session-id-capture-failed", audit.read_text(encoding="utf-8"))
            with self.assertRaises(OSError):
                os.getxattr(audit, "user.omo_rotation_reconciliation_eligible_sha256")
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.query_reconciliation_session_id") as query,
                self.assertRaises(StartError),
            ):
                reconcile_rotation_audit(self.reconciliation_args(root, pane))
            query.assert_not_called()
            self.assertFalse((root / "reconciliation.receipt").exists())

    def test_reconciliation_status_query_guards_every_input_in_tmux_server(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp"), 5252)
        commands: list[list[str]] = []

        def tmux(command: list[str], *, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
            del timeout_s
            commands.append(command)
            if "capture-pane" in command:
                output = "before\n/status\nSession: 119f670b-6a2f-7463-b9be-9aa6ff0cec43\n" if sum("capture-pane" in item for item in commands) == 2 else "before\n"
                return subprocess.CompletedProcess(command, 0, output, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("omo_manager.omo_codex_start.run", side_effect=tmux):
            self.assertEqual("119f670b-6a2f-7463-b9be-9aa6ff0cec43", query_reconciliation_session_id(pane, 240, 10.0))
        input_commands = [command for command in commands if "if-shell" in command]
        self.assertEqual(2, len(input_commands))
        for command in input_commands:
            condition = command[5]
            self.assertIn("#{==:#{pane_id},%2}", condition)
            self.assertIn("#{==:#{window_id},@2}", condition)
            self.assertIn("#{==:#{pane_pid},5252}", condition)
            self.assertIn("#{==:#{pane_current_command},bun}", condition)
        self.assertFalse(any(command[1] in {"paste-buffer", "send-keys"} for command in commands))

    def test_reconcile_lock_order_matches_concurrent_lifecycle_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            todo = root / "TODO.md"
            todo.write_text("current:\n", encoding="utf-8")
            (root / "todo-alias.md").symlink_to(todo)

            @contextmanager
            def target_lock(_root: Path, _target: str):
                yield

            for task_file in ("worker.md", "TODO.md", "todo-alias.md"):
                with self.subTest(task_file=task_file):
                    entered: list[Path] = []

                    @contextmanager
                    def file_lock(path: Path):
                        entered.append(path)
                        yield

                    args = self.args(root, task_file=task_file, session_id="", reconcile_rotation_audit=True)
                    with (
                        patch("omo_manager.omo_codex_start.task_target_lock", side_effect=target_lock),
                        patch("omo_manager.omo_codex_start.task_file_lock", side_effect=file_lock),
                        patch("omo_manager.omo_codex_start.reconcile_rotation_audit_locked", return_value="rotation-audit-reconciled"),
                    ):
                        self.assertEqual("rotation-audit-reconciled", reconcile_rotation_audit(args))
                    task = (root / task_file).resolve()
                    self.assertEqual(sorted({task, todo}, key=lambda path: str(path)), entered)

    def test_reconcile_rotation_audit_refuses_invalid_audit_and_assertion_bindings(self) -> None:
        audit_cases = {
            "operation": {"operation": "other"},
            "final result": {"final-result": "success"},
            "role": {"is-manager": "true"},
            "tool": {"tool": "pcodx"},
            "target": {"target": "cfg:9.0"},
            "pane": {"pane-id": "%9"},
            "window": {"window-id": "@9"},
            "old command": {"old-command": "zsh"},
            "old UUID": {"old-session-id": "unavailable"},
            "protected count": {"protected-target-count": "2"},
            "protected digest": {"protected-targets-sha256": "0" * 64},
            "queue digest": {"pending-items-sha256": "0" * 64},
            "replacement absent": {"replacement-observed": "false"},
            "replacement pid": {"replacement-pane-pid": "6262"},
            "unknown failure kind": {"failure-kind": "unknown"},
            "ineligible failure kind": {"failure-kind": "startup-failed"},
        }
        for case, changes in audit_cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
                audit = self.write_failed_rotation_audit(root, pane, **changes)
                args = self.reconciliation_args(root, pane, expected_rotation_audit_sha256=hashlib.sha256(audit.read_bytes()).hexdigest())
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.query_reconciliation_session_id") as query,
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    patch("omo_manager.omo_codex_start.send_shell_command") as send,
                    self.assertRaises(StartError),
                ):
                    reconcile_rotation_audit(args)
                query.assert_not_called()
                respawn.assert_not_called()
                send.assert_not_called()

        for case in ("absent", "symlink", "nonregular", "permissions", "owner", "hash", "extra field", "duplicate field", "duplicate kind", "CRLF", "missing final LF", "oversized"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
                audit = self.write_failed_rotation_audit(root, pane)
                args = self.reconciliation_args(root, pane)
                uid_patch = patch("omo_manager.omo_codex_start.os.getuid", return_value=os.getuid() + 1) if case == "owner" else patch("omo_manager.omo_codex_start.os.getuid", wraps=os.getuid)
                if case == "absent":
                    audit.unlink()
                elif case == "symlink":
                    target = root / "target.audit"
                    audit.rename(target)
                    audit.symlink_to(target)
                elif case == "nonregular":
                    audit.unlink()
                    audit.mkdir()
                elif case == "permissions":
                    audit.chmod(0o644)
                elif case == "hash":
                    args = self.reconciliation_args(root, pane, expected_rotation_audit_sha256="0" * 64)
                elif case == "extra field":
                    audit.write_text(audit.read_text(encoding="utf-8") + "extra: field\n", encoding="utf-8")
                    args = self.reconciliation_args(root, pane)
                elif case == "duplicate field":
                    audit.write_text(audit.read_text(encoding="utf-8") + "tool: codex\n", encoding="utf-8")
                    args = self.reconciliation_args(root, pane)
                elif case == "duplicate kind":
                    audit.write_text(audit.read_text(encoding="utf-8") + "failure-kind: post-respawn-new-session-id-capture-failed\n", encoding="utf-8")
                    args = self.reconciliation_args(root, pane)
                elif case == "CRLF":
                    audit.write_bytes(audit.read_bytes().replace(b"\n", b"\r\n"))
                    args = self.reconciliation_args(root, pane)
                elif case == "missing final LF":
                    audit.write_bytes(audit.read_bytes().removesuffix(b"\n"))
                    args = self.reconciliation_args(root, pane)
                elif case == "oversized":
                    audit.write_bytes(audit.read_bytes() + b"x" * (64 * 1024))
                    args = self.reconciliation_args(root, pane)
                with uid_patch, patch("omo_manager.omo_codex_start.query_reconciliation_session_id") as query, self.assertRaises((OSError, StartError)):
                    reconcile_rotation_audit(args)
                query.assert_not_called()

    def test_reconcile_rotation_audit_refuses_task_pane_uuid_and_receipt_mismatches(self) -> None:
        for case in (
            "task digest",
            "status",
            "blocker",
            "owner",
            "queue",
            "protected",
            "manager",
            "tool",
            "task target",
            "human",
            "self",
            "pane pid",
            "pane command",
            "unchanged pid",
            "missing UUID",
            "malformed UUID",
            "same UUID",
            "query failure",
            "receipt collision",
            "receipt parent",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
                self.write_failed_rotation_audit(root, pane, **({"old-pane-pid": "5252"} if case == "unchanged pid" else {}))
                changes: dict[str, object] = {}
                if case == "task digest":
                    changes["expected_task_sha256"] = "0" * 64
                elif case == "status":
                    changes["expected_status"] = "running"
                elif case == "blocker":
                    changes["expected_blocker"] = "other"
                elif case == "owner":
                    changes["expected_owner_target"] = "cfg:9"
                elif case == "queue":
                    changes["expected_pending_items"] = ("other",)
                elif case == "protected":
                    changes["protected_targets"] = ("other:9",)
                elif case in {"manager", "tool", "task target"}:
                    self.write_task(root, runat="cfg:9" if case == "task target" else "cfg:2", status="blocked", manager=case == "manager", tool="pcodx" if case == "tool" else "codex", pending=["preserve exact queue"])
                    changes["expected_task_sha256"] = hashlib.sha256((root / "worker.md").read_bytes()).hexdigest()
                elif case == "human":
                    changes["target"] = "hcfg:2"
                elif case == "pane pid":
                    changes["expected_current_pane_pid"] = 9999
                elif case == "pane command":
                    changes["expected_current_command"] = "codex"
                elif case == "receipt collision":
                    (root / "reconciliation.receipt").write_text("occupied", encoding="utf-8")
                elif case == "receipt parent":
                    parent = root / "insecure"
                    parent.mkdir(mode=0o755)
                    changes["reconciliation_receipt"] = parent / "receipt"
                args = self.reconciliation_args(root, pane, **changes)
                session = "" if case == "missing UUID" else "malformed" if case == "malformed UUID" else self.SESSION_ID if case == "same UUID" else "119f670b-6a2f-7463-b9be-9aa6ff0cec43"
                query_error = StartError("status query failed") if case == "query failure" else None
                caller = "%2" if case == "self" else ""
                with (
                    patch.dict(os.environ, {"TMUX_PANE": caller}),
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.query_reconciliation_session_id", return_value=session, side_effect=query_error),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    patch("omo_manager.omo_codex_start.send_shell_command") as send,
                    self.assertRaises((OSError, StartError)),
                ):
                    reconcile_rotation_audit(args)
                respawn.assert_not_called()
                send.assert_not_called()
                if (root / "reconciliation.receipt").is_file() and case != "receipt collision":
                    self.assertIn("final-result: failed", (root / "reconciliation.receipt").read_text(encoding="utf-8"))

    def test_reconcile_rotation_audit_catches_reservation_and_query_races(self) -> None:
        for phase in ("reservation", "query"):
            for resource in ("audit", "task", "todo", "pane"):
                with self.subTest(phase=phase, resource=resource), tempfile.TemporaryDirectory() as raw_root:
                    root = Path(raw_root)
                    self.write_task(root, status="blocked", pending=["preserve exact queue"])
                    initial_pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
                    changed_pane = replace(initial_pane, pane_pid=6262, command="codex")
                    audit = self.write_failed_rotation_audit(root, initial_pane)
                    args = self.reconciliation_args(root, initial_pane)
                    changed = False

                    def mutate() -> None:
                        nonlocal changed
                        changed = True
                        if resource == "audit":
                            replacement = root / "replacement.audit"
                            replacement.write_bytes(audit.read_bytes())
                            replacement.chmod(0o600)
                            os.replace(replacement, audit)
                        elif resource == "task":
                            task = root / "worker.md"
                            task.write_text(task.read_text(encoding="utf-8") + "race\n", encoding="utf-8")
                        elif resource == "todo":
                            todo = root / "TODO.md"
                            todo.write_text(todo.read_text(encoding="utf-8") + "concurrent entry\n", encoding="utf-8")

                    def reserve(path: Path, text: str) -> None:
                        reserve_reconciliation_receipt(path, text)
                        if phase == "reservation":
                            mutate()

                    def query(*_args: object) -> str:
                        if phase == "query":
                            mutate()
                        return "119f670b-6a2f-7463-b9be-9aa6ff0cec43"

                    def resolve(_target: str) -> Pane:
                        return changed_pane if changed and resource == "pane" else initial_pane

                    with (
                        patch("omo_manager.omo_codex_start.reserve_reconciliation_receipt", side_effect=reserve),
                        patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                        patch("omo_manager.omo_codex_start.query_reconciliation_session_id", side_effect=query),
                        patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                        patch("omo_manager.omo_codex_start.send_shell_command") as send,
                        self.assertRaises(StartError),
                    ):
                        reconcile_rotation_audit(args)
                    respawn.assert_not_called()
                    send.assert_not_called()
                    self.assertIn("final-result: failed", (root / "reconciliation.receipt").read_text(encoding="utf-8"))

    def test_reconcile_rotation_audit_finalization_fault_leaves_durable_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 5252)
            audit = self.write_failed_rotation_audit(root, pane)
            audit_before = audit.read_bytes()
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.query_reconciliation_session_id", return_value="119f670b-6a2f-7463-b9be-9aa6ff0cec43"),
                patch("omo_manager.omo_codex_start.finish_reconciliation_receipt", side_effect=StartError("receipt finalization failed")),
                self.assertRaisesRegex(StartError, "receipt finalization failed"),
            ):
                reconcile_rotation_audit(self.reconciliation_args(root, pane))
            receipt = (root / "reconciliation.receipt").read_text(encoding="utf-8")
            self.assertIn("completion: unknown-until-finalized", receipt)
            self.assertNotIn("final-result:", receipt)
            self.assertEqual(audit_before, audit.read_bytes())

    def test_legacy_rotation_starts_fresh_and_records_missing_old_session_causality(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            task_before = (root / "worker.md").read_bytes()
            initial = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
            replacement = replace(initial, pane_pid=5252)
            rotated = False
            new_session = "119f670b-6a2f-7463-b9be-9aa6ff0cec43"

            def resolve(_target: str) -> Pane:
                return replacement if rotated else initial

            def respawn(_pane: Pane, command: str) -> None:
                nonlocal rotated
                self.assertNotIn(" resume ", command)
                rotated = True

            sessions = iter((("", "legacy"), ("", "legacy"), ("", "legacy"), (new_session, "")))
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=lambda *_args: next(sessions)),
                patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n") as prompt,
                patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn),
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
            ):
                self.assertEqual("running", start(self.legacy_rotation_args(root)))

            prompt.assert_called_once()
            self.assertFalse(prompt.call_args.args[1])
            self.assertEqual(task_before, (root / "worker.md").read_bytes())
            audit = (root / "rotation.audit").read_text(encoding="utf-8")
            self.assertIn("old-session-id: unavailable-asserted-legacy\nlegacy-missing-session-id: asserted-and-observed\n", audit)
            self.assertIn(f"new-session-id: {new_session}\nfinal-result: success\n", audit)

    def test_legacy_rotation_refuses_false_or_missing_legacy_assertion(self) -> None:
        for case in ("assertion absent", "UUID recoverable"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                args = self.rotation_args(root) if case == "assertion absent" else self.legacy_rotation_args(root)
                session = "" if case == "assertion absent" else self.SESSION_ID
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.query_status_session_id", return_value=(session, "")),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    self.assertRaisesRegex(StartError, "could not capture|assertion is false"),
                ):
                    start(args)
                respawn.assert_not_called()
                self.assertFalse((root / "rotation.audit").exists())

    def test_legacy_rotation_refuses_lifecycle_role_target_and_identity_mismatches(self) -> None:
        for case in ("blocker", "manager", "human", "protected", "process"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", manager=case == "manager", pending=["preserve exact queue"])
                target = "hcfg:2" if case == "human" else "cfg:2"
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                args = self.legacy_rotation_args(
                    root,
                    target=target,
                    expected_blocker="wrong blocker" if case == "blocker" else "model capacity",
                    protected_targets=("cfg:2.0",) if case == "protected" else ("protected:9",),
                )

                def same_process(_pane: Pane) -> None:
                    if case == "process" and (root / "rotation.audit").exists():
                        raise StartError("tmux pane process identity changed before process replacement.")

                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("", "legacy")),
                    patch("omo_manager.omo_codex_start.verify_same_process", side_effect=same_process),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    self.assertRaises(StartError),
                ):
                    start(args)
                respawn.assert_not_called()
                if case == "process":
                    self.assertIn("final-result: failed", (root / "rotation.audit").read_text(encoding="utf-8"))

    def test_legacy_rotation_catches_last_probe_mutation_and_respawn_failure(self) -> None:
        for case in ("task mutation", "UUID recovery", "respawn failure"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                n_queries = 0

                def session(*_args: object) -> tuple[str, str]:
                    nonlocal n_queries
                    n_queries += 1
                    if case == "task mutation" and n_queries == 3:
                        task = root / "worker.md"
                        task.write_text(task.read_text(encoding="utf-8") + "concurrent mutation\n", encoding="utf-8")
                    if case == "UUID recovery" and n_queries == 3:
                        return self.SESSION_ID, ""
                    return "", "legacy"

                respawn_error = StartError("respawn failed")
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=session),
                    patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n"),
                    patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn_error) as respawn,
                    self.assertRaises(StartError),
                ):
                    start(self.legacy_rotation_args(root))

                if case in {"task mutation", "UUID recovery"}:
                    respawn.assert_not_called()
                else:
                    respawn.assert_called_once()
                self.assertIn("final-result: failed", (root / "rotation.audit").read_text(encoding="utf-8"))

    def test_legacy_rotation_audit_finalization_failure_leaves_durable_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("", "legacy")),
                patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n"),
                patch("omo_manager.omo_codex_start.respawn_codex", side_effect=StartError("respawn failed")),
                patch("omo_manager.omo_codex_start.finish_rotation_audit", side_effect=StartError("audit finalization failed")),
                self.assertRaises(StartError) as raised,
            ):
                start(self.legacy_rotation_args(root))
            self.assertTrue(any("audit remains completion-unknown" in note for note in getattr(raised.exception, "__notes__", ())))
            audit = (root / "rotation.audit").read_text(encoding="utf-8")
            self.assertIn("completion: unknown-until-finalized", audit)
            self.assertNotIn("final-result:", audit)

    def test_rotate_worker_audit_faults_leave_unknown_without_masking_rotation_failure(self) -> None:
        for case in ("success finalization", "post-respawn failure"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                task_before = (root / "worker.md").read_bytes()
                initial = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                rotated_pane = replace(initial, pane_pid=5252)
                rotated = False

                def resolve(_target: str) -> Pane:
                    return rotated_pane if rotated else initial

                def respawn(_pane: Pane, _command: str) -> None:
                    nonlocal rotated
                    rotated = True

                sessions = iter(((self.SESSION_ID, ""), (self.SESSION_ID, ""), (self.SESSION_ID, ""), ("119f670b-6a2f-7463-b9be-9aa6ff0cec43", "")))
                startup_error = StartError("fresh startup failed after respawn")
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=lambda *_args: next(sessions)),
                    patch("omo_manager.omo_codex_start.prompt_text", return_value="worker-only prompt\n"),
                    patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn),
                    patch("omo_manager.omo_codex_start.wait_started", side_effect=startup_error if case == "post-respawn failure" else None, return_value="running"),
                    patch("omo_manager.omo_codex_start.finish_rotation_audit", side_effect=StartError("audit finalization failed")),
                    self.assertRaises(StartError) as raised,
                ):
                    start(self.rotation_args(root))

                self.assertTrue(rotated)
                if case == "post-respawn failure":
                    self.assertIs(startup_error, raised.exception)
                    self.assertTrue(any("audit remains completion-unknown" in note for note in getattr(raised.exception, "__notes__", ())))
                else:
                    self.assertIn("audit finalization failed", str(raised.exception))
                self.assertEqual(task_before, (root / "worker.md").read_bytes())
                audit_text = (root / "rotation.audit").read_text(encoding="utf-8")
                self.assertIn("completion: unknown-until-finalized", audit_text)
                self.assertNotIn("final-result:", audit_text)

    def test_rotate_worker_refuses_task_target_role_and_lifecycle_mismatches(self) -> None:
        cases = ("target", "manager", "status", "owner", "queue", "bytes")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", manager=case == "manager", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                args = self.rotation_args(
                    root,
                    target="cfg:3" if case == "target" else "cfg:2",
                    expected_status="running" if case == "status" else "blocked",
                    expected_owner_target="wrong:1" if case == "owner" else "cfg:1",
                    expected_pending_items=("wrong queue",) if case == "queue" else ("preserve exact queue",),
                    expected_task_sha256="0" * 64 if case == "bytes" else hashlib.sha256((root / "worker.md").read_bytes()).hexdigest(),
                )

                def resolve(target: str) -> Pane:
                    return replace(pane, target="cfg:3.0", pane_id="%3") if target.startswith("cfg:3") else pane

                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                    patch("omo_manager.omo_codex_start.inspect"),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    self.assertRaises(StartError),
                ):
                    start(args)
                respawn.assert_not_called()
                self.assertFalse((root / "rotation.audit").exists())

    def test_rotate_worker_refuses_missing_human_owned_and_explicitly_protected_targets(self) -> None:
        for case in ("missing", "human", "protected"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                args = self.rotation_args(
                    root,
                    target="hcfg:2" if case == "human" else "cfg:2",
                    protected_targets=("cfg:2.0",) if case == "protected" else ("protected:9",),
                )
                error = StartError("tmux target does not exist: cfg:2")
                with patch("omo_manager.omo_codex_start.resolve_pane", side_effect=error) as resolve, patch("omo_manager.omo_codex_start.respawn_codex") as respawn, self.assertRaises(StartError):
                    start(args)
                if case in {"human", "protected"}:
                    resolve.assert_not_called()
                respawn.assert_not_called()

    def test_rotate_worker_refuses_lifecycle_byte_drift_before_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="blocked", pending=["preserve exact queue"])
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)

            def capture(*_args: object) -> tuple[str, str]:
                task = root / "worker.md"
                task.write_text(task.read_text(encoding="utf-8") + "lifecycle drift\n", encoding="utf-8")
                return self.SESSION_ID, ""

            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=capture),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "task or pending queue no longer has its captured binding"),
            ):
                start(self.rotation_args(root))
            respawn.assert_not_called()
            self.assertFalse((root / "rotation.audit").exists())

    def test_rotate_worker_revalidates_binding_and_session_after_audit_reservation(self) -> None:
        for case in ("task during reservation", "task during session check", "session"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root, status="blocked", pending=["preserve exact queue"])
                pane = Pane("cfg:2.0", "%2", "@2", "bun", root, 4242)
                reserved = False

                def reserve(path: Path, text: str) -> None:
                    nonlocal reserved
                    reserve_rotation_audit(path, text)
                    reserved = True
                    if case == "task during reservation":
                        task = root / "worker.md"
                        task.write_text(task.read_text(encoding="utf-8") + "lifecycle drift\n", encoding="utf-8")

                def session(*_args: object) -> tuple[str, str]:
                    if reserved and case == "task during session check":
                        task = root / "worker.md"
                        task.write_text(task.read_text(encoding="utf-8") + "lifecycle drift\n", encoding="utf-8")
                    return ("119f670b-6a2f-7463-b9be-9aa6ff0cec43", "") if reserved and case == "session" else (self.SESSION_ID, "")

                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=session),
                    patch("omo_manager.omo_codex_start.reserve_rotation_audit", side_effect=reserve),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    self.assertRaises(StartError),
                ):
                    start(self.rotation_args(root))

                respawn.assert_not_called()
                self.assertIn("final-result: failed", (root / "rotation.audit").read_text(encoding="utf-8"))

    def test_recover_non_codex_requires_prompt_and_evidence(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "max",
            "--recover-non-codex",
        ]
        with self.assertRaises(SystemExit):
            parse_args(common)
        with self.assertRaises(SystemExit):
            parse_args(common + ["--prompt-file", "worker.md"])
        args = parse_args(
            common
            + [
                "--prompt-file",
                "worker.md",
                "--recovery-evidence",
                "/tmp/recovery.receipt",
            ]
        )
        self.assertTrue(args.recover_non_codex)
        self.assertFalse(args.confirm_empty_shell)
        self.assertEqual("/tmp/recovery.receipt", args.recovery_evidence)

    def test_record_recovery_evidence_requires_event_id_and_output(self) -> None:
        common = [
            "--task-file",
            "worker.md",
            "--target",
            "cfg:2",
            "--model",
            "gpt-5.6-terra",
            "--reasoning-effort",
            "max",
            "--record-recovery-evidence",
        ]
        with self.assertRaises(SystemExit):
            parse_args(common)
        with self.assertRaises(SystemExit):
            parse_args(common + ["--recovery-output", "/tmp/receipt"])
        args = parse_args(common + ["--recovery-output", "/tmp/receipt", "--failed-delivery-id", "watch-1"])
        self.assertTrue(args.record_recovery_evidence)
        self.assertEqual(Path("/tmp/receipt"), args.recovery_output)

    def test_recovery_rejects_human_target_before_status_probe(self) -> None:
        pane = Pane("hcfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
        evidence = "/tmp/recovery.receipt"
        with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
            with self.assertRaisesRegex(StartError, "human-owned"):
                require_recovery_target(pane, evidence)
        capture.assert_not_called()

    def test_helper_produced_receipt_validates_event_binding_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])):
                require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))
                consume_recovery_receipt(root, str(receipt))
                with self.assertRaisesRegex(StartError, "already consumed"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))

    def test_recovery_receipt_accepts_stable_later_not_codex_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            original = ["malformed agent output", "first failure"]
            later = ["malformed agent output", "first failure", "stable fatal detail"]
            event_id = "11111111-1111-4111-8111-222222222222"
            identity = subprocess.CompletedProcess([], 0, f"{pane.target}\t{pane.pane_id}\t{pane.window_id}\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch(
                "omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, original)
            ):
                self.assertIsNotNone(record_terminal_delivery_failure(root, pane.target, event_id, "status=not_codex"))
            receipt = root / RECOVERY_RECEIPT_DIRNAME / f"{event_id}.receipt"
            task = self.recovery_task(pane)
            with patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, later)), patch(
                "omo_manager.omo_codex_start.verify_same_pane"
            ):
                record_recovery_evidence(root, pane, receipt, event_id, "worker.md", task)
            fields = dict(item.split("=", 1) for item in receipt.read_text(encoding="utf-8").strip().split(";"))
            self.assertEqual(hashlib.sha256("\n".join(original).encode()).hexdigest(), fields["original_tail_sha256"])
            self.assertEqual(hashlib.sha256("\n".join(later).encode()).hexdigest(), fields["tail_sha256"])
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch(
                "omo_manager.omo_codex_start.exact_tail", return_value=(True, later)
            ):
                require_recovery_target(pane, str(receipt), root, "worker.md", task)

    def test_record_recovery_evidence_rejects_before_creating_receipt_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = root / RECOVERY_RECEIPT_DIRNAME / "missing-event.receipt"
            with self.assertRaisesRegex(StartError, "tracked task"):
                record_recovery_evidence(root, pane, receipt, "missing-event")
            self.assertFalse((root / RECOVERY_RECEIPT_DIRNAME).exists())
            with patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["fatal output"])), patch(
                "omo_manager.omo_codex_start.verify_same_pane"
            ):
                with self.assertRaisesRegex(StartError, "not readable"):
                    record_recovery_evidence(root, pane, receipt, "missing-event", "worker.md", self.recovery_task(pane))
            self.assertFalse((root / RECOVERY_RECEIPT_DIRNAME).exists())

    def test_recovery_receipt_rejects_queue_drift_and_recovered_codex(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            lines = ["malformed agent output", "stable fatal detail"]
            receipt = self.write_recovery_receipt(root, pane, lines)
            drifted = replace(self.recovery_task(pane), pending_task_items=("different queue",))
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch(
                "omo_manager.omo_codex_start.exact_tail"
            ) as capture:
                with self.assertRaisesRegex(StartError, "immutable pending queue"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", drifted)
            capture.assert_not_called()
            running = ["• Working (1s • esc to interrupt)", "  gpt-5.6"]
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch(
                "omo_manager.omo_codex_start.exact_tail", return_value=(True, running)
            ):
                with self.assertRaisesRegex(StartError, "fresh not_codex"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))

    def test_verified_recovery_retirement_removes_complete_transaction_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            consume_recovery_receipt(root, str(receipt))
            retire_recovery_receipt(root, str(receipt))
            retire_recovery_receipt(root, str(receipt))
            self.assertEqual([], list((root / RECOVERY_EVENT_DIRNAME).iterdir()))
            self.assertEqual([], list((root / RECOVERY_RECEIPT_DIRNAME).iterdir()))

    def test_recovery_retirement_retains_ambiguous_incomplete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            consume_recovery_receipt(root, str(receipt))
            receipt.with_name(f"{receipt.name}.used").unlink()
            with self.assertRaisesRegex(StartError, "transaction is incomplete"):
                retire_recovery_receipt(root, str(receipt))
            self.assertTrue(receipt.is_file())
            self.assertTrue(delivery_event_path(root, receipt.stem).is_file())

    def test_recovery_retirement_retains_remaining_records_when_event_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            consume_recovery_receipt(root, str(receipt))
            delivery_event_path(root, receipt.stem).unlink()
            with self.assertRaisesRegex(StartError, "transaction is incomplete"):
                retire_recovery_receipt(root, str(receipt))
            self.assertTrue(receipt.is_file())
            self.assertTrue(recovery_issuance_path(receipt).is_file())

    def test_recovery_retirement_resumes_after_interrupted_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            consume_recovery_receipt(root, str(receipt))
            original_unlink = Path.unlink
            calls = 0

            def interrupted_unlink(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic interruption")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", interrupted_unlink), self.assertRaisesRegex(StartError, "synthetic interruption"):
                retire_recovery_receipt(root, str(receipt))
            self.assertEqual(
                0,
                main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "unused.md",
                        "--target",
                        "cfg:2.0",
                        "--retire-recovery-evidence",
                        "--recovery-evidence",
                        str(receipt),
                    ]
                ),
            )
            self.assertEqual(
                0,
                main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "unused.md",
                        "--target",
                        "cfg:2.0",
                        "--retire-recovery-evidence",
                        "--recovery-evidence",
                        str(receipt),
                    ]
                ),
            )
            self.assertEqual([], list((root / RECOVERY_EVENT_DIRNAME).iterdir()))
            self.assertEqual([], list((root / RECOVERY_RECEIPT_DIRNAME).iterdir()))

    def test_recovery_retirement_fsyncs_manifest_before_first_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            consume_recovery_receipt(root, str(receipt))
            operations: list[str] = []
            original_fsync = os.fsync
            original_unlink = Path.unlink

            def tracked_fsync(fd: int) -> None:
                mode = os.fstat(fd).st_mode
                operations.append("file-fsync" if stat.S_ISREG(mode) else "directory-fsync")
                original_fsync(fd)

            def tracked_unlink(path: Path, *args: object, **kwargs: object) -> None:
                operations.append("unlink")
                original_unlink(path, *args, **kwargs)

            with patch("omo_manager.omo_codex_start.os.fsync", tracked_fsync), patch.object(Path, "unlink", tracked_unlink):
                retire_recovery_receipt(root, str(receipt))
            self.assertLess(operations.index("file-fsync"), operations.index("unlink"))
            self.assertLess(operations.index("directory-fsync"), operations.index("unlink"))

    def test_recovery_retirement_cli_rejects_dry_run(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "unused.md",
                    "--target",
                    "cfg:2.0",
                    "--retire-recovery-evidence",
                    "--recovery-evidence",
                    "/tmp/recovery.receipt",
                    "--dry-run",
                ]
            )

    def test_watcher_event_producer_captures_status_and_rejects_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            target = "cfg:2.0"
            event_id = "watcher-event-1"
            identity = subprocess.CompletedProcess([], 0, f"{target}\t%2\t@2\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])):
                event = record_terminal_delivery_failure(root, target, event_id, "sender failed")
                duplicate = record_terminal_delivery_failure(root, target, event_id, "sender failed again")
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(0o600, event.stat().st_mode & 0o777)
            fields = dict(item.split("=", 1) for item in event.read_text(encoding="utf-8").strip().split(";"))
            self.assertEqual(event_id, fields["delivery_id"])
            self.assertEqual("not_codex", fields["status"])
            self.assertEqual("failed", fields["delivery"])
            self.assertEqual(f"{RECOVERY_RECEIPT_DIRNAME}/{event_id}.receipt", fields["receipt_file"])
            self.assertEqual(64, len(fields["receipt_nonce"]))
            self.assertIsNone(duplicate)

    def test_watcher_event_is_fsynced_before_durable_path_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            operations: list[str] = []
            original_fsync = os.fsync
            original_link = os.link

            def tracked_fsync(fd: int) -> None:
                operations.append("file-fsync" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory-fsync")
                original_fsync(fd)

            def tracked_link(*args: object, **kwargs: object) -> None:
                operations.append("link")
                original_link(*args, **kwargs)

            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_pending_watch.os.fsync", tracked_fsync),
                patch("omo_manager.omo_pending_watch.os.link", tracked_link),
            ):
                event = record_terminal_delivery_failure(root, "cfg:2.0", "durable-order", "sender failed")
            self.assertIsNotNone(event)
            self.assertLess(operations.index("file-fsync"), operations.index("link"))
            self.assertLess(operations.index("link"), operations.index("directory-fsync"))

    def test_watcher_event_reports_no_record_when_directory_fsync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            original_fsync = os.fsync

            def fail_directory_fsync(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("synthetic directory fsync failure")
                original_fsync(fd)

            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_pending_watch.os.fsync", fail_directory_fsync),
            ):
                self.assertIsNone(record_terminal_delivery_failure(root, "cfg:2.0", "durable-failed", "sender failed"))

    def test_watcher_event_fsyncs_new_directory_parent_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            directory_fsyncs = 0
            original_fsync = os.fsync

            def tracked_fsync(fd: int) -> None:
                nonlocal directory_fsyncs
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    directory_fsyncs += 1
                original_fsync(fd)

            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_pending_watch.os.fsync", tracked_fsync),
            ):
                event = record_terminal_delivery_failure(root, "cfg:2.0", "durable-new-dir", "sender failed")
            self.assertIsNotNone(event)
            self.assertEqual(2, directory_fsyncs)

    def test_watcher_event_fsyncs_parent_when_event_directory_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / RECOVERY_EVENT_DIRNAME).mkdir(mode=0o700)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            directory_fsyncs = 0
            original_fsync = os.fsync

            def tracked_fsync(fd: int) -> None:
                nonlocal directory_fsyncs
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    directory_fsyncs += 1
                original_fsync(fd)

            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_pending_watch.os.fsync", tracked_fsync),
            ):
                event = record_terminal_delivery_failure(root, "cfg:2.0", "durable-existing-dir", "sender failed")
            self.assertIsNotNone(event)
            self.assertEqual(2, directory_fsyncs)

    def test_watcher_event_refuses_to_create_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_parent:
            root = Path(raw_parent) / "missing-root"
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
            ):
                self.assertIsNone(record_terminal_delivery_failure(root, "cfg:2.0", "missing-root", "sender failed"))
            self.assertFalse(root.exists())

    def test_watcher_event_rolls_back_canonical_record_when_temporary_unlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            original_unlink = os.unlink
            failed_once = False

            def fail_temporary_unlink(path: str | bytes, *args: object, **kwargs: object) -> None:
                nonlocal failed_once
                if str(path).startswith(".delivery-event-") and not failed_once:
                    failed_once = True
                    raise OSError("synthetic temporary-link cleanup failure")
                original_unlink(path, *args, **kwargs)

            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_pending_watch.os.unlink", fail_temporary_unlink),
            ):
                event = record_terminal_delivery_failure(root, "cfg:2.0", "durable-cleanup-failed", "sender failed")
            self.assertIsNone(event)
            self.assertFalse((root / RECOVERY_EVENT_DIRNAME / "durable-cleanup-failed.event").exists())
            self.assertEqual(1, len(list((root / RECOVERY_EVENT_DIRNAME).glob(".delivery-event-*"))))

    def test_recovery_pipeline_accepts_private_setgid_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            event_dir = root / RECOVERY_EVENT_DIRNAME
            receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
            event_dir.mkdir()
            receipt_dir.mkdir()
            event_dir.chmod(0o2700)
            receipt_dir.chmod(0o2700)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            event_id = "private-setgid-event"
            identity = subprocess.CompletedProcess([], 0, f"{pane.target}\t{pane.pane_id}\t{pane.window_id}\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])):
                event = record_terminal_delivery_failure(root, pane.target, event_id, "sender failed")
            self.assertIsNotNone(event)
            receipt = receipt_dir / f"{event_id}.receipt"
            with patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])), patch("omo_manager.omo_codex_start.verify_same_pane"):
                record_recovery_evidence(root, pane, receipt, event_id, "worker.md", self.recovery_task(pane))
            self.assertTrue(receipt.is_file())
            self.assertEqual(0, stat.S_IMODE(event_dir.stat().st_mode) & 0o077)
            self.assertEqual(0, stat.S_IMODE(receipt_dir.stat().st_mode) & 0o077)

    def test_watcher_event_producer_rejects_group_accessible_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            event_dir = root / RECOVERY_EVENT_DIRNAME
            event_dir.mkdir()
            event_dir.chmod(0o750)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])):
                event = record_terminal_delivery_failure(root, "cfg:2.0", "unsafe-event-dir", "sender failed")
            self.assertIsNone(event)
            self.assertFalse(any(event_dir.iterdir()))

    def test_terminal_failure_exposes_only_durable_event_id(self) -> None:
        root = Path("/tmp/work-logs")
        definite = RuntimeError("target is not a Codex pane before submit: cfg:2.0")
        event_path = root / RECOVERY_EVENT_DIRNAME / "durable-delivery.event"
        with patch("omo_manager.omo_pending_watch.record_terminal_delivery_failure", return_value=event_path) as record:
            result = terminal_delivery_failure(root, "cfg:2.0", "delivery-1", definite)
        record.assert_called_once_with(root, "cfg:2.0", "delivery-1", str(definite))
        self.assertEqual(
            f"target is not a Codex pane before submit: cfg:2.0; recovery event recorded at `{event_path}`",
            result.error,
        )

        with patch("omo_manager.omo_pending_watch.record_terminal_delivery_failure", return_value=None):
            result = terminal_delivery_failure(root, "cfg:2.0", "delivery-1", definite)
        self.assertEqual(f"{definite}; no recovery event was created", result.error)

        unknown = RuntimeError("Codex paste not verified after 5s")
        with patch("omo_manager.omo_pending_watch.record_terminal_delivery_failure") as record:
            result = terminal_delivery_failure(root, "cfg:2.0", "delivery-2", unknown)
        record.assert_not_called()
        self.assertEqual(str(unknown), result.error)

    def test_sync_delivery_returns_durable_recovery_event_id(self) -> None:
        root = Path("/tmp/work-logs")
        event_path = root / RECOVERY_EVENT_DIRNAME / "durable-delivery.event"
        failure = PrePasteRejected("target is not a Codex pane before submit: cfg:2.0")
        with (
            patch("omo_manager.omo_pending_watch.uuid.uuid4", return_value="attempted-delivery"),
            patch("omo_manager.omo_pending_watch.submit_delivery_send", side_effect=failure),
            patch("omo_manager.omo_pending_watch.record_terminal_delivery_failure", return_value=event_path),
        ):
            result = try_send_delivery_text("pending delivery", "message", "cfg:2.0", root=root)
        self.assertEqual(1, result.status)
        self.assertEqual(f"{failure}; recovery event recorded at `{event_path}`", result.error)

    def test_watcher_event_producer_refuses_failed_or_codex_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(False, [])):
                self.assertIsNone(record_terminal_delivery_failure(root, "cfg:2.0", "capture-failed", "sender failed"))
            with (
                patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity),
                patch(
                    "omo_manager.omo_pending_watch.exact_codex_tail",
                    return_value=(True, ["• Working (1s • esc to interrupt)", "  gpt-5.6"]),
                ),
            ):
                self.assertIsNone(record_terminal_delivery_failure(root, "cfg:2.0", "codex-running", "sender failed"))
            self.assertFalse((root / RECOVERY_EVENT_DIRNAME).exists())

    def test_start_record_mode_writes_receipt_without_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            event_id = "11111111-1111-4111-8111-111111111111"
            receipt = root / RECOVERY_RECEIPT_DIRNAME / f"{event_id}.receipt"
            identity = subprocess.CompletedProcess([], 0, f"{pane.target}\t{pane.pane_id}\t{pane.window_id}\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])):
                record_terminal_delivery_failure(root, pane.target, event_id, "status=not_codex")
            args = self.args(root, session_id="", prompt_file=None, record_recovery_evidence=True, recovery_output=receipt, failed_delivery_id=event_id)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_codex_start.verify_same_pane"),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
            ):
                self.assertEqual("recovery-evidence-recorded", start(args))
            self.assertTrue(receipt.is_file())
            self.assertTrue((root / RECOVERY_EVENT_DIRNAME).is_dir())
            respawn.assert_not_called()

    def test_recovery_rejects_forged_receipt_without_matching_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            fields = dict(item.split("=", 1) for item in receipt.read_text(encoding="utf-8").strip().split(";"))
            fields["delivery_id"] = "forged-delivery"
            receipt.write_text(";".join(f"{key}={value}" for key, value in fields.items()) + "\n", encoding="utf-8")
            receipt.chmod(0o600)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "event record changed|not bound"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))
            capture.assert_not_called()

    def test_recovery_rejects_copied_receipt_outside_helper_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            copied = root / "copied.receipt"
            copied.write_bytes(receipt.read_bytes())
            copied.chmod(0o600)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "directly under the helper receipt directory"):
                    require_recovery_target(pane, str(copied), root, "worker.md", self.recovery_task(pane))
            capture.assert_not_called()

    def test_recovery_rejects_handwritten_same_dir_receipt_without_issuance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            forged = receipt.with_name("handwritten.receipt")
            fields = dict(item.split("=", 1) for item in receipt.read_text(encoding="utf-8").strip().split(";"))
            fields["receipt_file"] = str(forged.relative_to(root))
            forged.write_text(";".join(f"{key}={value}" for key, value in fields.items()) + "\n", encoding="utf-8")
            forged.chmod(0o600)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "watcher-issued path|bind this pane"):
                    require_recovery_target(pane, str(forged), root, "worker.md", self.recovery_task(pane))
            capture.assert_not_called()

    def test_recovery_rejects_handwritten_canonical_receipt_without_issuance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            issuance = recovery_issuance_path(receipt)
            issuance.unlink()
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "readable|issuance"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))
            capture.assert_not_called()

    def test_recovery_rejects_mismatched_issuance_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            issuance = recovery_issuance_path(receipt)
            issuance_fields = dict(item.split("=", 1) for item in issuance.read_text(encoding="utf-8").strip().split(";"))
            issuance_fields["receipt_inode"] = "0"
            issuance.write_text(";".join(f"{key}={value}" for key, value in issuance_fields.items()) + "\n", encoding="utf-8")
            issuance.chmod(0o600)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "immutable issuance"):
                    require_recovery_target(pane, str(receipt), root, "worker.md", self.recovery_task(pane))
            capture.assert_not_called()

    def test_watcher_event_producer_refuses_symlinked_event_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, tempfile.TemporaryDirectory() as outside_raw:
            root = Path(raw_root)
            outside = Path(outside_raw)
            outside.chmod(0o755)
            event_dir = root / RECOVERY_EVENT_DIRNAME
            os.symlink(outside, event_dir)
            identity = subprocess.CompletedProcess([], 0, "cfg:2.0\t%2\t@2\n", "")
            with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, ["delivery failed"])):
                self.assertIsNone(record_terminal_delivery_failure(root, "cfg:2.0", "symlinked-event-dir", "sender failed"))
            self.assertEqual(0o755, outside.stat().st_mode & 0o777)
            self.assertFalse(any(outside.iterdir()))

    def test_recovery_requires_fresh_not_codex_evidence_and_non_shell(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            running_lines = ["• Working (1s • esc to interrupt)", "  gpt-5.6"]
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"], digest_lines=running_lines)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, running_lines)):
                with self.assertRaisesRegex(StartError, "fresh not_codex"):
                    require_recovery_target(pane, str(receipt), task_file="worker.md", task=self.recovery_task(pane))
            shell = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=shell), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "not a verified non-Codex"):
                    require_recovery_target(shell, str(receipt), task_file="worker.md", task=self.recovery_task(pane))
            capture.assert_not_called()
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, [])):
                with self.assertRaisesRegex(StartError, "empty status capture"):
                    require_recovery_target(pane, str(receipt), task_file="worker.md", task=self.recovery_task(pane))
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(False, [])):
                with self.assertRaisesRegex(StartError, "capture failed"):
                    require_recovery_target(pane, str(receipt), task_file="worker.md", task=self.recovery_task(pane))
            stale = self.write_recovery_receipt(root, pane, ["delivery failed"], observed_at=datetime.now(timezone.utc).replace(year=2020))
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "stale"):
                    require_recovery_target(pane, str(stale), task_file="worker.md", task=self.recovery_task(pane))
            capture.assert_not_called()
            mismatched_digest = self.write_recovery_receipt(root, pane, ["delivery failed"], digest_lines=["different output"])
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])):
                with self.assertRaisesRegex(StartError, "status capture changed"):
                    require_recovery_target(pane, str(mismatched_digest), task_file="worker.md", task=self.recovery_task(pane))
            changed = Pane("cfg:2.0", "%9", "@9", "bunx", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=changed), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "identity changed"):
                    require_recovery_target(pane, str(receipt), task_file="worker.md", task=self.recovery_task(pane))
            capture.assert_not_called()

    def test_restart_command_execs_resumed_session(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
        command = launch_command(self.args(root, restart_running=True), pane, None, "[marker]", replace_process=True)
        self.assertIn("&& exec bunx", command)
        self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)

    def test_respawn_replaces_process_and_preserves_pane_identity(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp/work logs"), 4242)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("omo_manager.omo_codex_start.run", return_value=completed) as run,
            patch("omo_manager.omo_codex_start.verify_same_process") as verify_process,
            patch("omo_manager.omo_codex_start.verify_same_pane") as verify_pane,
        ):
            respawn_codex(pane, "exec codex resume session")
        run.assert_called_once_with(
            [
                "tmux",
                "if-shell",
                "-F",
                "-t",
                "%2",
                "#{&&:#{==:#{pane_id},%2},#{==:#{window_id},@2},#{==:#{session_name}:#{window_index}.#{pane_index},cfg:2.0},#{==:#{pane_pid},4242},#{==:#{pane_current_command},bun}}",
                "respawn-pane -k -t %2 -c '/tmp/work logs' 'exec codex resume session'",
                "run-shell 'exit 1'",
            ]
        )
        verify_process.assert_called_once_with(pane)
        verify_pane.assert_called_once_with(pane)

    def test_respawn_refuses_atomic_identity_guard_failure_without_post_check(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp/work logs"))
        failed = subprocess.CompletedProcess([], 1, "", "moved")
        with (
            patch("omo_manager.omo_codex_start.run", return_value=failed) as run,
            patch("omo_manager.omo_codex_start.verify_same_process") as verify_process,
            patch("omo_manager.omo_codex_start.verify_same_pane") as verify_pane,
        ):
            with self.assertRaisesRegex(StartError, "failed to respawn Codex"):
                respawn_codex(pane, "exec codex resume session")
        run.assert_called_once()
        verify_process.assert_called_once_with(pane)
        verify_pane.assert_not_called()

    def test_restart_captures_session_before_atomic_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            args = self.args(root, session_id="", restart_running=True)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("019f670b-6a2f-7463-b9be-9aa6ff0cec43", "")) as capture,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
                patch("omo_manager.omo_codex_start.verify_restart_continuity") as continuity,
            ):
                self.assertEqual("running", start(args))
            capture.assert_called_once_with("%2", 240, 10.0)
            continuity.assert_called_once()
            command = respawn.call_args.args[1]
            self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)

    def test_recovery_respawns_verified_non_codex_in_same_pane_with_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            prompt = root / "recovery-prompt.md"
            prompt.write_text("Continue the recorded mailbox task.\n", encoding="utf-8")
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            args = self.args(
                root,
                session_id="",
                prompt_file=prompt,
                recover_non_codex=True,
                recovery_evidence=str(receipt),
            )
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
            ):
                self.assertEqual("running", start(args))
            respawn.assert_called_once()
            self.assertIs(respawn.call_args.args[0], pane)
            command = respawn.call_args.args[1]
            self.assertIn("&& exec bunx", command)
            self.assertIn("--model gpt-5.6-terra", command)
            self.assertIn("model_reasoning_effort=", command)
            self.assertIn("$(cat --", command)

    def test_recovery_refuses_status_transition_before_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            prompt = root / "recovery-prompt.md"
            prompt.write_text("Continue the recorded mailbox task.\n", encoding="utf-8")
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            args = self.args(root, session_id="", prompt_file=prompt, recover_non_codex=True, recovery_evidence=str(receipt))
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch(
                    "omo_manager.omo_codex_start.exact_tail",
                    side_effect=[(True, ["delivery failed"]), (True, ["• Working (1s • esc to interrupt)", "  gpt-5.6"])],
                ),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
            ):
                with self.assertRaisesRegex(StartError, "no longer has fresh not_codex"):
                    start(args)
            respawn.assert_not_called()

    def test_recovery_refuses_task_queue_drift_at_respawn_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            prompt = root / "recovery-prompt.md"
            prompt.write_text("Continue the recorded task.\n", encoding="utf-8")
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            receipt = self.write_recovery_receipt(root, pane, ["delivery failed"])
            args = self.args(root, session_id="", prompt_file=prompt, recover_non_codex=True, recovery_evidence=str(receipt))

            def capture_with_task_drift(_target: str, _lines: int) -> tuple[bool, list[str]]:
                self.write_task(root, pending=["different queue"])
                return True, ["delivery failed"]

            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.exact_tail", side_effect=capture_with_task_drift),
                patch("omo_manager.omo_codex_start.consume_recovery_receipt") as consume,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
            ):
                with self.assertRaisesRegex(StartError, "task or pending queue"):
                    start(args)
            consume.assert_not_called()
            respawn.assert_not_called()

    def test_restart_does_not_replace_process_when_session_capture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("", "")),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "pane was not replaced"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            respawn.assert_not_called()

    def test_restart_rejects_non_codex_process_before_session_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "python", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("not_codex", ["python output"])),
                patch("omo_manager.omo_codex_start.query_status_session_id") as capture,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "not a supported live Codex pane"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            capture.assert_not_called()
            respawn.assert_not_called()

    def test_restart_rejects_process_transition_after_session_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", side_effect=[Report("running", ["working"]), Report("not_codex", ["shell"])]),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("019f670b-6a2f-7463-b9be-9aa6ff0cec43", "")),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "not a supported live Codex pane"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            respawn.assert_not_called()

    def test_validate_task_accepts_pcodx_tool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, tool="pcodx")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                binding = validate_task(self.args(root, session_id="", restart_running=True), pane)
            self.assertEqual("pcodx", binding.tool)

    def test_validate_task_rejects_pcodx_outside_live_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, tool="pcodx")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                self.assertRaisesRegex(StartError, "limited to --restart-running"),
            ):
                validate_task(self.args(root), pane)

    def test_pcodx_restart_command_preserves_model_without_nested_codex_package(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("hwl:3.0", "%3", "@3", "bunx", root)
        state = {
            "PCODX_POC_ROOT": "/tmp/pcodx-poc",
            "PCODX_RUN_DIR": "/tmp/pcodx-run",
            "PCODX_LEDGER_PATH": "/tmp/pcodx-run/ledger.json",
            "PCODX_SESSION_ID": "pcodx-3",
        }
        command = launch_command(self.args(root), pane, None, "[marker]", replace_process=True, tool="pcodx", pcodx_env=state)
        self.assertIn("/omo_manager/pcodx --dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("pcodx @openai/codex", command)
        self.assertNotIn("check_for_update_on_startup", command)
        self.assertIn("PCODX_LEDGER_PATH=/tmp/pcodx-run/ledger.json", command)
        self.assertIn("--model gpt-5.6-terra", command)
        self.assertIn("--cd '/tmp/work logs' resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)

    def test_pcodx_command_requires_complete_live_state_binding(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("hwl:3.0", "%3", "@3", "bunx", root)
        with self.assertRaisesRegex(StartError, "exact live state binding"):
            launch_command(self.args(root), pane, None, "[marker]", tool="pcodx")

    def test_human_restart_authority_accepts_only_assigned_source_and_lines(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = self.write_human_restart_authority(root)
            pane = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
            authorized = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            with patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root):
                authority = require_human_restart_authority(authorized, pane)
            self.assertIsNotNone(authority)
            assert authority is not None
            self.assertEqual(source, authority.source_path)
            self.assertEqual("restart", authority.action)
            self.assertEqual("hwl:3.0", authority.target)
            self.assertEqual(("%3", "@3", 4242), (authority.pane_id, authority.window_id, authority.pane_pid))
            with (
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                self.assertRaisesRegex(StartError, "exact approved source lines"),
            ):
                require_human_restart_authority(replace(authorized, human_email_lines=(3, 3)), pane)

    def test_human_restart_authority_rejects_byte_identical_fake_root_and_other_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_human_restart_authority(root)
            pane = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
            args = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            with self.assertRaisesRegex(StartError, "exact approved work-log root"):
                require_human_restart_authority(args, pane)
            with (
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                self.assertRaisesRegex(StartError, "exact approved human_task_planner.md task"),
            ):
                require_human_restart_authority(replace(args, task_file="other.md"), pane)

    def test_human_restart_authority_rejects_other_h_target_and_paraphrase(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = self.write_human_restart_authority(root)
            args = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            other = Pane("hwl:4.0", "%4", "@4", "bun", root, 4242)
            with self.assertRaisesRegex(StartError, "only to the exact approved hwl:3"):
                require_human_restart_authority(replace(args, target="hwl:4"), other)
            source.write_text("Subject: restart\n\nPlease reboot hwl:3 where it is.\n", encoding="utf-8")
            source.chmod(0o600)
            with (
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                self.assertRaisesRegex(StartError, "content does not match"),
            ):
                require_human_restart_authority(args, Pane("hwl:3.0", "%3", "@3", "bun", root, 4242))

    def test_exact_hwl_restart_preserves_pane_session_task_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(
                root,
                runat="hwl:3",
                status="long_running",
                manager=True,
                tool="pcodx",
                pending=["keep exact queue"],
                task_file=HUMAN_RESTART_TASK_FILE,
            )
            self.write_human_restart_authority(root)
            before_task = (root / HUMAN_RESTART_TASK_FILE).read_bytes()
            initial = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
            resumed = replace(initial, pane_pid=5252)
            pcodx = {
                "PCODX_POC_ROOT": "/tmp/pcodx-poc",
                "PCODX_RUN_DIR": "/tmp/pcodx-run",
                "PCODX_LEDGER_PATH": "/tmp/pcodx-run/ledger.json",
                "PCODX_SESSION_ID": "pcodx-3",
            }
            restarted = False

            def resolve(_target: str) -> Pane:
                return resumed if restarted else initial

            def respawn(_pane: Pane, _command: str) -> None:
                nonlocal restarted
                restarted = True

            args = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.pcodx_state", return_value=pcodx),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=(self.SESSION_ID, "")) as sessions,
                patch("omo_manager.omo_codex_start.respawn_codex", side_effect=respawn) as replace_process,
                patch("omo_manager.omo_codex_start.wait_started", return_value="ready"),
            ):
                self.assertEqual("ready", start(args))
            self.assertTrue(restarted)
            self.assertEqual(2, sessions.call_count)
            replace_process.assert_called_once()
            self.assertIn(f"resume {self.SESSION_ID}", replace_process.call_args.args[1])
            self.assertEqual(before_task, (root / HUMAN_RESTART_TASK_FILE).read_bytes())

    def test_exact_hwl_restart_rejects_missing_original_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, runat="hwl:3", tool="pcodx", task_file=HUMAN_RESTART_TASK_FILE)
            self.write_human_restart_authority(root)
            pane = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
            pcodx = {
                "PCODX_POC_ROOT": "/tmp/pcodx-poc",
                "PCODX_RUN_DIR": "/tmp/pcodx-run",
                "PCODX_LEDGER_PATH": "/tmp/pcodx-run/ledger.json",
                "PCODX_SESSION_ID": "pcodx-3",
            }
            args = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.pcodx_state", return_value=pcodx),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("", "")),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "could not capture the current Codex session"),
            ):
                start(args)
            respawn.assert_not_called()

    def test_exact_hwl_restart_rejects_changed_pane_before_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, runat="hwl:3", tool="pcodx", task_file=HUMAN_RESTART_TASK_FILE)
            self.write_human_restart_authority(root)
            initial = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
            changed = Pane("hwl:3.0", "%9", "@9", "bun", root, 5252)
            pcodx = {
                "PCODX_POC_ROOT": "/tmp/pcodx-poc",
                "PCODX_RUN_DIR": "/tmp/pcodx-run",
                "PCODX_LEDGER_PATH": "/tmp/pcodx-run/ledger.json",
                "PCODX_SESSION_ID": "pcodx-3",
            }
            pane_changed = False

            def resolve(_target: str) -> Pane:
                return changed if pane_changed else initial

            def capture(_target: str, _lines: int, _wait_s: float) -> tuple[str, str]:
                nonlocal pane_changed
                pane_changed = True
                return self.SESSION_ID, ""

            args = self.args(
                root,
                task_file=HUMAN_RESTART_TASK_FILE,
                target="hwl:3",
                session_id="",
                restart_running=True,
                human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
            )
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.pcodx_state", return_value=pcodx),
                patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=capture),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "pane or window identity changed"),
            ):
                start(args)
            respawn.assert_not_called()

    def test_exact_hwl_restart_rejects_stale_source_and_queue_drift(self) -> None:
        for drift in ("source", "queue"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(
                    root,
                    runat="hwl:3",
                    tool="pcodx",
                    pending=["preserve me"],
                    task_file=HUMAN_RESTART_TASK_FILE,
                )
                source = self.write_human_restart_authority(root)
                pane = Pane("hwl:3.0", "%3", "@3", "bun", root, 4242)
                pcodx = {
                    "PCODX_POC_ROOT": "/tmp/pcodx-poc",
                    "PCODX_RUN_DIR": "/tmp/pcodx-run",
                    "PCODX_LEDGER_PATH": "/tmp/pcodx-run/ledger.json",
                    "PCODX_SESSION_ID": "pcodx-3",
                }

                def capture(_target: str, _lines: int, _wait_s: float) -> tuple[str, str]:
                    if drift == "source":
                        source_stat = source.stat()
                        os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1))
                    else:
                        task = root / HUMAN_RESTART_TASK_FILE
                        task.write_text(task.read_text(encoding="utf-8").replace("- preserve me", "- queue drift"), encoding="utf-8")
                    return self.SESSION_ID, ""

                args = self.args(
                    root,
                    task_file=HUMAN_RESTART_TASK_FILE,
                    target="hwl:3",
                    session_id="",
                    restart_running=True,
                    human_email_file=HUMAN_RESTART_AUTHORITY_FILE,
                    human_email_lines=HUMAN_RESTART_AUTHORITY_LINES,
                )
                error = "stale or mismatched" if drift == "source" else "task or pending queue changed"
                with (
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.HUMAN_RESTART_ROOT", root),
                    patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                    patch("omo_manager.omo_codex_start.pcodx_state", return_value=pcodx),
                    patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=capture),
                    patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                    self.assertRaisesRegex(StartError, error),
                ):
                    start(args)
                respawn.assert_not_called()

    def test_post_marker_capture_uses_numeric_target_not_pane_id(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", Path("/tmp"))
        with patch("omo_manager.omo_codex_start.tail", return_value=["old", "[marker]", "new"]) as capture:
            self.assertEqual(["new"], post_marker_lines(pane, "[marker]"))
        capture.assert_called_once_with("cfg:2.0", 200)

    def test_require_same_shell_rejects_codex_after_lock_wait(self) -> None:
        expected = Pane("cfg:2.0", "%2", "@2", "zsh", Path("/tmp"))
        running = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp"))
        with patch("omo_manager.omo_codex_start.resolve_pane", return_value=running):
            with self.assertRaisesRegex(StartError, "not an empty shell"):
                require_same_shell(expected)

    def test_start_rejects_human_owned_session_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("hcfg:2.0", "%2", "@2", "zsh", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.require_same_shell") as require_shell,
                self.assertRaisesRegex(StartError, "human-owned"),
            ):
                start(self.args(root, target="hcfg:2"))

            require_shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
