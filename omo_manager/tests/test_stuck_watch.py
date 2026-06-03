import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_status import Report
from omo_manager.omo_stuck_watch import Args, check


class StuckWatchTests(unittest.TestCase):
    def test_check_reports_status_and_tail_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('running', ['working'])), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80)))
                self.assertEqual(0, check(Args(registry, state, 80)))
            self.assertIn('cfg:1', state.read_text(encoding='utf-8'))


if __name__ == '__main__':
    _ = unittest.main()
