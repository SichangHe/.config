from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch

import yaml
from omo_manager.omo_codex_start import (
    Args,
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
    require_recovery_target,
    require_same_shell,
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

    def write_task(self, root: Path, *, runat: str = "cfg:2", status: str = "blocked", manager: bool = False) -> None:
        fields = {
            "version": "v1.0.0",
            "status": status,
            "blocked_on": "model capacity" if status == "blocked" else None,
            "runat": runat,
            "tool": "codex",
            "managerat": "cfg:1",
            "is_manager": manager,
            "pending_task_items": [],
        }
        text = "---\n" + yaml.safe_dump({key: value for key, value in fields.items() if value is not None}, sort_keys=False) + "---\n\nGoal.\n"
        (root / "worker.md").write_text(text, encoding="utf-8")
        (root / "TODO.md").write_text(f"current:\n\nworker.md {runat}\n", encoding="utf-8")

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
        result = subprocess.CompletedProcess([], 0, "wl:18.0\t%18\t@18\tzsh\t/tmp\n", "")
        for target in ("wl:18", "wl:18.0"):
            with self.subTest(target=target), patch("omo_manager.omo_codex_start.run", return_value=result):
                self.assertEqual(Pane("wl:18.0", "%18", "@18", "zsh", Path("/tmp")), resolve_pane(target))

    def test_resolve_pane_rejects_ambiguous_identity_fallbacks(self) -> None:
        mismatches = (("wl:18", "wl:1.0"), ("wl:18", "other:18.0"), ("wl:18.1", "wl:18.0"))
        for requested, resolved in mismatches:
            with self.subTest(requested=requested, resolved=resolved):
                result = subprocess.CompletedProcess([], 0, f"{resolved}\t%18\t@18\tzsh\t/tmp\n", "")
                with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "does not exist exactly"):
                    resolve_pane(requested)

    def test_resolve_pane_reports_empty_tmux_expansion_as_missing_target(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "target does not exist: wl:18"):
            resolve_pane("wl:18")

    def test_resolve_pane_rejects_near_empty_tmux_expansion(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\n\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "invalid identity"):
            resolve_pane("wl:18")

    def test_validate_task_requires_active_exact_todo_and_same_pane(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                self.assertFalse(validate_task(self.args(root), pane))
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
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
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
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
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
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
        changed = Pane("cfg:2.0", "%9", "@9", "bunx", Path("/tmp"))
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", side_effect=[pane, changed]),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
            patch("omo_manager.omo_codex_start.run") as run,
            self.assertRaisesRegex(StartError, "identity changed"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_not_called()

    def test_update_prompt_atomic_process_mismatch_refuses_input(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
        failed = subprocess.CompletedProcess([], 1, "", "process changed")
        with (
            patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
            patch("omo_manager.omo_codex_start.exact_tail", return_value=(True, self.update_prompt_lines())),
            patch("omo_manager.omo_codex_start.run", return_value=failed) as run,
            self.assertRaisesRegex(StartError, "failed to skip Codex update.*process changed"),
        ):
            skip_codex_update_prompt(pane, self.SESSION_ID)
        run.assert_called_once()

    def test_update_prompt_recovery_continues_same_resumed_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
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
                    "#{&&:#{==:#{window_id},@2},#{==:#{session_name}:#{window_index}.#{pane_index},cfg:2.0},#{==:#{pane_current_command},bunx}}",
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
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp/work logs"))
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("omo_manager.omo_codex_start.run", return_value=completed) as run, patch("omo_manager.omo_codex_start.verify_same_pane") as verify:
            respawn_codex(pane, "exec codex resume session")
        run.assert_called_once_with(
            [
                "tmux",
                "if-shell",
                "-F",
                "-t",
                "%2",
                "#{&&:#{==:#{pane_id},%2},#{==:#{window_id},@2},#{==:#{session_name}:#{window_index}.#{pane_index},cfg:2.0}}",
                "respawn-pane -k -t %2 -c '/tmp/work logs' 'exec codex resume session'",
                "run-shell 'exit 1'",
            ]
        )
        self.assertEqual([call(pane), call(pane)], verify.call_args_list)

    def test_respawn_refuses_atomic_identity_guard_failure_without_post_check(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp/work logs"))
        failed = subprocess.CompletedProcess([], 1, "", "moved")
        with patch("omo_manager.omo_codex_start.run", return_value=failed) as run, patch("omo_manager.omo_codex_start.verify_same_pane") as verify:
            with self.assertRaisesRegex(StartError, "failed to respawn Codex"):
                respawn_codex(pane, "exec codex resume session")
        run.assert_called_once()
        verify.assert_called_once_with(pane)

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
            ):
                self.assertEqual("running", start(args))
            capture.assert_called_once_with("%2", 240, 10.0)
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

    def test_validate_task_rejects_non_codex_tool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            path = root / "worker.md"
            path.write_text(path.read_text(encoding="utf-8").replace("tool: codex", "tool: pcodx"), encoding="utf-8")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with self.assertRaisesRegex(StartError, "only `tool: codex`"):
                validate_task(self.args(root), pane)

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
