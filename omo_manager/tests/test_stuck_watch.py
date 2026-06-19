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
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1)))
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1)))
            self.assertIn('cfg:1', state.read_text(encoding='utf-8'))

    def test_running_unchanged_tail_becomes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('running', ['working'])), patch('omo_manager.omo_stuck_watch.time.time', side_effect=[100.0, 131.0]), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 30.0, False, 60.0, 1)))
                self.assertEqual(0, check(Args(registry, state, 80, 30.0, False, 60.0, 1)))
            self.assertIn('stale_running: task=x.md target=cfg:1 changed=false same_tail_s=31', out.getvalue())

    def test_check_includes_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('ready', ['done'])), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1, 'mgr:1.0')))
            self.assertIn('ready: task=manager target=mgr:1.0', out.getvalue())

    def test_check_auto_unsticks_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=report), patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present', return_value='sent_enter') as unstick, contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1)))
            unstick.assert_called_once_with('cfg:1', report)
            self.assertIn('stuck_input: task=x.md target=cfg:1 changed=true same_tail_s=0 unstick=sent_enter', out.getvalue())

    def test_check_unsticks_duplicate_stuck_input_target_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"},{"task_file":"y.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=report), patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present', return_value='sent_enter') as unstick, contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1)))
            unstick.assert_called_once_with('cfg:1', report)
            self.assertEqual(1, out.getvalue().count('unstick=sent_enter'))
            self.assertEqual(1, out.getvalue().count('unstick=already_sent'))

    def test_check_unsticks_alias_stuck_input_target_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"},{"task_file":"y.md","tmux_target":"cfg:1.0"}]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=report), patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present', return_value='sent_enter') as unstick, contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1)))
            unstick.assert_called_once_with('cfg:1', report)
            self.assertEqual(1, out.getvalue().count('unstick=sent_enter'))
            self.assertEqual(1, out.getvalue().count('unstick=already_sent'))

    def test_check_does_not_duplicate_manager_target_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('ready', ['done'])), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1, 'cfg:1.0')))
            self.assertNotIn('task=manager', out.getvalue())

    def test_check_reports_precise_disabled_unstick_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=report), patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present') as unstick, contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(registry, state, 80, 900.0, False, 60.0, 1, '', False)))
            unstick.assert_not_called()
            self.assertIn('unstick=disabled:no_auto_unstick', out.getvalue())


if __name__ == '__main__':
    _ = unittest.main()
