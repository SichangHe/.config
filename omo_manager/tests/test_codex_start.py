from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
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
    current_todo_entries,
    consume_recovery_receipt,
    is_codex_update_prompt,
    launch_command,
    parse_args,
    post_marker_lines,
    prompt_text,
    record_recovery_evidence,
    recovery_issuance_path,
    require_human_restart_authority,
    require_recovery_target,
    require_same_shell,
    require_update_prompt,
    reserve_rotation_audit,
    resolve_pane,
    respawn_codex,
    skip_codex_update_prompt,
    start,
    validate_task,
)
from omo_manager.omo_codex_status import Report
from omo_manager.omo_pending_watch import record_terminal_delivery_failure


class CodexStartTests(unittest.TestCase):
    SESSION_ID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"

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
        (root / task_file).write_text(text, encoding="utf-8")
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
        event_number = len(list((root / RECOVERY_EVENT_DIRNAME).glob("*.event"))) + 1
        event_id = f"11111111-1111-4111-8111-{event_number:012d}"
        identity = subprocess.CompletedProcess([], 0, f"{pane.target}\t{pane.pane_id}\t{pane.window_id}\n", "")
        with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=identity), patch("omo_manager.omo_pending_watch.exact_codex_tail", return_value=(True, lines)):
            record_terminal_delivery_failure(root, pane.target, event_id, "status=not_codex")
        receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
        receipt = receipt_dir / f"{event_id}.receipt"
        with patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, lines)), patch("omo_manager.omo_codex_start.verify_same_pane"):
            record_recovery_evidence(root, pane, receipt, event_id)
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
            event_path = root / fields["event_file"]
            event_text = event_path.read_text(encoding="utf-8")
            event_fields = dict(item.split("=", 1) for item in event_text.strip().split(";"))
            event_fields["tail_sha256"] = digest
            updated_event = ";".join(f"{key}={value}" for key, value in event_fields.items()) + "\n"
            event_path.write_text(updated_event, encoding="utf-8")
            event_path.chmod(0o600)
            fields["event_sha256"] = hashlib.sha256(updated_event.encode("utf-8")).hexdigest()
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
        self.assertIn("OMO_AGENT_TMUX_TARGET=cfg:2.0", command)
        self.assertIn("--model gpt-5.6-terra", command)
        self.assertIn("model_reasoning_effort=", command)
        self.assertIn("check_for_update_on_startup=false", command)
        self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)
        self.assertIn("cd '/tmp/work logs'", command)
        self.assertIn("printf '%s\\n' '[marker]'", command)

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
                require_recovery_target(pane, str(receipt), root)
                consume_recovery_receipt(root, str(receipt))
                with self.assertRaisesRegex(StartError, "already consumed"):
                    require_recovery_target(pane, str(receipt), root)

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
                    require_recovery_target(pane, str(receipt), root)
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
                    require_recovery_target(pane, str(copied), root)
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
                    require_recovery_target(pane, str(forged), root)
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
                    require_recovery_target(pane, str(receipt), root)
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
                    require_recovery_target(pane, str(receipt), root)
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
                    require_recovery_target(pane, str(receipt))
            shell = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=shell), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "not a verified non-Codex"):
                    require_recovery_target(shell, str(receipt))
            capture.assert_not_called()
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, [])):
                with self.assertRaisesRegex(StartError, "empty status capture"):
                    require_recovery_target(pane, str(receipt))
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(False, [])):
                with self.assertRaisesRegex(StartError, "capture failed"):
                    require_recovery_target(pane, str(receipt))
            stale = self.write_recovery_receipt(root, pane, ["delivery failed"], observed_at=datetime.now(timezone.utc).replace(year=2020))
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "stale"):
                    require_recovery_target(pane, str(stale))
            capture.assert_not_called()
            mismatched_digest = self.write_recovery_receipt(root, pane, ["delivery failed"], digest_lines=["different output"])
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, ["delivery failed"])):
                with self.assertRaisesRegex(StartError, "status capture changed"):
                    require_recovery_target(pane, str(mismatched_digest))
            changed = Pane("cfg:2.0", "%9", "@9", "bunx", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=changed), patch("omo_manager.omo_codex_start.exact_tail") as capture:
                with self.assertRaisesRegex(StartError, "identity changed"):
                    require_recovery_target(pane, str(receipt))
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
