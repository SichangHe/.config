import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import omo_manager.omo_codex_stop as codex_stop
from omo_manager.omo_codex_status import Report
from omo_manager.omo_codex_stop import (
    Args,
    LOCAL_ENV_PATH,
    close_authorized_human_pane,
    close_note,
    close_exited_codex_shell,
    close_tmux_target,
    codex_status,
    current_pane_id,
    extract_exit_resume_id,
    extract_new_status_session_id,
    extract_resume_id,
    extract_status_session_id,
    feedback_prompt,
    main,
    maybe_request_feedback,
    pane_id,
    parse_args,
    post_interrupt_output,
    query_status_session_id,
    record_close,
    resume_cmd,
    send_exit_keys,
    stop,
    validate_exited_codex_shell,
)


class CodexStopTests(unittest.TestCase):
    def setUp(self) -> None:
        session = patch("omo_manager.omo_codex_stop.target_session_name", return_value="cfg")
        session.start()
        self.addCleanup(session.stop)
        target = patch("omo_manager.omo_codex_stop.pane_target", return_value="cfg:1.0")
        target.start()
        self.addCleanup(target.stop)
        inspect = patch("omo_manager.omo_codex_stop.inspect", return_value=Report("ready", ["idle"]))
        inspect.start()
        self.addCleanup(inspect.stop)

    def test_current_pane_id_uses_calling_process_environment(self) -> None:
        with patch.dict(os.environ, {"TMUX_PANE": "%caller"}, clear=True), patch("omo_manager.omo_codex_stop.tmux") as tmux:
            self.assertEqual("%caller", current_pane_id())
        tmux.assert_not_called()

    def test_bound_process_fields_append_without_rebinding_legacy_positional_proof_fields(self) -> None:
        args = Args(
            "cfg:1",
            1.0,
            20,
            False,
            False,
            Path("/tmp/root"),
            "task.md",
            True,
            2.0,
            "human.txt",
            "human-sha",
            "",
            "cfg:1",
            "%42",
            "/tmp/proof",
            "/tmp/audit",
            "secret",
            "commitment",
        )
        self.assertEqual("/tmp/proof", args.bound_close_proof_path)
        self.assertEqual("/tmp/audit", args.bound_close_audit_path)
        self.assertEqual("secret", args.bound_close_proof_secret)
        self.assertEqual("commitment", args.bound_close_proof_commitment)
        self.assertEqual(0, args.bound_pane_pid)
        self.assertEqual(0, args.bound_pane_start_ticks)
        self.assertEqual("", args.bound_expected_session_id)

    def test_manager_config_path_is_resolved_from_the_implementation_not_path_wrapper(self) -> None:
        self.assertEqual(Path(codex_stop.__file__).resolve().with_name("local.env"), LOCAL_ENV_PATH)

    def test_close_exited_codex_shell_closes_only_unchanged_proven_shell(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        transcript = f'{{"accepted":true,"receipt":"specific-token"}}\nConversation interrupted\nTo continue this session, run codex resume {session_id}\n$ '
        with (
            patch("omo_manager.omo_codex_stop.pane_id", side_effect=["%42", "%42", "%42", "%42", ""]),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%99"),
            patch("omo_manager.omo_codex_stop.current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.inspect", return_value=Report("not_codex", ["$ "])),
            patch("omo_manager.omo_codex_stop.capture", return_value=transcript),
            patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
        ):
            close_exited_codex_shell("cfg:1", "%42", session_id, "specific-token")

        close.assert_called_once_with("cfg:1", "%42")

    def test_validate_exited_codex_shell_authenticates_without_closing(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        transcript = f'{{"accepted":true,"receipt":"specific-token"}}\nConversation interrupted\nTo continue this session, run codex resume {session_id}\n$ '
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%99"),
            patch("omo_manager.omo_codex_stop.current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.inspect", return_value=Report("not_codex", ["$ "])),
            patch("omo_manager.omo_codex_stop.capture", return_value=transcript),
            patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
        ):
            capture_sha256 = validate_exited_codex_shell("cfg:1", "%42", session_id, "specific-token")
        self.assertEqual(hashlib.sha256(transcript.encode()).hexdigest(), capture_sha256)
        close.assert_not_called()

    def test_close_exited_codex_shell_preserves_live_positional_call(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        with (
            patch("omo_manager.omo_codex_stop.validate_exited_codex_shell", return_value="a" * 64) as validate,
            patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
            patch("omo_manager.omo_codex_stop.pane_id", return_value=""),
        ):
            close_exited_codex_shell("cfg:1", "%42", session_id, "specific-token", 73)
        validate.assert_called_once_with("cfg:1", "%42", session_id, "specific-token", 73)
        close.assert_called_once_with("cfg:1", "%42")

    def test_close_exited_codex_shell_rejects_capture_drift_from_durable_intent(self) -> None:
        evidence_is_current = Mock(return_value=True)
        with (
            patch("omo_manager.omo_codex_stop.validate_exited_codex_shell", return_value="a" * 64),
            patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
            self.assertRaisesRegex(RuntimeError, "capture changed"),
        ):
            close_exited_codex_shell(
                "cfg:1",
                "%42",
                "11111111-2222-3333-4444-555555555555",
                "specific-token",
                expected_capture_sha256="b" * 64,
                evidence_is_current=evidence_is_current,
            )
        evidence_is_current.assert_not_called()
        close.assert_not_called()

    def test_close_exited_codex_shell_rejects_lifecycle_drift_before_close(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.validate_exited_codex_shell", return_value="a" * 64),
            patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
            self.assertRaisesRegex(RuntimeError, "lifecycle evidence changed"),
        ):
            close_exited_codex_shell(
                "cfg:1",
                "%42",
                "11111111-2222-3333-4444-555555555555",
                "specific-token",
                expected_capture_sha256="a" * 64,
                evidence_is_current=lambda: False,
            )
        close.assert_not_called()

    def test_close_authorized_human_pane_kills_only_the_exact_pane(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
        ):
            close_authorized_human_pane("%42", lambda: True)
        tmux.assert_called_once_with(["kill-pane", "-t", "%42"], check=True)

    def test_close_authorized_human_pane_refuses_to_report_success_before_shell_exit(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.current_command", return_value="codex"),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            self.assertRaisesRegex(RuntimeError, "did not exit to a shell"),
        ):
            close_authorized_human_pane("%42", lambda: True)
        tmux.assert_not_called()

    def test_status_fallback_does_not_submit_after_human_pane_identity_changes(self) -> None:
        before = "ready\n"
        still_input = f"{before}› /status\n"
        identity = iter((True, True, False))
        with (
            patch("omo_manager.omo_codex_stop.capture", side_effect=[before, still_input]),
            patch("omo_manager.omo_codex_stop.paste_text"),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            self.assertRaisesRegex(RuntimeError, "fallback status submission"),
        ):
            query_status_session_id("%42", 10, 0.1, lambda: next(identity))
        self.assertEqual(["send-keys", "-t", "%42", "Enter"], tmux.call_args.args[0])

    def test_status_pre_input_guard_runs_after_final_capture_and_before_paste(self) -> None:
        captured = False

        def final_capture(*_args: object) -> str:
            nonlocal captured
            captured = True
            return "ready\n"

        def reject_drift() -> None:
            self.assertTrue(captured)
            raise RuntimeError("lifecycle drift during final capture")

        with (
            patch("omo_manager.omo_codex_stop.guarded_capture", side_effect=final_capture),
            patch("omo_manager.omo_codex_stop.guarded_paste_text") as paste,
            patch("omo_manager.omo_codex_stop.guarded_tmux_command") as send,
            self.assertRaisesRegex(RuntimeError, "drift during final capture"),
        ):
            query_status_session_id(
                "%42",
                10,
                0.1,
                lambda: True,
                ("hwork:1", "%42"),
                True,
                4242,
                reject_drift,
            )
        paste.assert_not_called()
        send.assert_not_called()

    def test_close_exited_codex_shell_rejects_ambiguous_or_changed_state(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        transcript = f'{{"accepted":true,"receipt":"accepted-report-token"}}\nConversation interrupted\nTo continue this session, run codex resume {session_id}\n$ '
        cases = (
            (Report("ready", ["idle"]), "zsh", (transcript, transcript), session_id, "accepted-report-token", "exited non-Codex shell"),
            (Report("not_codex", ["shell"]), "bunx", (transcript, transcript), session_id, "accepted-report-token", "exited non-Codex shell"),
            (Report("not_codex", ["shell"]), "zsh", (transcript, transcript), "99999999-2222-3333-4444-555555555555", "accepted-report-token", "does not match"),
            (Report("not_codex", ["shell"]), "zsh", (transcript, transcript), session_id, "missing-report-token", "evidence is absent"),
            (Report("not_codex", ["shell"]), "zsh", (transcript, transcript + "changed"), session_id, "accepted-report-token", "changed during recovery"),
            (
                Report("not_codex", ["shell"]),
                "zsh",
                (transcript + "\nran unrelated command\n$ ", transcript + "\nran unrelated command\n$ "),
                session_id,
                "accepted-report-token",
                "shell activity",
            ),
            (
                Report("not_codex", ["shell"]),
                "zsh",
                (
                    transcript + f"\n$ # To continue this session, run codex resume {session_id}\n$ ",
                    transcript + f"\n$ # To continue this session, run codex resume {session_id}\n$ ",
                ),
                session_id,
                "accepted-report-token",
                "does not match",
            ),
            (
                Report("not_codex", ["shell"]),
                "zsh",
                (
                    f'{{"accepted":true,"receipt":"accepted-report-token"}}\nConversation interrupted\nTo continue this session, run codex resume 99999999-2222-3333-4444-555555555555\n$ codex\nConversation interrupted\nTo continue this session, run codex resume {session_id}\n$ ',
                )
                * 2,
                session_id,
                "accepted-report-token",
                "evidence is absent",
            ),
        )
        for report, command, captures, supplied_session, evidence, error in cases:
            with self.subTest(error=error):
                with (
                    patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
                    patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%99"),
                    patch("omo_manager.omo_codex_stop.current_command", return_value=command),
                    patch("omo_manager.omo_codex_stop.inspect", return_value=report),
                    patch("omo_manager.omo_codex_stop.capture", side_effect=captures),
                    patch("omo_manager.omo_codex_stop.guarded_tmux_shell_close") as close,
                    self.assertRaisesRegex(RuntimeError, error),
                ):
                    close_exited_codex_shell("cfg:1", "%42", supplied_session, evidence)
                close.assert_not_called()

    def test_codex_status_captures_pinned_pane_id(self) -> None:
        lines = ["› Use /skills to list available skills", "  gpt-5.6-terra"]
        with patch("omo_manager.omo_codex_stop.tail_pane_id", return_value=lines) as capture, patch("omo_manager.omo_codex_stop.tail") as symbolic:
            self.assertEqual("ready", codex_status("%42"))
        capture.assert_called_once_with("%42", 80)
        symbolic.assert_not_called()

    def test_pane_id_rejects_missing_exact_target_without_tmux_prefix_fallback(self) -> None:
        with patch("omo_manager.omo_codex_stop.exact_pane_id", return_value="") as exact, patch("omo_manager.omo_codex_stop.tmux") as tmux:
            self.assertEqual("", pane_id("wl:1.0"))
        exact.assert_called_once_with("wl:1.0")
        tmux.assert_not_called()

    def test_obsolete_preserve_pane_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--target", "cfg:1.0", "--preserve-pane"])

    def test_extract_resume_id_from_resume_command(self) -> None:
        text = "To resume, run codex resume 11111111-2222-3333-4444-555555555555\n"
        self.assertEqual("11111111-2222-3333-4444-555555555555", extract_resume_id(text))

    def test_extract_resume_id_from_resume_line(self) -> None:
        text = "Resume this session with 99999999-aaaa-bbbb-cccc-dddddddddddd when ready.\n"
        self.assertEqual("", extract_resume_id(text))

    def test_extract_exit_resume_id_from_continue_line_after_repaint(self) -> None:
        before = "› ready\ncodex resume 11111111-2222-3333-4444-555555555555\n"
        after = (
            "› ready\n\n"
            "To continue this session, run codex resume 99999999-aaaa-bbbb-cccc-dddddddddddd\n"
        )
        self.assertEqual("99999999-aaaa-bbbb-cccc-dddddddddddd", extract_exit_resume_id(before, after))

    def test_extract_exit_resume_id_ignores_stale_continue_line(self) -> None:
        before = "To continue this session, run codex resume 11111111-2222-3333-4444-555555555555\n"
        after = f"› ready\n{before}"
        self.assertEqual("", extract_exit_resume_id(before, after))

    def test_extract_status_session_id_from_status_box(self) -> None:
        text = "│  Session:              019e9ed9-6262-71c0-b4b3-72ffd4182e98       │\n"
        self.assertEqual("019e9ed9-6262-71c0-b4b3-72ffd4182e98", extract_status_session_id(text))

    def test_extract_new_status_session_id_handles_tui_repaint(self) -> None:
        before = "› Reply done\n  gpt-5.5 medium · Context 0% used\n"
        after = before + "/status\n│  Session:              019e9ed9-6262-71c0-b4b3-72ffd4182e98       │\n"
        self.assertEqual("019e9ed9-6262-71c0-b4b3-72ffd4182e98", extract_new_status_session_id(before, after))

    def test_extract_new_status_session_id_ignores_old_status_box(self) -> None:
        before = "/status\n│  Session:              11111111-2222-3333-4444-555555555555       │\n"
        after = before + "\n› ready\n"
        self.assertEqual("", extract_new_status_session_id(before, after))

    def test_post_interrupt_output_returns_only_new_tail(self) -> None:
        before = "agent output\ncodex resume 11111111-2222-3333-4444-555555555555\n"
        after = f"{before}To resume, run codex resume 99999999-aaaa-bbbb-cccc-dddddddddddd\n"
        self.assertEqual("To resume, run codex resume 99999999-aaaa-bbbb-cccc-dddddddddddd\n", post_interrupt_output(before, after))

    def test_main_prints_empty_session_id_when_missing(self) -> None:
        out = io.StringIO()
        with patch("omo_manager.omo_codex_stop.stop", return_value=""), contextlib.redirect_stdout(out):
            self.assertEqual(0, main(["--target", "cfg:1.0", "--dry-run"]))
        self.assertEqual("session_id:\n", out.getvalue())

    def test_record_close_appends_resume_note_to_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )
            text = task.read_text(encoding="utf-8")
        self.assertIn("session_id: `11111111-2222-3333-4444-555555555555`", text)
        self.assertNotIn("codex resume", text)

    def test_record_close_does_not_duplicate_existing_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            args = Args("cfg:1.0", 0.0, 10, False, False, root, "task.md")

            record_close(args, "11111111-2222-3333-4444-555555555555")
            record_close(args, "11111111-2222-3333-4444-555555555555")

            text = task.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("manager closed Codex agent"))

    def test_record_close_ignores_unrelated_session_id_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text(
                "runat: cfg:1 codex\n"
                "prior note with session_id: `11111111-2222-3333-4444-555555555555`\n",
                encoding="utf-8",
            )

            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )

            text = task.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("manager closed Codex agent"))

    def test_record_close_ignores_malformed_close_note_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text(
                "runat: cfg:1 codex\n"
                "(manager closed Codex agent text with tmux target `cfg:1.0` and session_id: `11111111-2222-3333-4444-555555555555`.)\n",
                encoding="utf-8",
            )

            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )

            text = task.read_text(encoding="utf-8")
        self.assertEqual(2, text.count("manager closed Codex agent"))

    def test_record_close_ignores_forged_close_note_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text(
                "runat: cfg:1 codex\n"
                "(manager closed Codex agent fabricated-record; tmux target `cfg:1.0`; session_id: `11111111-2222-3333-4444-555555555555`.)\n",
                encoding="utf-8",
            )

            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )

            text = task.read_text(encoding="utf-8")
        self.assertEqual(2, text.count("manager closed Codex agent"))

    def test_record_close_requires_unmodified_close_note_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text(
                "runat: cfg:1 codex\n"
                " (manager closed Codex agent 07-14 11:00 PDT; tmux target `cfg:1.0`; session_id: `11111111-2222-3333-4444-555555555555`.)\n",
                encoding="utf-8",
            )

            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )

            text = task.read_text(encoding="utf-8")
        self.assertEqual(2, text.count("manager closed Codex agent"))

    def test_record_close_ignores_no_session_note_for_different_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text(
                "runat: cfg:1 codex\n"
                "(manager closed Codex agent 07-14 11:00 PDT; tmux target `cfg:2.0`; Codex session id not found in captured tmux output.)\n",
                encoding="utf-8",
            )

            record_close(Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"), "")

            text = task.read_text(encoding="utf-8")
        self.assertEqual(2, text.count("manager closed Codex agent"))

    def test_record_close_retry_after_partial_failure_does_not_duplicate_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            args = Args("cfg:1.0", 0.0, 10, False, False, root, "task.md")

            with patch("omo_manager.omo_codex_stop.move_todo_to_previous", side_effect=RuntimeError("TODO locked")):
                with self.assertRaisesRegex(RuntimeError, "TODO locked"):
                    record_close(args, "11111111-2222-3333-4444-555555555555")
            record_close(args, "11111111-2222-3333-4444-555555555555")

            text = task.read_text(encoding="utf-8")
        self.assertEqual(1, text.count("manager closed Codex agent"))

    def test_record_close_moves_todo_current_entry_to_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text(
                "current:\n\nother.md cfg:2\ntask.md cfg:1\n\nprevious:\nold.md cfg:0 (done)\n",
                encoding="utf-8",
            )
            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"),
                "11111111-2222-3333-4444-555555555555",
            )
            todo = (root / "TODO.md").read_text(encoding="utf-8")
        self.assertIn("current:\n\nother.md cfg:2\n\nprevious:\ntask.md cfg:1\nold.md cfg:0 (done)\n", todo)

    def test_record_close_moves_absolute_todo_entry_as_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "nested" / "task.md"
            task.parent.mkdir()
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text(f"current:\n{task} cfg:1\n", encoding="utf-8")
            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, str(task)),
                "11111111-2222-3333-4444-555555555555",
            )
            todo = (root / "TODO.md").read_text(encoding="utf-8")
        self.assertIn("previous:\nnested/task.md cfg:1\n", todo)
        self.assertNotIn(str(root), todo)

    def test_record_close_normalizes_absolute_todo_entry_when_called_with_relative_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "nested" / "task.md"
            task.parent.mkdir()
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text(f"current:\n{task} cfg:1\n", encoding="utf-8")
            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "nested/task.md"),
                "11111111-2222-3333-4444-555555555555",
            )
            todo = (root / "TODO.md").read_text(encoding="utf-8")
        self.assertIn("previous:\nnested/task.md cfg:1\n", todo)
        self.assertNotIn(str(root), todo)

    def test_record_close_normalizes_existing_absolute_previous_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "nested" / "task.md"
            task.parent.mkdir()
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            _ = (root / "TODO.md").write_text(f"current:\nnested/task.md cfg:1\n\nprevious:\n{task} cfg:1 old\n", encoding="utf-8")
            record_close(
                Args("cfg:1.0", 0.0, 10, False, False, root, "nested/task.md"),
                "11111111-2222-3333-4444-555555555555",
            )
            todo = (root / "TODO.md").read_text(encoding="utf-8")
        self.assertIn("previous:\nnested/task.md cfg:1 old\n", todo)
        self.assertNotIn(str(root), todo)

    def test_close_note_omits_year(self) -> None:
        text = close_note("cfg:1.0", "11111111-2222-3333-4444-555555555555", datetime(2026, 6, 6, 11, 18, tzinfo=timezone.utc))
        self.assertIn("06-06 11:18 UTC", text)
        self.assertNotIn("2026", text)

    def test_record_close_appends_no_session_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            record_close(Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"), "")
            text = task.read_text(encoding="utf-8")
        self.assertIn("Codex session id not found in captured tmux output", text)

    def test_main_records_session_id_when_task_file_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            _ = task.write_text("runat: cfg:1 codex\n", encoding="utf-8")
            out = io.StringIO()
            with patch("omo_manager.omo_codex_stop.stop", return_value="11111111-2222-3333-4444-555555555555"), contextlib.redirect_stdout(
                out
            ):
                self.assertEqual(
                    0,
                    main(["--target", "cfg:1.0", "--root", str(root), "--task-file", "task.md"]),
                )
            text = task.read_text(encoding="utf-8")
        self.assertIn("session_id: 11111111-2222-3333-4444-555555555555\n", out.getvalue())
        self.assertIn("resume_cmd: codex resume 11111111-2222-3333-4444-555555555555\n", out.getvalue())
        self.assertIn("tmux target `cfg:1.0`", text)

    def test_resume_cmd_defaults_to_pcodx_and_uses_task_tool(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:1.0", 0.0, 10, False, False), session_id))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("runat: cfg:1 pcodx\n", encoding="utf-8")
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"), session_id))
            _ = (root / "task.md").write_text("runat: cfg:1 codex\n", encoding="utf-8")
            self.assertEqual(f"codex resume {session_id}", resume_cmd(Args("cfg:1.0", 0.0, 10, False, False, root, "task.md"), session_id))
            _ = (root / "task.md").write_text("old notes\nrunat: cfg:1 codex\n\nrunat: cfg:2 pcodx\n", encoding="utf-8")
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:2.0", 0.0, 10, False, False, root, "task.md"), session_id))
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:9.0", 0.0, 10, False, False, root, "task.md"), session_id))
            _ = (root / "task.md").write_text("old notes\nrunat: cfg:2 codex\n\nrunat: cfg:9 pcodx\n", encoding="utf-8")
            self.assertEqual(f"codex resume {session_id}", resume_cmd(Args("cfg:2.1", 0.0, 10, False, False, root, "task.md"), session_id))
            _ = (root / "task.md").write_text("runat: cfg:1 codex\n\nold notes\nrunat: cfg:2 pcodx\n", encoding="utf-8")
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:2.0", 0.0, 10, False, False, root, "task.md"), session_id))
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:9.0", 0.0, 10, False, False, root, "task.md"), session_id))
            _ = (root / "task.md").write_text("runat: cfg:2 pcodx\n\nold notes\nrunat: cfg:2 codex\n", encoding="utf-8")
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:2.0", 0.0, 10, False, False, root, "task.md"), session_id))
            self.assertEqual(f"pcodx resume {session_id}", resume_cmd(Args("cfg:9.0", 0.0, 10, False, False, root, "task.md"), session_id))

    def test_stop_preflights_task_file_before_sending_ctrl_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
                patch("omo_manager.omo_codex_stop.tmux") as tmux,
            ):
                with self.assertRaisesRegex(RuntimeError, "task file not found"):
                    stop(Args("cfg:1.0", 0.0, 10, False, False, Path(tmp), "missing.md"))
        tmux.assert_not_called()

    def test_stop_rejects_human_owned_target_before_inspection(self) -> None:
        with patch("omo_manager.omo_codex_stop.pane_id") as pane_id:
            with self.assertRaisesRegex(RuntimeError, "human-owned"):
                stop(Args("human:1.0", 0.0, 10, False, False))
        pane_id.assert_not_called()

    def test_stop_allows_one_pinned_human_pane_only_with_bound_direct_authority(self) -> None:
        authority = b"Subject: close task.md\n\nclose hwork:1 and leave every other pane alone\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority) as read_authority,
                patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.target_session_name", return_value="hwork"),
                patch("omo_manager.omo_codex_stop.pane_target", return_value="hwork:1.0"),
                patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("", "")),
                patch("omo_manager.omo_codex_stop.send_exit_keys"),
                patch("omo_manager.omo_codex_stop.wait_shell"),
                patch("omo_manager.omo_codex_stop.capture", return_value=""),
                patch("omo_manager.omo_codex_stop.close_authorized_human_pane") as close,
            ):
                self.assertEqual(
                    "",
                    stop(
                        Args(
                            "%42",
                            0.0,
                            10,
                            False,
                            False,
                            root,
                            "task.md",
                            True,
                            0.0,
                            "manager_mail/test.txt",
                            "a" * 64,
                            "hwork:1",
                        )
                    ),
                )
        self.assertEqual(
            [(("manager_mail/test.txt", "a" * 64), {}), (("manager_mail/test.txt", "a" * 64), {})],
            [(call.args, call.kwargs) for call in read_authority.call_args_list],
        )
        self.assertEqual("%42", close.call_args.args[0])

    def test_source1240_exact_replacement_sentence_authorizes_named_human_pane(self) -> None:
        authority = (
            b"Subject: Re: Low-priority task decisions\n\n"
            b"Replace the failed PCODX manager task.md at hwork:1 with one fresh plain-Codex manager inheriting all tasks and comments.\n"
            b"Just do it\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            args = Args(
                "hwork:1",
                0.0,
                10,
                False,
                False,
                root,
                "task.md",
                True,
                0.0,
                "manager_mail/source-1240.txt",
                "a" * 64,
                "hwork:1",
            )
            with patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority):
                codex_stop.validate_human_close_authorization(args)

            wrong = authority.replace(b"task.md at hwork:1", b"other.md at hwork:1")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=wrong),
                self.assertRaisesRegex(RuntimeError, "one exact task- and target-bound directive"),
            ):
                codex_stop.validate_human_close_authorization(args)

            for malformed in (
                authority.replace(b"comments.\n", b"comments. Do not close it.\n"),
                authority.replace(
                    b"Just do it\n",
                    b"Replace the failed PCODX manager task.md at hwork:1 with one fresh plain-Codex manager inheriting all tasks and comments.\n",
                ),
                authority.replace(b"Just do it\n", b"Do not replace that manager.\n"),
                authority.replace(b"Just do it\n", b"Cancel the replacement.\n"),
                authority.replace(b"Just do it\n", b"No replacement of that manager.\n"),
                authority.replace(b"Replace the failed", b"REPLACE THE FAILED"),
            ):
                with (
                    patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=malformed),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "one exact task- and target-bound directive|subject does not name the exact task file",
                    ),
                ):
                    codex_stop.validate_human_close_authorization(args)

    def test_manager_replacement_closes_exact_bound_human_pane_with_durable_proof(self) -> None:
        authority = b"Subject: close task.md\n\nclose hwork:1 and replace the failed PCODX manager\n"
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"

        def guarded(_target: str, _pane: str, command: list[str], _pid: int) -> str:
            if command[-1] == "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}":
                return "%42\thwork:1.0\n"
            if command[-1] == "#{session_name}:#{window_index}.#{pane_index}":
                return "hwork:1.0\n"
            if command[-1] == "#{session_name}":
                return "hwork\n"
            return "%42\n"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority),
                patch("omo_manager.omo_codex_stop.bound_guarded_read", side_effect=guarded),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=999),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.guarded_capture", return_value="› ready\n"),
                patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
                patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=(session_id, "status")),
                patch("omo_manager.omo_codex_stop.send_exit_keys"),
                patch("omo_manager.omo_codex_stop.wait_shell"),
                patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
            ):
                result = stop(
                    Args(
                        target="hwork:1",
                        wait_s=0.0,
                        lines=10,
                        dry_run=False,
                        allow_self=False,
                        root=root,
                        task_file="task.md",
                        no_feedback=True,
                        human_close_authorization_source="manager_mail/test.txt",
                        human_close_authorization_sha256="a" * 64,
                        human_close_authorized_target="hwork:1",
                        bound_symbolic_target="hwork:1",
                        bound_pane_id="%42",
                        bound_close_proof_path=str(root / "proof"),
                        bound_close_audit_path=str(root / "audit"),
                        bound_close_proof_secret="b" * 64,
                        bound_close_proof_commitment="c" * 64,
                        bound_pane_pid=4242,
                        bound_pane_start_ticks=999,
                        bound_expected_session_id=session_id,
                    )
                )
        self.assertEqual(session_id, result)
        self.assertEqual("%42", close.call_args.args[0])
        self.assertEqual(("hwork:1", "%42"), close.call_args.args[2:4])

    def test_manager_replacement_pre_input_drift_guard_blocks_every_pane_input(self) -> None:
        authority = b"Subject: close task.md\n\nclose hwork:1 and replace the failed PCODX manager\n"
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority),
                patch(
                    "omo_manager.omo_codex_stop.bound_guarded_read",
                    side_effect=("%42\n", "hwork\n", "hwork:1.0\n", "%42\thwork:1.0\n"),
                ),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=999),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.guarded_capture", return_value="› ready\n"),
                patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
                patch("omo_manager.omo_codex_stop.guarded_paste_text") as paste,
                patch("omo_manager.omo_codex_stop.guarded_tmux_command") as guarded_input,
                patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
                patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
                self.assertRaisesRegex(RuntimeError, "exact lifecycle drift"),
            ):
                stop(
                    Args(
                        target="hwork:1",
                        wait_s=0.0,
                        lines=10,
                        dry_run=False,
                        allow_self=False,
                        root=root,
                        task_file="task.md",
                        no_feedback=True,
                        human_close_authorization_source="manager_mail/test.txt",
                        human_close_authorization_sha256="a" * 64,
                        human_close_authorized_target="hwork:1",
                        bound_symbolic_target="hwork:1",
                        bound_pane_id="%42",
                        bound_close_proof_path=str(root / "proof"),
                        bound_close_audit_path=str(root / "audit"),
                        bound_close_proof_secret="b" * 64,
                        bound_close_proof_commitment="c" * 64,
                        bound_pane_pid=4242,
                        bound_pane_start_ticks=999,
                        bound_expected_session_id=session_id,
                        bound_pre_input_check=lambda: (_ for _ in ()).throw(RuntimeError("exact lifecycle drift")),
                    )
                )
        paste.assert_not_called()
        guarded_input.assert_not_called()
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_manager_replacement_pre_input_guard_accepts_session_bound_nonhuman_close(self) -> None:
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checked: list[bool] = []
            with (
                patch(
                    "omo_manager.omo_codex_stop.bound_guarded_read",
                    side_effect=(
                        "%42\n",
                        "work\n",
                        "work:1.0\n",
                        "%42\twork:1.0\n",
                        "%42\twork:1.0\n",
                        "%42\twork:1.0\n",
                    ),
                ),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=999),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.guarded_capture", return_value="› ready\n"),
                patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
                patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=(session_id, "status")),
                patch("omo_manager.omo_codex_stop.send_exit_keys"),
                patch("omo_manager.omo_codex_stop.wait_shell"),
                patch("omo_manager.omo_codex_stop.close_bound_tmux_target"),
            ):
                result = stop(
                    Args(
                        target="work:1",
                        wait_s=0.0,
                        lines=10,
                        dry_run=False,
                        allow_self=False,
                        root=root,
                        no_feedback=True,
                        bound_symbolic_target="work:1",
                        bound_pane_id="%42",
                        bound_close_proof_path=str(root / "proof"),
                        bound_close_audit_path=str(root / "audit"),
                        bound_close_proof_secret="b" * 64,
                        bound_close_proof_commitment="c" * 64,
                        bound_pane_pid=4242,
                        bound_pane_start_ticks=999,
                        bound_expected_session_id=session_id,
                        bound_pre_input_check=lambda: checked.append(True),
                    )
                )
        self.assertEqual(session_id, result)

    def test_nonhuman_lifecycle_guard_rechecks_after_status_paste_before_enter(self) -> None:
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        checks = 0

        def guard() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise RuntimeError("descendant reappeared")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch(
                    "omo_manager.omo_codex_stop.bound_guarded_read",
                    side_effect=("%42\n", "work\n", "work:1.0\n", *(["%42\twork:1.0\n"] * 10)),
                ),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=999),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.guarded_capture", return_value="› ready\n"),
                patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
                patch("omo_manager.omo_codex_stop.guarded_paste_text"),
                patch("omo_manager.omo_codex_stop.guarded_tmux_command") as tmux_input,
                patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
                patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
                self.assertRaisesRegex(RuntimeError, "descendant reappeared"),
            ):
                stop(
                    Args(
                        target="work:1",
                        wait_s=0.0,
                        lines=10,
                        dry_run=False,
                        allow_self=False,
                        root=root,
                        no_feedback=True,
                        bound_symbolic_target="work:1",
                        bound_pane_id="%42",
                        bound_close_proof_path=str(root / "proof"),
                        bound_close_audit_path=str(root / "audit"),
                        bound_close_proof_secret="b" * 64,
                        bound_close_proof_commitment="c" * 64,
                        bound_pane_pid=4242,
                        bound_pane_start_ticks=999,
                        bound_expected_session_id=session_id,
                        bound_pre_input_check=guard,
                    )
                )
        tmux_input.assert_not_called()
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_stop_rejects_human_authority_that_does_not_bind_exact_task_and_target_before_tmux(self) -> None:
        authority = b"Subject: task.md.old\n\nclose hwork:1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority),
                patch("omo_manager.omo_codex_stop.pane_id") as pane_id,
                self.assertRaisesRegex(RuntimeError, "does not name the exact task file"),
            ):
                stop(
                    Args(
                        "hwork:1",
                        0.0,
                        10,
                        False,
                        False,
                        root,
                        "task.md",
                        True,
                        0.0,
                        "manager_mail/test.txt",
                        "a" * 64,
                    )
                )
        pane_id.assert_not_called()

    def test_reply_human_authority_binds_task_through_quoted_exact_owner(self) -> None:
        authority = (
            b"Subject: Re: ESBMC pilot and threading question\n\n"
            b"Cancel this task and consider them overall done\n\n"
            b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
            b"It does not change task ownership.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            args = Args(
                "hwork:1",
                0.0,
                10,
                False,
                False,
                root,
                "task.md",
                True,
                0.0,
                "manager_mail/test.txt",
                "a" * 64,
            )
            with patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority):
                codex_stop.validate_human_close_authorization(args)

            crlf_authority = authority.replace(b"\n", b"\r\n")
            with patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=crlf_authority):
                codex_stop.validate_human_close_authorization(args)

    def test_reply_human_authority_rejects_ambiguous_or_wrong_thread_binding(self) -> None:
        cases = (
            b"> This is not from the responsible hwork:1 EDA/C++ owner.\n",
            b"> Do not take this from the responsible hwork:1 EDA/C++ owner.\n",
            (
                b"> This is a mailbox-compression summary of reports from the responsible other:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
                b"> This is a mailbox-compression summary of reports from the responsible other:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"> This is a mailbox-compression summary of reports from the responsible other:1 EDA/C++ owner. "
                b"It does not change task ownership. This is a mailbox-compression summary of reports from the responsible "
                b"hwork:1 EDA/C++ owner. It does not change task ownership.\n"
            ),
            (
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
                b">> This is a mailbox-compression summary of reports from the responsible other:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
                b"> > This is a mailbox-compression summary of reports from the responsible other:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
            (
                b"close other:1\n"
                b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
                b"It does not change task ownership.\n"
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            args = Args(
                "hwork:1",
                0.0,
                10,
                False,
                False,
                root,
                "task.md",
                True,
                0.0,
                "manager_mail/test.txt",
                "a" * 64,
            )
            for binding in cases:
                authority = b"Subject: Re: thread\n\nCancel this task\n\n" + binding
                with self.subTest(binding=binding), patch(
                    "omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority
                ), self.assertRaisesRegex(RuntimeError, "reply does not bind it"):
                    codex_stop.validate_human_close_authorization(args)

    def test_reply_human_authority_requires_one_direct_unquoted_cancellation(self) -> None:
        direct_bodies = (
            b"> Cancel this task\n",
            b"Cancel this task\nCancel this task\n",
            b"Close this task\n",
            b"Please consider this task done\n",
        )
        binding = (
            b"> This is a mailbox-compression summary of reports from the responsible hwork:1 EDA/C++ owner. "
            b"It does not change task ownership.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            args = Args(
                "hwork:1",
                0.0,
                10,
                False,
                False,
                root,
                "task.md",
                True,
                0.0,
                "manager_mail/test.txt",
                "a" * 64,
            )
            for direct_body in direct_bodies:
                authority = b"Subject: Re: thread\n\n" + direct_body + binding
                with self.subTest(direct_body=direct_body), patch(
                    "omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority
                ), self.assertRaisesRegex(RuntimeError, "reply does not bind it"):
                    codex_stop.validate_human_close_authorization(args)

    def test_stop_refuses_pinned_human_pane_that_moves_before_interrupt(self) -> None:
        authority = b"Subject: close task.md\n\nclose hwork:1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "task.md").write_text("---\nrunat: hwork:1\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_codex_stop.read_human_close_authorization", return_value=authority),
                patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
                patch("omo_manager.omo_codex_stop.target_session_name", return_value="hwork"),
                patch("omo_manager.omo_codex_stop.pane_target", side_effect=["hwork:1.0", "hother:9.0"]),
                patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("", "")),
                patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
                patch("omo_manager.omo_codex_stop.close_tmux_target") as close,
                self.assertRaisesRegex(RuntimeError, "disappeared before interrupt"),
            ):
                stop(
                    Args(
                        "%42",
                        0.0,
                        10,
                        False,
                        False,
                        root,
                        "task.md",
                        True,
                        0.0,
                        "manager_mail/test.txt",
                        "a" * 64,
                        "hwork:1",
                    )
                )
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_stop_rejects_non_codex_process_before_status_probe_or_interrupt(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.inspect", return_value=Report("not_codex", ["shell"])),
            patch("omo_manager.omo_codex_stop.query_status_session_id") as query,
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
            self.assertRaisesRegex(RuntimeError, "not a supported live Codex pane"),
        ):
            stop(Args("cfg:1.0", 0.0, 10, False, False))
        query.assert_not_called()
        interrupt.assert_not_called()

    def test_stop_cursor_agent_does_not_send_codex_status_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.md").write_text(
                "---\nversion: v1.0.0\nstatus: done\nrunat: cur:1\ntool: cursor\nmanagerat: mgr:1\nis_manager: false\npending_task_items: []\n---\nbody\n",
                encoding="utf-8",
            )
            with (
                patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
                patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
                patch("omo_manager.omo_codex_stop.target_session_name", return_value="cur"),
                patch("omo_manager.omo_codex_stop.pane_target", return_value="cur:1.0"),
                patch("omo_manager.omo_codex_stop.inspect", return_value=Report("running", ["Cursor Agent"])),
                patch("omo_manager.omo_codex_stop.query_status_session_id") as query,
                patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
                patch("omo_manager.omo_codex_stop.wait_shell"),
                patch("omo_manager.omo_codex_stop.capture", return_value=""),
                patch("omo_manager.omo_codex_stop.close_tmux_target") as close,
            ):
                self.assertEqual("", stop(Args("cur:1", 0.0, 10, False, False, root, "task.md", True, 0.0)))
        query.assert_not_called()
        interrupt.assert_called_once_with("%1")
        close.assert_called_once_with("%1")

    def test_stop_rejects_human_owned_target_resolved_from_pane_id(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
            patch("omo_manager.omo_codex_stop.target_session_name", return_value="hwork"),
        ):
            with self.assertRaisesRegex(RuntimeError, "human-owned"):
                stop(Args("%42", 0.0, 10, False, False))

    def test_stop_uses_resolved_pane_after_optional_pane_target(self) -> None:
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=(session_id, "")) as query,
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
            patch("omo_manager.omo_codex_stop.wait_shell", return_value=True),
            patch("omo_manager.omo_codex_stop.capture", return_value=""),
            patch("omo_manager.omo_codex_stop.close_tmux_target") as close,
        ):
            self.assertEqual(session_id, stop(Args("cfg:1", 0.0, 10, False, False)))
        query.assert_called_once_with("%42", 10, 0.0)
        interrupt.assert_called_once_with("%42")
        close.assert_called_once_with("%42")

    def test_stop_ignores_resume_id_from_pre_interrupt_transcript(self) -> None:
        visible_transcript = "codex resume 11111111-2222-3333-4444-555555555555\n"
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.capture", return_value=visible_transcript),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("", visible_transcript)),
            patch("omo_manager.omo_codex_stop.send_exit_keys"),
            patch("omo_manager.omo_codex_stop.wait_shell"),
            patch("omo_manager.omo_codex_stop.close_tmux_target"),
            patch("omo_manager.omo_codex_stop.tmux"),
        ):
            self.assertEqual("", stop(Args("cfg:1.0", 0.0, 10, False, False)))

    def test_stop_extracts_resume_id_from_post_interrupt_output(self) -> None:
        before = "codex resume 11111111-2222-3333-4444-555555555555\n"
        after = f"{before}To resume, run codex resume 99999999-aaaa-bbbb-cccc-dddddddddddd\n"
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.capture", return_value=after),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("", before)),
            patch("omo_manager.omo_codex_stop.send_exit_keys"),
            patch("omo_manager.omo_codex_stop.wait_shell"),
            patch("omo_manager.omo_codex_stop.close_tmux_target"),
            patch("omo_manager.omo_codex_stop.tmux"),
        ):
            self.assertEqual("99999999-aaaa-bbbb-cccc-dddddddddddd", stop(Args("cfg:1.0", 0.0, 10, False, False)))

    def test_stop_prefers_new_status_session_id(self) -> None:
        status_after = "before\n│  Session:              019e9ed9-6262-71c0-b4b3-72ffd4182e98       │\n"
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("019e9ed9-6262-71c0-b4b3-72ffd4182e98", status_after)),
            patch("omo_manager.omo_codex_stop.send_exit_keys"),
            patch("omo_manager.omo_codex_stop.wait_shell"),
            patch("omo_manager.omo_codex_stop.capture", return_value=status_after),
            patch("omo_manager.omo_codex_stop.close_tmux_target"),
        ):
            self.assertEqual("019e9ed9-6262-71c0-b4b3-72ffd4182e98", stop(Args("cfg:1.0", 0.0, 10, False, False)))

    def test_stop_does_not_interrupt_stale_resolved_pane(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.pane_id", side_effect=["%1", ""]),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=("", "")),
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
        ):
            with self.assertRaisesRegex(RuntimeError, "disappeared before interrupt"):
                stop(Args("cfg:1.0", 0.0, 10, False, False))
        interrupt.assert_not_called()

    def test_stop_refuses_bound_nonhuman_target_rebind_before_input(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
            patch(
                "omo_manager.omo_codex_stop.guarded_tmux_read",
                side_effect=(
                    "%42\n",
                    "vl\n",
                    "vl:2.0\n",
                    "› Use /skills to list available skills\n",
                    RuntimeError("bound read rejected"),
                ),
            ),
            patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
            patch("omo_manager.omo_codex_stop.paste_text") as paste,
            patch("omo_manager.omo_codex_stop.capture") as raw_capture,
            patch("omo_manager.omo_codex_stop.current_command") as raw_command,
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
            patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
            self.assertRaisesRegex(RuntimeError, "identity changed before status query"),
        ):
            stop(
                Args(
                    "vl:2",
                    0.0,
                    10,
                    False,
                    False,
                    no_feedback=True,
                    bound_symbolic_target="vl:2",
                    bound_pane_id="%42",
                )
            )
        paste.assert_not_called()
        raw_capture.assert_not_called()
        raw_command.assert_not_called()
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_bound_stop_rejects_rebind_during_first_resolution_without_raw_pane_access(self) -> None:
        with (
            patch(
                "omo_manager.omo_codex_stop.guarded_tmux_read",
                side_effect=RuntimeError("tmux symbolic target no longer owns exact pane"),
            ) as guarded,
            patch("omo_manager.omo_codex_stop.pane_id") as raw_pane_id,
            patch("omo_manager.omo_codex_stop.inspect") as raw_inspect,
            patch("omo_manager.omo_codex_stop.capture") as raw_capture,
            patch("omo_manager.omo_codex_stop.current_command") as raw_command,
            patch("omo_manager.omo_codex_stop.tmux") as raw_tmux,
            self.assertRaisesRegex(RuntimeError, "no longer owns"),
        ):
            stop(
                Args(
                    "vl:2",
                    0.0,
                    10,
                    False,
                    False,
                    no_feedback=True,
                    bound_symbolic_target="vl:2",
                    bound_pane_id="%42",
                )
            )
        guarded.assert_called_once_with(
            "vl:2", "%42", ["display-message", "-p", "-t", "vl:2", "#{pane_id}"]
        )
        raw_pane_id.assert_not_called()
        raw_inspect.assert_not_called()
        raw_capture.assert_not_called()
        raw_command.assert_not_called()
        raw_tmux.assert_not_called()

    def test_guarded_tmux_command_checks_binding_in_same_server_queue(self) -> None:
        def tmux_call(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            self.assertEqual(["if-shell", "-F", "-t", "vl:2"], argv[:4])
            self.assertEqual(
                "#{&&:#{==:#{pane_id},%42},#{&&:#{==:#{session_name},vl},#{==:#{window_index},2}}}",
                argv[4],
            )
            self.assertIn("send-keys -t %42 C-c", argv[5])
            token = argv[5].rsplit("display-message -p ", 1)[1]
            return subprocess.CompletedProcess(argv, 0, token + "\n", "")

        with patch("omo_manager.omo_codex_stop.tmux", side_effect=tmux_call):
            codex_stop.guarded_tmux_command("vl:2", "%42", ["send-keys", "-t", "%42", "C-c"])

    def test_guarded_tmux_command_can_bind_pane_process_in_same_server_queue(self) -> None:
        def tmux_call(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            self.assertIn("#{==:#{pane_id},%42}", argv[4])
            self.assertIn("#{==:#{pane_pid},4242}", argv[4])
            token = argv[5].rsplit("display-message -p ", 1)[1]
            return subprocess.CompletedProcess(argv, 0, token + "\n", "")

        with patch("omo_manager.omo_codex_stop.tmux", side_effect=tmux_call):
            codex_stop.guarded_tmux_command("vl:2", "%42", ["send-keys", "-t", "%42", "C-c"], 4242)

    def test_bound_stop_rejects_process_start_drift_before_any_later_access(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.bound_guarded_read", return_value="%42\n") as guarded,
            patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=1000),
            patch("omo_manager.omo_codex_stop.pane_id") as raw_pane,
            self.assertRaisesRegex(RuntimeError, "process identity changed"),
        ):
            stop(
                Args(
                    "vl:2",
                    0.0,
                    10,
                    False,
                    False,
                    no_feedback=True,
                    bound_symbolic_target="vl:2",
                    bound_pane_id="%42",
                    bound_pane_pid=4242,
                    bound_pane_start_ticks=999,
                )
            )
        guarded.assert_called_once()
        raw_pane.assert_not_called()

    def test_guarded_tmux_command_rejects_stale_pane_id_without_accepting_mutation(self) -> None:
        def stale_tmux(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            rejected = argv[6].rsplit("display-message -p ", 1)[1]
            self.assertIn("#{==:#{pane_id},%42}", argv[4])
            self.assertIn("kill-pane -t %42", argv[5])
            return subprocess.CompletedProcess(argv, 0, rejected + "\n", "")

        with (
            patch("omo_manager.omo_codex_stop.tmux", side_effect=stale_tmux),
            self.assertRaisesRegex(RuntimeError, "no longer owns"),
        ):
            codex_stop.guarded_tmux_command("vl:2", "%42", ["kill-pane", "-t", "%42"])

    def test_close_exited_shell_rechecks_ordinary_shell_in_same_tmux_queue_as_kill(self) -> None:
        def command_changed_tmux(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            self.assertEqual(["if-shell", "-F", "-t", "vl:2"], argv[:4])
            self.assertIn("#{==:#{pane_id},%42}", argv[4])
            for shell in codex_stop.SHELL_COMMANDS:
                self.assertIn(f"#{{==:#{{pane_current_command}},{shell}}}", argv[4])
            self.assertIn("kill-pane -t %42", argv[5])
            rejected = argv[6].rsplit("display-message -p ", 1)[1]
            return subprocess.CompletedProcess(argv, 0, rejected + "\n", "")

        with (
            patch("omo_manager.omo_codex_stop.validate_exited_codex_shell", return_value="a" * 64),
            patch("omo_manager.omo_codex_stop.tmux", side_effect=command_changed_tmux),
            self.assertRaisesRegex(RuntimeError, "no longer owns"),
        ):
            close_exited_codex_shell(
                "vl:2",
                "%42",
                "11111111-2222-3333-4444-555555555555",
                "specific-token",
            )

    def test_bound_nonhuman_rebind_at_paste_stops_before_any_later_input(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%42"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
            patch(
                "omo_manager.omo_codex_stop.guarded_tmux_read",
                side_effect=(
                    "%42\n",
                    "vl\n",
                    "vl:2.0\n",
                    "› Use /skills to list available skills\n",
                    "%42\tvl:2.0\n",
                    "› Use /skills to list available skills\n",
                ),
            ),
            patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
            patch("omo_manager.omo_codex_stop.guarded_paste_text", side_effect=RuntimeError("rebound at paste")),
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
            patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
            self.assertRaisesRegex(RuntimeError, "rebound at paste"),
        ):
            stop(
                Args(
                    "vl:2",
                    0.0,
                    10,
                    False,
                    False,
                    no_feedback=True,
                    bound_symbolic_target="vl:2",
                    bound_pane_id="%42",
                )
            )
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_bound_session_mismatch_never_interrupts_or_closes_pane(self) -> None:
        expected = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        mismatched = "11111111-2222-3333-4444-555555555555"
        with (
            patch(
                "omo_manager.omo_codex_stop.bound_guarded_read",
                side_effect=("%42\n", "vl\n", "vl:2.0\n", "%42\tvl:2.0\n"),
            ),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%caller"),
            patch(
                "omo_manager.omo_codex_stop.guarded_capture",
                return_value="› Use /skills to list available skills\n",
            ),
            patch("omo_manager.omo_codex_stop.report_from_lines", return_value=Report("ready", [])),
            patch(
                "omo_manager.omo_codex_stop.query_status_session_id",
                return_value=(mismatched, "fresh status response"),
            ) as query,
            patch("omo_manager.omo_codex_stop.send_exit_keys") as interrupt,
            patch("omo_manager.omo_codex_stop.close_bound_tmux_target") as close,
            self.assertRaisesRegex(RuntimeError, "session id mismatch before interrupt"),
        ):
            stop(
                Args(
                    "vl:2",
                    0.0,
                    10,
                    False,
                    False,
                    no_feedback=True,
                    bound_symbolic_target="vl:2",
                    bound_pane_id="%42",
                    bound_expected_session_id=expected,
                )
            )
        self.assertTrue(query.call_args.kwargs["strict_status_response"])
        interrupt.assert_not_called()
        close.assert_not_called()

    def test_bound_nonhuman_rebind_at_interrupt_never_sends_ctrl_c(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.guarded_tmux_command", side_effect=RuntimeError("rebound at interrupt")) as guarded,
            patch("omo_manager.omo_codex_stop.tmux") as raw_tmux,
            self.assertRaisesRegex(RuntimeError, "rebound at interrupt"),
        ):
            codex_stop.send_exit_keys("%42", lambda: True, ("vl:2", "%42"))
        guarded.assert_called_once_with("vl:2", "%42", ["send-keys", "-t", "%42", "C-c"])
        raw_tmux.assert_not_called()

    def test_bound_nonhuman_close_with_sibling_always_guards_exact_kill_pane(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.guarded_current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.window_panes") as window_panes,
            patch("omo_manager.omo_codex_stop.guarded_tmux_sequence", side_effect=RuntimeError("rebound at close")) as guarded,
            self.assertRaisesRegex(RuntimeError, "rebound at close"),
        ):
            codex_stop.close_bound_tmux_target("%42", lambda: True, "vl:2", "%42")
        guarded.assert_called_once_with("vl:2", "%42", [["kill-pane", "-t", "%42"]])
        window_panes.assert_not_called()

    def test_bound_close_uses_identity_bound_child_for_kill_and_proof(self) -> None:
        secret = "a" * 64
        commitment = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "park.yaml"
            proof = Path(tmp) / ".park.yaml.owner-stopped"
            with (
                patch("omo_manager.omo_codex_stop.guarded_current_command", return_value="zsh"),
                patch("omo_manager.omo_codex_stop.guarded_tmux_sequence", return_value="") as guarded,
            ):
                codex_stop.close_bound_tmux_target(
                    "%42", lambda: True, "vl:2", "%42", str(proof), str(audit), secret, commitment, 4242, 999
                )
        commands = guarded.call_args.args[2]
        self.assertEqual("run-shell", commands[0][0])
        self.assertIn("--kill-bound-and-write-close-proof", commands[0][1])
        self.assertNotIn("kill-window", commands[0][1])

    def test_bound_close_accepts_lost_guard_marker_only_with_durable_proof(self) -> None:
        secret = "a" * 64
        commitment = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "park.yaml"
            proof = Path(tmp) / ".park.yaml.owner-stopped"
            with (
                patch("omo_manager.omo_codex_stop.guarded_current_command", return_value="zsh"),
                patch(
                    "omo_manager.omo_codex_stop.guarded_tmux_sequence",
                    side_effect=RuntimeError("tmux symbolic target no longer owns the exact pane at command execution"),
                ),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=999),
                patch("omo_manager.omo_codex_stop.has_bound_close_proof", return_value=True),
            ):
                codex_stop.close_bound_tmux_target(
                    "%42", lambda: True, "vl:2", "%42", str(proof), str(audit), secret, commitment, 4242, 999
                )

    def test_bound_close_rejects_lost_guard_marker_without_durable_proof(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.guarded_current_command", return_value="zsh"),
            patch(
                "omo_manager.omo_codex_stop.guarded_tmux_sequence",
                side_effect=RuntimeError("tmux symbolic target no longer owns the exact pane at command execution"),
            ),
            self.assertRaisesRegex(RuntimeError, "symbolic target no longer owns"),
        ):
            codex_stop.close_bound_tmux_target("%42", lambda: True, "vl:2", "%42")

    def test_kill_bound_close_proof_revalidates_then_writes_after_absence(self) -> None:
        secret = "a" * 64
        commitment = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "park.yaml"
            proof = Path(tmp) / ".park.yaml.owner-stopped"
            with (
                patch("omo_manager.omo_codex_stop.pane_id", side_effect=["%42", "%42", "", ""]),
                patch("omo_manager.omo_codex_stop.process_start_ticks", side_effect=[999, None, None]),
                patch("omo_manager.omo_codex_stop.tmux") as tmux,
                patch("omo_manager.omo_codex_stop.write_bound_close_proof") as writer,
            ):
                codex_stop.kill_bound_and_write_close_proof(
                    "vl:2", "%42", 4242, 999, proof, audit, secret, commitment
                )
        tmux.assert_called_once_with(["kill-pane", "-t", "%42"], check=True)
        writer.assert_called_once_with(proof, audit, secret, commitment)

    def test_close_proof_internal_mode_rejects_uncommitted_or_incomplete_capability(self) -> None:
        secret = "a" * 64
        commitment = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            private.chmod(0o700)
            audit = private / "park.yaml"
            proof = private / ".park.yaml.owner-stopped"
            audit.write_text(
                "operation: park-unlinked\nstate: prepared\n"
                f"close_proof_commitment: {'0' * 64}\n",
                encoding="utf-8",
            )
            audit.chmod(0o600)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--write-bound-close-proof", str(proof), str(audit), secret, commitment]))
            self.assertFalse(proof.exists())
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["--write-bound-close-proof", str(proof), commitment])
            self.assertFalse(proof.exists())

    def test_close_proof_accepts_prepared_manager_replacement_capability(self) -> None:
        secret = "a" * 64
        commitment = hashlib.sha256(secret.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            private.chmod(0o700)
            replacement = private / "manager-replace.json"
            replacement_record = {
                "operation": "manager-replace",
                "state": "prepared",
                "close_proof_commitment": commitment,
                "old_target": "vl:2",
                "old_pane": {"id": "%42", "pid": 4242, "start_ticks": 999},
            }
            replacement_record["record_sha256"] = hashlib.sha256(
                json.dumps(replacement_record, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            replacement_bytes = (json.dumps(replacement_record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(0o600)
            audit = private / ".manager-replace.json.close-authority"
            proof = private / "..manager-replace.json.close-authority.owner-stopped"
            audit.write_text(
                json.dumps(
                    {
                        "operation": "manager-replace",
                        "state": "prepared",
                        "close_proof_commitment": commitment,
                        "audit_path": str(replacement),
                        "replacement_audit_sha256": hashlib.sha256(replacement_bytes).hexdigest(),
                        "old_target": "vl:2",
                        "old_pane_id": "%42",
                        "old_pane_pid": 4242,
                        "old_pane_start_ticks": 999,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            audit.chmod(0o600)

            self.assertEqual(0, main(["--write-bound-close-proof", str(proof), str(audit), secret, commitment]))

            self.assertTrue(codex_stop.has_bound_close_proof(proof, commitment))

    def test_bound_rebind_rejects_capture_and_shell_poll_without_raw_pane_reads(self) -> None:
        guard = ("vl:2", "%42")
        with (
            patch("omo_manager.omo_codex_stop.guarded_tmux_read", side_effect=RuntimeError("rebound")),
            patch("omo_manager.omo_codex_stop.capture") as raw_capture,
            patch("omo_manager.omo_codex_stop.current_command") as raw_command,
            self.assertRaisesRegex(RuntimeError, "rebound"),
        ):
            codex_stop.guarded_capture("%42", 80, guard)
        raw_capture.assert_not_called()
        raw_command.assert_not_called()
        with (
            patch("omo_manager.omo_codex_stop.guarded_current_command", side_effect=RuntimeError("rebound")),
            patch("omo_manager.omo_codex_stop.current_command") as raw_command,
            self.assertRaisesRegex(RuntimeError, "rebound"),
        ):
            codex_stop.wait_shell("%42", 10.0, guard)
        raw_command.assert_not_called()

    def test_stop_closes_pane_without_recovery_flag(self) -> None:
        session_id = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value="%1"),
            patch("omo_manager.omo_codex_stop.current_pane_id", return_value="%2"),
            patch("omo_manager.omo_codex_stop.query_status_session_id", return_value=(session_id, "")),
            patch("omo_manager.omo_codex_stop.send_exit_keys"),
            patch("omo_manager.omo_codex_stop.wait_shell"),
            patch("omo_manager.omo_codex_stop.capture", return_value=""),
            patch("omo_manager.omo_codex_stop.close_tmux_target") as close,
        ):
            self.assertEqual(session_id, stop(Args("cfg:1.0", 0.0, 10, False, False)))
        close.assert_called_once_with("%1")

    def test_maybe_request_feedback_prompts_ready_task_worker(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.codex_status", side_effect=["ready", "running", "ready"]),
            patch("omo_manager.omo_codex_stop.paste_text") as paste_text,
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            patch("omo_manager.omo_codex_stop.time.sleep"),
        ):
            maybe_request_feedback(Args("cfg:1.0", 0.0, 10, False, False, task_file="task.md", feedback_wait_s=1.0))
        self.assertIn("manager-triggered compaction", paste_text.call_args.args[1])
        self.assertEqual(["send-keys", "-t", "cfg:1.0", "Enter"], tmux.call_args.args[0])

    def test_maybe_request_feedback_skips_non_ready_worker(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.codex_status", return_value="running"),
            patch("omo_manager.omo_codex_stop.paste_text") as paste_text,
        ):
            maybe_request_feedback(Args("cfg:1.0", 0.0, 10, False, False, task_file="task.md"))
        paste_text.assert_not_called()

    def test_maybe_request_feedback_skips_cursor_agent_composer(self) -> None:
        lines = [
            "  → Add a follow-up",
            " ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
            "  Cursor Grok 4.6 Low · 51.3%",
            "  /tmp · main",
        ]
        with patch("omo_manager.omo_codex_stop.tail", return_value=lines), patch("omo_manager.omo_codex_stop.paste_text") as paste_text:
            maybe_request_feedback(Args("cfg:1.0", 0.0, 10, False, False, task_file="task.md", feedback_wait_s=1.0))
        paste_text.assert_not_called()

    def test_feedback_prompt_names_task_file_and_report_path(self) -> None:
        text = feedback_prompt("task.md")
        self.assertIn("REPORT_FILE=$(omo_report.sh --alloc-message-file)", text)
        self.assertIn('omo_report.sh --status done --message-file "$REPORT_FILE"', text)
        self.assertNotIn("--task-file", text)
        self.assertNotIn("--root", text)
        self.assertIn("Do not use cat, heredocs, or shell text injection for report bodies.", text)
        self.assertIn("whether you had partial-compaction access", text)
        self.assertIn("whether you used it", text)
        self.assertIn("PCODX ledger path", text)
        self.assertIn("forward it to OPC partial-compaction work", text)
        self.assertIn("at most five short bullets", text)

    def test_stop_dry_run_refuses_missing_target_before_printing(self) -> None:
        with patch("omo_manager.omo_codex_stop.pane_id", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "tmux target not found"):
                stop(Args("cfg:9.0", 0.0, 10, True, False))

    def test_query_status_session_id_pastes_status_and_submits_once(self) -> None:
        before = "ready\n"
        after = f"{before}/status\n│  Session:              019e9ed9-6262-71c0-b4b3-72ffd4182e98       │\n"
        with (
            patch("omo_manager.omo_codex_stop.capture", side_effect=[before, after]),
            patch("omo_manager.omo_codex_stop.paste_text") as paste_text,
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
        ):
            self.assertEqual(
                ("019e9ed9-6262-71c0-b4b3-72ffd4182e98", after),
                query_status_session_id("cfg:1.0", 10, 0.1),
            )
        paste_text.assert_called_once_with("cfg:1.0", "/status")
        self.assertEqual([["send-keys", "-t", "cfg:1.0", "Enter"]], [call.args[0] for call in tmux.call_args_list])

    def test_query_status_session_id_sends_one_fallback_enter_when_status_remains_in_input(self) -> None:
        before = "ready\n"
        still_input = f"{before}› /status\n"
        after = f"{before}/status\n│  Session:              019e9ed9-6262-71c0-b4b3-72ffd4182e98       │\n"
        with (
            patch("omo_manager.omo_codex_stop.capture", side_effect=[before, still_input, after]),
            patch("omo_manager.omo_codex_stop.paste_text"),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            patch("omo_manager.omo_codex_stop.time.sleep"),
        ):
            self.assertEqual(
                ("019e9ed9-6262-71c0-b4b3-72ffd4182e98", after),
                query_status_session_id("cfg:1.0", 10, 0.1),
            )
        self.assertEqual(
            [["send-keys", "-t", "cfg:1.0", "Enter"], ["send-keys", "-t", "cfg:1.0", "Enter"]],
            [call.args[0] for call in tmux.call_args_list],
        )

    def test_query_status_session_id_strictly_uses_submitted_status_response(self) -> None:
        old = "019e9ed9-6262-71c0-b4b3-72ffd4182e98"
        new = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"
        before = f"/status\n│  Session:              {old}       │\nready\n"
        response = f"\n│  Session:              {new}       │\n"
        after = f"{before}/status{response}"
        with patch("omo_manager.omo_codex_stop.capture", side_effect=[before, after]), patch("omo_manager.omo_codex_stop.paste_text"), patch("omo_manager.omo_codex_stop.tmux"):
            self.assertEqual((new, response), query_status_session_id("cfg:1.0", 10, 0.1, strict_status_response=True))

    def test_query_status_session_id_captures_once_after_deadline(self) -> None:
        session_id = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"
        before = "ready\n"
        response = f"\n│  Session:              {session_id}       │\n"
        after = f"{before}/status{response}"
        with (
            patch("omo_manager.omo_codex_stop.capture", side_effect=[before, after]) as capture,
            patch("omo_manager.omo_codex_stop.paste_text"),
            patch("omo_manager.omo_codex_stop.tmux"),
            patch("omo_manager.omo_codex_stop.time.monotonic", side_effect=[0.0, 1.0]),
        ):
            self.assertEqual(
                (session_id, response),
                query_status_session_id("cfg:1.0", 10, 0.1, strict_status_response=True),
            )
        self.assertEqual(2, capture.call_count)

    def test_send_exit_keys_retries_ctrl_c_until_shell(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            patch("omo_manager.omo_codex_stop.current_command", side_effect=["bunx", "bunx", "zsh"]),
            patch("omo_manager.omo_codex_stop.time.sleep") as sleep,
        ):
            send_exit_keys("cfg:1.0")
        self.assertEqual(
            [
                ["send-keys", "-t", "cfg:1.0", "C-c"],
                ["send-keys", "-t", "cfg:1.0", "C-c"],
                ["send-keys", "-t", "cfg:1.0", "C-c"],
            ],
            [call.args[0] for call in tmux.call_args_list],
        )
        self.assertEqual(3, sleep.call_count)

    def test_send_exit_keys_stops_after_bounded_ctrl_c_attempts(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
            patch("omo_manager.omo_codex_stop.current_command", return_value="bunx"),
            patch("omo_manager.omo_codex_stop.time.sleep"),
        ):
            send_exit_keys("cfg:1.0")
        self.assertEqual(
            [
                ["send-keys", "-t", "cfg:1.0", "C-c"],
                ["send-keys", "-t", "cfg:1.0", "C-c"],
                ["send-keys", "-t", "cfg:1.0", "C-c"],
                ["send-keys", "-t", "cfg:1.0", "C-c"],
            ],
            [call.args[0] for call in tmux.call_args_list],
        )

    def test_close_tmux_target_kills_single_pane_window_after_shell(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.window_panes", return_value=1),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
        ):
            close_tmux_target("cfg:1.0")
        self.assertEqual(["kill-window", "-t", "cfg:1.0"], tmux.call_args.args[0])

    def test_close_tmux_target_kills_only_pane_in_multi_pane_window(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.current_command", return_value="zsh"),
            patch("omo_manager.omo_codex_stop.window_panes", return_value=2),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
        ):
            close_tmux_target("cfg:1.0")
        self.assertEqual(["kill-pane", "-t", "cfg:1.0"], tmux.call_args.args[0])

    def test_close_tmux_target_keeps_running_codex_pane(self) -> None:
        with (
            patch("omo_manager.omo_codex_stop.current_command", return_value="bunx"),
            patch("omo_manager.omo_codex_stop.tmux") as tmux,
        ):
            close_tmux_target("cfg:1.0")
        tmux.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
