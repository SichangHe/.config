import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch
from omo_manager.omo_codex_start import Args, Pane, StartError, launch_command, query_exact_status_session_id, record_session_id
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata


class CodexSessionCaptureTests(unittest.TestCase):
    UUID = "019f670b-6a2f-7463-b9be-9aa6ff0cec43"

    def test_frontmatter_session_id_round_trip(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\nbody\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text(text)
            record_session_id(path, self.UUID)
            self.assertEqual(parse_task_metadata(path.read_text()).session_id, self.UUID)

    def test_rejects_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: codex\nmanagerat: w:2\nis_manager: false\npending_task_items: []\n---\n")
            path.write_text(path.read_text() + "drift")
            with self.assertRaises(StartError):
                record_session_id(path, self.UUID, "0" * 64)

    def test_non_codex_session_id_rejected(self):
        text = "---\nversion: v1.0.0\nstatus: running\nrunat: w:1\ntool: pcodx\nmanagerat: w:2\nis_manager: false\npending_task_items: []\nsession_id: %s\n---\n" % self.UUID
        with self.assertRaises(TaskFrontmatterError):
            parse_task_metadata(text)

    def test_fresh_launch_command_withholds_prompt(self):
        args = Args(Path("/tmp"), "task.md", "w:1", "gpt-5", "high", "", Path("/tmp/prompt"), 1, False, True)
        pane = Pane("w:1", "%1", "@1", "zsh", Path("/tmp"), 42)
        command = launch_command(args, pane, None, "marker")
        self.assertNotIn("prompt", command)

    def test_prompt_sender_not_called_when_capture_fails(self):
        with patch("omo_manager.omo_codex_start.query_status_session_id", side_effect=RuntimeError("identity")) as query, patch("omo_manager.omo_codex_start.send_prompt") as prompt:
            with self.assertRaises(RuntimeError):
                query("w:1", 10, 1, tmux_guard=("w:1", "%1"))
            prompt.assert_not_called()

    def test_status_submission_is_one_exact_process_guarded_sequence(self):
        pane = Pane("w:1.0", "%1", "@1", "bun", Path("/tmp"), 42)
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")

        def fake_run(argv):
            if argv[:3] == ["tmux", "if-shell", "-F"]:
                token = next(part for part in argv[6].split(" ; ") if part.startswith("display-message -p ")).split()[-1]
                return __import__("subprocess").CompletedProcess(argv, 0, token + "\n", "")
            return completed

        tails = iter(((True, ["before"]), (True, ["after"])))
        with patch("omo_manager.omo_codex_start.run", side_effect=fake_run) as run_mock, patch("omo_manager.omo_codex_start.exact_tail", side_effect=lambda *_: next(tails)), patch("omo_manager.omo_codex_start.extract_new_status_session_id", return_value=self.UUID), patch("omo_manager.omo_codex_start.verify_same_process"):
            self.assertEqual(self.UUID, query_exact_status_session_id(pane, 80, 1))
        if_shell = next(call.args[0] for call in run_mock.call_args_list if call.args[0][:3] == ["tmux", "if-shell", "-F"])
        self.assertIn("#{pane_pid},42", if_shell[5])
        self.assertIn("#{pane_current_command},bun", if_shell[5])


if __name__ == "__main__":
    unittest.main()
