import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_stop import (
    Args,
    close_note,
    close_tmux_target,
    extract_exit_resume_id,
    extract_new_status_session_id,
    extract_resume_id,
    extract_status_session_id,
    feedback_prompt,
    main,
    maybe_request_feedback,
    post_interrupt_output,
    query_status_session_id,
    record_close,
    resume_cmd,
    send_exit_keys,
    stop,
)


class CodexStopTests(unittest.TestCase):
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
