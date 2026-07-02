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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
            self.assertIn('cfg:1', state.read_text(encoding='utf-8'))

    def test_running_unchanged_tail_becomes_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[{"task_file":"x.md","tmux_target":"cfg:1"}]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('running', ['working'])), patch('omo_manager.omo_stuck_watch.time.time', side_effect=[100.0, 131.0]), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(root, registry, state, 80, 30.0, False, 60.0, 1)))
                self.assertEqual(0, check(Args(root, registry, state, 80, 30.0, False, 60.0, 1)))
            self.assertIn('stale_running: task=x.md target=cfg:1 changed=false same_tail_s=31', out.getvalue())

    def test_check_includes_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            with patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('ready', ['done'])), contextlib.redirect_stdout(out):
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1, 'mgr:1.0')))
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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1, 'cfg:1.0')))
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
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1, '', False)))
            unstick.assert_not_called()
            self.assertIn('unstick=disabled:no_auto_unstick', out.getvalue())

    def test_check_scans_unregistered_vl_tmux_panes_when_vl_work_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = (root / 'TODO.md').write_text('current:\nvl_example.md vl:53\n', encoding='utf-8')
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            report = Report(
                'stuck_input',
                ['› VeruLaw language update landed in commit 1dcbc7c.'],
                'VeruLaw language update landed in commit 1dcbc7c.',
                True,
            )
            with (
                patch('omo_manager.omo_stuck_watch.subprocess.run') as run,
                patch('omo_manager.omo_stuck_watch.inspect', return_value=report),
                patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present') as unstick,
                contextlib.redirect_stdout(out),
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = 'vl:53.0\nwl:2.0\n'
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
            unstick.assert_not_called()
            self.assertIn('stuck_input: task=tmux:vl:53.0 target=vl:53.0', out.getvalue())
            self.assertIn('unstick=disabled:unregistered_tmux', out.getvalue())

    def test_check_scans_unregistered_vl_tmux_panes_for_loose_vl_todo_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = (root / 'TODO.md').write_text('current:\nactive.md vl 15\n', encoding='utf-8')
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› pasted prompt'], 'pasted prompt', True)
            with (
                patch('omo_manager.omo_stuck_watch.subprocess.run') as run,
                patch('omo_manager.omo_stuck_watch.inspect', return_value=report),
                patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present') as unstick,
                contextlib.redirect_stdout(out),
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = 'vl:15.0\n'
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
            unstick.assert_not_called()
            self.assertIn('stuck_input: task=tmux:vl:15.0 target=vl:15.0', out.getvalue())

    def test_check_ignores_historical_vl_todo_entries_for_tmux_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = (root / 'TODO.md').write_text('current:\nactive.md wl:1\nprevious:\nold_vl.md vl 15\n', encoding='utf-8')
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            with (
                patch('omo_manager.omo_stuck_watch.subprocess.run') as run,
                patch('omo_manager.omo_stuck_watch.inspect', return_value=Report('stuck_input', ['› pasted prompt'], 'pasted prompt', True)),
                contextlib.redirect_stdout(out),
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = 'vl:15.0\n'
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1)))
            run.assert_not_called()
            self.assertEqual('', out.getvalue())

    def test_check_keeps_explicit_vl_manager_target_out_of_unregistered_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / 'sessions.json'
            state = root / 'state.json'
            _ = (root / 'TODO.md').write_text('current:\nactive.md vl 15\n', encoding='utf-8')
            _ = registry.write_text('{"sessions":[]}', encoding='utf-8')
            out = io.StringIO()
            report = Report('stuck_input', ['› manager prompt'], 'manager prompt', True)
            with (
                patch('omo_manager.omo_stuck_watch.subprocess.run') as run,
                patch('omo_manager.omo_stuck_watch.inspect', return_value=report),
                patch('omo_manager.omo_stuck_watch.submit_stuck_input_if_present', return_value='sent_enter') as unstick,
                contextlib.redirect_stdout(out),
            ):
                run.return_value.returncode = 0
                run.return_value.stdout = 'vl:15.0\n'
                self.assertEqual(0, check(Args(root, registry, state, 80, 900.0, False, 60.0, 1, 'vl:15')))
            unstick.assert_called_once_with('vl:15', report)
            self.assertIn('stuck_input: task=manager target=vl:15', out.getvalue())
            self.assertNotIn('task=tmux:vl:15.0', out.getvalue())


if __name__ == '__main__':
    _ = unittest.main()
