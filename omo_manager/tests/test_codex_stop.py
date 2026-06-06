import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_stop import Args, extract_resume_id, main, record_close, stop


class CodexStopTests(unittest.TestCase):
    def test_extract_resume_id_from_resume_command(self) -> None:
        text = "To resume, run codex resume 11111111-2222-3333-4444-555555555555\n"
        self.assertEqual("11111111-2222-3333-4444-555555555555", extract_resume_id(text))

    def test_extract_resume_id_from_resume_line(self) -> None:
        text = "Resume this session with 99999999-aaaa-bbbb-cccc-dddddddddddd when ready.\n"
        self.assertEqual("99999999-aaaa-bbbb-cccc-dddddddddddd", extract_resume_id(text))

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
        self.assertIn("resume: `codex resume 11111111-2222-3333-4444-555555555555`", text)

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
        self.assertIn("tmux target `cfg:1.0`", text)

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

    def test_stop_dry_run_refuses_missing_target_before_printing(self) -> None:
        with patch("omo_manager.omo_codex_stop.pane_id", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "tmux target not found"):
                stop(Args("cfg:9.0", 0.0, 10, True, False))


if __name__ == "__main__":
    _ = unittest.main()
