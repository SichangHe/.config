import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task import Args, codex_cmd, ensure_task_file, link_todo, main, new_window, parse_args, start_codex, wait_command_started


class OmoTaskTests(unittest.TestCase):
    def test_creates_task_file_with_runat_header_and_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, 'x.md', 'cfg', '2', 'codex', None, '', None, False, False, '', '', ())
            path = ensure_task_file(args, 'cfg:2')
            link_todo(args, 'cfg:2')
            self.assertEqual('runat: cfg:2 codex', path.read_text(encoding='utf-8').splitlines()[0])
            self.assertIn('x.md cfg:2', (root / 'TODO.md').read_text(encoding='utf-8'))

    def test_new_window_uses_tmux_new_window_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False, '', '', ())
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_shell') as wait_shell, patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                tmux.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            command = tmux.call_args.args[0]
            self.assertEqual(['new-window', '-P'], command[:2])
            self.assertNotIn('bunx @openai/codex --dangerously-bypass-approvals-and-sandbox', command)
            wait_shell.assert_called_once_with('cfg:7')
            start_codex_mock.assert_called_once_with('cfg:7', args)

    def test_codex_cmd_resumes_quoted_session(self) -> None:
        self.assertEqual("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume abc", codex_cmd("abc"))
        self.assertEqual("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume 'abc def'", codex_cmd("abc def"))

    def test_codex_cmd_uses_prompt_argument_from_file(self) -> None:
        self.assertEqual(
            'bunx @openai/codex --dangerously-bypass-approvals-and-sandbox "$(cat -- /tmp/prompt.md)"',
            codex_cmd(prompt_file=Path("/tmp/prompt.md")),
        )

    def test_codex_cmd_adds_reasoning_effort_and_extra_flags(self) -> None:
        self.assertEqual(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
            codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review")),
        )

    def test_parse_args_accepts_repeatable_codex_flags(self) -> None:
        args = parse_args(["--task-file", "x.md", "--reasoning-effort", "xhigh", "--codex-flag=--profile", "--codex-flag", "deep-review"])
        self.assertEqual("xhigh", args.reasoning_effort)
        self.assertEqual(("--profile", "deep-review"), args.codex_flags)

    def test_new_window_can_resume_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False, '11111111-1111-1111-1111-111111111111', '', ())
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_shell'), patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                tmux.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            start_codex_mock.assert_called_once_with('cfg:7', args)

    def test_start_codex_sends_command_inside_existing_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', prompt, False, False, '11111111-1111-1111-1111-111111111111', '', ())
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_command_started') as wait_command_started_mock:
                start_codex('cfg:7', args)
            command = tmux.call_args_list[0].args[0]
            self.assertEqual(['send-keys', '-t', 'cfg:7'], command[:3])
            self.assertIn('bash -lc', command[3])
            self.assertIn('resume 11111111-1111-1111-1111-111111111111', command[3])
            self.assertIn('$(cat --', command[3])
            self.assertEqual('Enter', command[4])
            wait_command_started_mock.assert_called_once_with('cfg:7')

    def test_wait_command_started_accepts_visible_codex_status(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=['› Use /skills to list available skills', '  gpt-5.5']), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.sleep') as sleep:
            wait_command_started('cfg:7')
            sleep.assert_not_called()

    def test_wait_command_started_fails_when_shell_remains_active(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=[]), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.monotonic', side_effect=[0, 6]), patch('omo_manager.omo_task.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Codex launch not verified'):
                wait_command_started('cfg:7')

    def test_main_dry_run_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("hello", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--prompt-file", str(prompt), "--dry-run"]))
            self.assertIn("tmux new-window", out.getvalue())
            self.assertIn("tmux send-keys", out.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_validates_prompt_file_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--prompt-file", str(root / "missing.md"), "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_multiline_codex_flag_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--codex-flag", "bad\nflag", "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_rejects_non_codex_tool(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md", "--tool", "other"])


if __name__ == '__main__':
    _ = unittest.main()
