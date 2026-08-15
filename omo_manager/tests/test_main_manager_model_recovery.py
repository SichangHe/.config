from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_main_manager_model_recovery import (
    FAILED_MODEL,
    FATAL_ERROR,
    MAIN_MANAGER_TARGET,
    Args,
    Authority,
    Binding,
    Handoff,
    ManagerEnvironment,
    RecoveryError,
    bind,
    guarded_status_session_id,
    parse_args,
    probe_model,
    read_authority,
    recover,
    reserve_handoff,
    finish_handoff,
    require_fatal_state,
    verify_continuity,
)
from omo_manager.omo_codex_start import Pane
from omo_manager.omo_manager_rotate import LaunchMetadata


class MainManagerModelRecoveryTests(unittest.TestCase):
    SESSION_ID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"

    def args(self, root: Path) -> Args:
        return Args(
            root=root,
            model="gpt-5.6-terra",
            authority_file=Path("manager_mail/authority.txt"),
            authority_lines=(3, 3),
            authority_envelope=Path("authority-envelope.md"),
            handoff_output=root / "state" / "handoff.txt",
            startup_timeout_s=4.0,
            model_probe_timeout_s=4.0,
            dry_run=False,
        )

    def binding(self, root: Path) -> Binding:
        state = root / "state"
        pane = Pane(MAIN_MANAGER_TARGET, "%42", "@9", "bun", root, 4242)
        launch = LaunchMetadata(
            FAILED_MODEL,
            "xhigh",
            "inferred",
            5252,
            ("bunx", "@openai/codex", "--model", FAILED_MODEL, "--config", 'model_reasoning_effort="xhigh"'),
        )
        return Binding(pane, launch, ManagerEnvironment(root, state))

    def authority(self, root: Path) -> Authority:
        return Authority(
            root / "manager_mail" / "authority.txt",
            (3, 3),
            1,
            2,
            3,
            4,
            "a" * 64,
            root / "authority-envelope.md",
            5,
            6,
            7,
            8,
            "b" * 64,
        )

    def write_authority(self, root: Path, text: str | None = None) -> Path:
        mail = root / "manager_mail"
        mail.mkdir(mode=0o700)
        mail.chmod(0o700)
        source = mail / "authority.txt"
        source.write_text(
            text
            or "Subject: recovery\n\nmain-manager-model-recovery: target=wl:1 action=resume-same-pane model=gpt-5.6-terra replacement-pane=forbidden\n",
            encoding="utf-8",
        )
        source.chmod(0o600)
        envelope = root / "authority-envelope.md"
        selected = "".join(source.read_text(encoding="utf-8").splitlines(keepends=True)[2:3])
        envelope.write_text(
            '<human_instruction authoritative="true" source="manager_mail/authority.txt:3-3">\n'
            + selected
            + "</human_instruction>\n",
            encoding="utf-8",
        )
        envelope.chmod(0o600)
        return source

    def test_parser_has_no_target_override_and_rejects_the_failed_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir(mode=0o700)
            parsed = parse_args(
                [
                    "--root",
                    str(root),
                    "--model",
                    "gpt-5.6-terra",
                    "--authority-file",
                    "manager_mail/authority.txt",
                    "--authority-lines",
                    "3-3",
                    "--authority-envelope",
                    str(root / "authority-envelope.md"),
                    "--handoff-output",
                    str(root / "state" / "handoff.txt"),
                ]
            )
            self.assertEqual("gpt-5.6-terra", parsed.model)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(
                    [
                        "--root",
                        str(root),
                        "--model",
                        FAILED_MODEL,
                        "--authority-file",
                        "manager_mail/authority.txt",
                        "--authority-lines",
                        "3-3",
                        "--authority-envelope",
                        str(root / "authority-envelope.md"),
                        "--handoff-output",
                        str(root / "state" / "handoff.txt"),
                    ]
                )

    def test_authority_requires_one_exact_human_instruction_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.write_authority(root)
            authority = read_authority(self.args(root))
            self.assertEqual(source, authority.source_path)
            self.assertEqual((3, 3), authority.lines)

            envelope = root / "authority-envelope.md"
            envelope.write_text(
                '<manager_delegation from="wl:18"> Recover the same tmux pane at wl:1 and resume its session. Do not launch a replacement pane.\n',
                encoding="utf-8",
            )
            envelope.chmod(0o600)
            with self.assertRaisesRegex(RecoveryError, "authority envelope"):
                read_authority(self.args(root))

    def test_authority_envelope_must_bind_the_exact_file_and_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.write_authority(root)
            envelope = root / "authority-envelope.md"
            envelope.write_text(
                '<human_instruction authoritative="true" source="manager_mail/authority.txt:2-3">\n'
                + source.read_text(encoding="utf-8")
                + "</human_instruction>\n",
                encoding="utf-8",
            )
            envelope.chmod(0o600)
            with self.assertRaisesRegex(RecoveryError, "does not bind"):
                read_authority(self.args(root))

    def test_authority_rejects_a_denial_that_contains_the_old_keyword_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_authority(
                root,
                "Subject: recovery\n\nDo not resume the session in the same pane at wl:1. Do not launch a replacement pane.\n",
            )
            with self.assertRaisesRegex(RecoveryError, "exact positive"):
                read_authority(self.args(root))

    def test_authority_grant_must_name_the_requested_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_authority(root)
            with self.assertRaisesRegex(RecoveryError, "requested model"):
                read_authority(replace(self.args(root), model="gpt-5.6-luna"))

    def test_bind_rejects_any_pane_other_than_the_hard_coded_main_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = Pane("other:1.0", "%42", "@9", "bun", root, 4242)
            with patch("omo_manager.omo_main_manager_model_recovery.resolve_pane", return_value=pane):
                with self.assertRaisesRegex(RecoveryError, "exact live Codex manager"):
                    bind(self.args(root))

    def test_model_probe_is_ephemeral_read_only_and_requires_schema_output(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps({"available": True}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("omo_manager.omo_main_manager_model_recovery.subprocess.run", side_effect=fake_run):
            probe_model("gpt-5.6-terra", "xhigh", 4.0)

        command = calls[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertEqual("gpt-5.6-terra", command[command.index("--model") + 1])

    def test_reserve_and_finish_handoff_keep_one_private_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            handoff = reserve_handoff(state / "handoff.txt", "phase: prepared\n")
            finish_handoff(handoff, "succeeded", "ready")
            text = (state / "handoff.txt").read_text(encoding="utf-8")
            self.assertIn("phase: prepared", text)
            self.assertIn("final-outcome: succeeded", text)
            self.assertEqual(0o600, os.stat(state / "handoff.txt").st_mode & 0o777)

    def test_recovery_resumes_the_captured_session_in_the_same_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir(mode=0o700)
            args = self.args(root)
            binding = self.binding(root)
            authority = self.authority(root)
            handoff = Handoff(root / "state" / "handoff.txt", 1, 2)
            with (
                patch("omo_manager.omo_main_manager_model_recovery.read_authority", return_value=authority),
                patch("omo_manager.omo_main_manager_model_recovery.bind", return_value=binding),
                patch("omo_manager.omo_main_manager_model_recovery.probe_model"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_binding") as verify_binding,
                patch("omo_manager.omo_main_manager_model_recovery.capture_session", return_value=self.SESSION_ID),
                patch("omo_manager.omo_main_manager_model_recovery.verify_authority") as verify_authority,
                patch("omo_manager.omo_main_manager_model_recovery.reserve_handoff", return_value=handoff),
                patch("omo_manager.omo_main_manager_model_recovery.respawn_codex") as respawn,
                patch("omo_manager.omo_main_manager_model_recovery.wait_started", return_value="ready"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_continuity") as continuity,
                patch("omo_manager.omo_main_manager_model_recovery.finish_handoff") as finish,
            ):
                self.assertEqual("ready", recover(args))

            command = respawn.call_args.args[1]
            self.assertIn(f"resume {self.SESSION_ID}", command)
            self.assertIn("--model gpt-5.6-terra", command)
            self.assertIn('model_reasoning_effort="xhigh"', command)
            self.assertIn('tui.resume_cwd="current"', command)
            self.assertIn(f"--cd {root}", command)
            self.assertIn("OMO_MANAGER_TMUX_TARGET=wl:1.0", command)
            self.assertGreaterEqual(verify_binding.call_count, 2)
            self.assertGreaterEqual(verify_authority.call_count, 2)
            continuity.assert_called_once_with(args, binding, authority, self.SESSION_ID)
            finish.assert_called_once_with(handoff, "succeeded", "ready")

    def test_model_probe_failure_cannot_replace_wl_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.args(root)
            with (
                patch("omo_manager.omo_main_manager_model_recovery.read_authority", return_value=self.authority(root)),
                patch("omo_manager.omo_main_manager_model_recovery.bind", return_value=self.binding(root)),
                patch("omo_manager.omo_main_manager_model_recovery.probe_model", side_effect=RecoveryError("model unavailable")),
                patch("omo_manager.omo_main_manager_model_recovery.respawn_codex") as respawn,
                self.assertRaisesRegex(RecoveryError, "model unavailable"),
            ):
                recover(args)
            respawn.assert_not_called()

    def test_dry_run_never_queries_or_replaces_wl_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = replace(self.args(root), dry_run=True)
            binding = self.binding(root)
            with (
                patch("omo_manager.omo_main_manager_model_recovery.read_authority", return_value=self.authority(root)),
                patch("omo_manager.omo_main_manager_model_recovery.bind", return_value=binding),
                patch("omo_manager.omo_main_manager_model_recovery.probe_model"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_binding"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_authority") as verify_authority,
                patch("omo_manager.omo_main_manager_model_recovery.capture_session") as capture,
                patch("omo_manager.omo_main_manager_model_recovery.respawn_codex") as respawn,
            ):
                self.assertEqual("dry-run", recover(args))
            capture.assert_not_called()
            respawn.assert_not_called()
            verify_authority.assert_called_once()

    def test_unverified_post_respawn_state_is_recorded_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir(mode=0o700)
            args = self.args(root)
            binding = self.binding(root)
            handoff = Handoff(root / "state" / "handoff.txt", 1, 2)
            with (
                patch("omo_manager.omo_main_manager_model_recovery.read_authority", return_value=self.authority(root)),
                patch("omo_manager.omo_main_manager_model_recovery.bind", return_value=binding),
                patch("omo_manager.omo_main_manager_model_recovery.probe_model"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_binding"),
                patch("omo_manager.omo_main_manager_model_recovery.capture_session", return_value=self.SESSION_ID),
                patch("omo_manager.omo_main_manager_model_recovery.verify_authority"),
                patch("omo_manager.omo_main_manager_model_recovery.reserve_handoff", return_value=handoff),
                patch("omo_manager.omo_main_manager_model_recovery.respawn_codex"),
                patch("omo_manager.omo_main_manager_model_recovery.wait_started", return_value="ready"),
                patch("omo_manager.omo_main_manager_model_recovery.verify_continuity", side_effect=RecoveryError("session mismatch")),
                patch("omo_manager.omo_main_manager_model_recovery.finish_handoff") as finish,
                self.assertRaisesRegex(RecoveryError, "session mismatch"),
            ):
                recover(args)
            finish.assert_called_once_with(handoff, "completion-unknown", "RecoveryError")

    def test_continuity_rebinds_the_new_process_to_the_requested_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir(mode=0o700)
            args = self.args(root)
            expected = self.binding(root)
            current = Pane(MAIN_MANAGER_TARGET, "%42", "@9", "bun", root, 6262)
            resumed = LaunchMetadata(
                args.model,
                expected.launch.reasoning_effort,
                "inferred",
                7272,
                ("bunx", "@openai/codex", "--model", args.model, "--config", 'model_reasoning_effort="xhigh"'),
            )
            with (
                patch("omo_manager.omo_main_manager_model_recovery.resolve_pane", return_value=current),
                patch("omo_manager.omo_main_manager_model_recovery.live_launch", return_value=resumed) as live,
                patch("omo_manager.omo_main_manager_model_recovery.manager_environment", return_value=expected.environment),
                patch("omo_manager.omo_main_manager_model_recovery.guarded_status_session_id", return_value=(self.SESSION_ID, "")),
            ):
                verify_continuity(args, expected, self.authority(root), self.SESSION_ID)
            live.assert_called_once_with(current, args.model)

    def test_continuity_rejects_a_changed_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self.binding(root)
            current = Pane(MAIN_MANAGER_TARGET, "%42", "@9", "bun", root / "other", 6262)
            with patch("omo_manager.omo_main_manager_model_recovery.resolve_pane", return_value=current), self.assertRaisesRegex(RecoveryError, "same-pane recovery"):
                verify_continuity(self.args(root), expected, self.authority(root), self.SESSION_ID)

    def test_guarded_status_query_refuses_a_server_identity_mismatch_before_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pane = self.binding(Path(tmp)).pane
            calls: list[list[str]] = []

            def fake_tmux(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[0] == "capture-pane":
                    return subprocess.CompletedProcess(command, 0, "old output\n", "")
                if command[0] == "set-buffer":
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "if-shell":
                    return subprocess.CompletedProcess(command, 1, "", "identity changed")
                if command[0] == "delete-buffer":
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.fail(f"unexpected tmux command: {command}")

            with (
                patch("omo_manager.omo_main_manager_model_recovery.same_pane", return_value=pane),
                patch("omo_manager.omo_main_manager_model_recovery.tmux_run", side_effect=fake_tmux),
                self.assertRaisesRegex(RecoveryError, "status paste and Enter"),
            ):
                guarded_status_session_id(pane, 1.0, lambda: None)

            guarded = next(command for command in calls if command[0] == "if-shell")
            self.assertIn("paste-buffer", guarded[5])
            self.assertFalse(any(command[0] in {"paste-buffer", "send-keys"} for command in calls))

    def test_revoked_authority_prevents_a_guarded_status_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pane = self.binding(Path(tmp)).pane
            calls: list[list[str]] = []

            def fake_tmux(command: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[0] == "capture-pane":
                    return subprocess.CompletedProcess(command, 0, "old output\n", "")
                if command[0] in {"set-buffer", "delete-buffer"}:
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.fail(f"unexpected tmux command: {command}")

            def revoked() -> None:
                raise RecoveryError("authority revoked")

            with (
                patch("omo_manager.omo_main_manager_model_recovery.same_pane", return_value=pane),
                patch("omo_manager.omo_main_manager_model_recovery.tmux_run", side_effect=fake_tmux),
                self.assertRaisesRegex(RecoveryError, "authority revoked"),
            ):
                guarded_status_session_id(pane, 1.0, revoked)

            self.assertFalse(any(command[0] == "if-shell" for command in calls))
            self.assertFalse(any(command[0] in {"paste-buffer", "send-keys"} for command in calls))

    def test_fatal_state_accepts_only_the_known_unadorned_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pane = self.binding(Path(tmp)).pane
            exact = ["────", FATAL_ERROR, "  gpt-5.6-sol"]
            with patch("omo_manager.omo_main_manager_model_recovery.captured_lines", return_value=exact):
                require_fatal_state(pane)

            decorated = ["────", FATAL_ERROR.removesuffix("}") + ',"code":"unsupported"}', "  gpt-5.6-sol"]
            with patch("omo_manager.omo_main_manager_model_recovery.captured_lines", return_value=decorated), self.assertRaises(RecoveryError):
                require_fatal_state(pane)


if __name__ == "__main__":
    _ = unittest.main()
