import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task import Args, ensure_task_file, link_todo, new_window


class OmoTaskTests(unittest.TestCase):
    def test_creates_task_file_with_runat_header_and_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, 'x.md', 'cfg', '2', 'codex', None, '', None, False, False)
            path = ensure_task_file(args, 'cfg:2')
            link_todo(args, 'cfg:2')
            self.assertEqual('runat: cfg:2 codex', path.read_text(encoding='utf-8').splitlines()[0])
            self.assertIn('x.md cfg:2', (root / 'TODO.md').read_text(encoding='utf-8'))

    def test_new_window_dry_run_returns_sentinel_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual('cfg:DRYRUN', new_window(args))

    def test_new_window_uses_tmux_new_window_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False)
            with patch('omo_manager.omo_task.subprocess.run') as run:
                run.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            command = run.call_args.args[0]
            self.assertEqual(['tmux', 'new-window', '-P'], command[:3])
            self.assertIn('bunx @openai/codex --dangerously-bypass-approvals-and-sandbox', command)


if __name__ == '__main__':
    _ = unittest.main()
