import subprocess
import unittest
from unittest.mock import patch

from omo_manager.omo_codex_status import Args, Report, can_submit_stuck_input, current_block, current_input_text, has_compacting_indicator, inspect, last_output, status, submit_stuck_input_if_present


class CodexStatusTests(unittest.TestCase):
    def test_extracts_last_output_from_current_block(self) -> None:
        lines = ['old', '────', ' kept  ', '', '─ Worked for 1m 2s ─', '  gpt-5.5']
        self.assertEqual([' kept'], last_output(lines))

    def test_status_requires_codex_marker_in_last_line(self) -> None:
        self.assertEqual('not_codex', status(['shell'], current_block(['shell'])))

    def test_status_ready_from_current_worked_footer(self) -> None:
        lines = ['────', 'done', '─ Worked for 1s ─', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_ready_from_idle_input_footer(self) -> None:
        lines = ['────', 'done', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_running_when_message_is_queued(self) -> None:
        lines = ['• Messages to be submitted after next tool call (press esc to interrupt and send immediately)', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))

    def test_status_running_while_waiting_for_background_terminal_with_review_placeholder(self) -> None:
        lines = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_running_while_working_with_explain_placeholder(self) -> None:
        lines = ['• Working (4m 34s • esc to interrupt)', '', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_running_while_working_with_implement_placeholder(self) -> None:
        lines = ['• Working (4m 34s • esc to interrupt)', '', '› Implement {feature}', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_running_with_queued_input_footer_without_model_footer(self) -> None:
        lines = [
            '• Working (19m 47s • esc to interrupt)',
            '',
            '› [Pasted Content 1020 chars] as hypothesis-generating, not reassuring proof.',
            '  - Add population and marker tables.',
            '',
            '',
            '  tab to queue message                                                                                    28% context left',
        ]
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_running_with_long_queued_input_footer_without_model_footer(self) -> None:
        lines = [
            '• Working (19m 47s • esc to interrupt)',
            '',
            '› [Pasted Content 3000 chars] first queued line',
            *[f'  queued line {idx}' for idx in range(25)],
            '  tab to queue message                                                                                    28% context left',
        ]
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_stuck_input_while_compacting_but_not_safe_to_submit_immediately(self) -> None:
        lines = ['• Compacting conversation', '', '› Continue task', '  gpt-5.5']
        self.assertEqual('Continue task', current_input_text(lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))
        self.assertTrue(has_compacting_indicator(lines))

    def test_compacting_indicator_requires_active_codex_compacting_status(self) -> None:
        self.assertFalse(has_compacting_indicator(['• Compacting conversation', '› Continue task']))
        self.assertFalse(has_compacting_indicator(['────', '• Compacting conversation', '────', 'done', '› Continue task', '  gpt-5.5']))
        self.assertFalse(has_compacting_indicator(['────', 'done', '• compact this report later', '› Continue task', '  gpt-5.5']))

    def test_status_not_codex_for_queued_footer_without_working_indicator(self) -> None:
        lines = ['shell output', '  tab to queue message                                                                                    28% context left']
        self.assertEqual('not_codex', status(lines, current_block(lines)))

    def test_status_not_codex_when_queued_footer_is_not_final(self) -> None:
        lines = ['• Working (19m 47s • esc to interrupt)', '  tab to queue message                                                                                    28% context left', '$ shell prompt']
        self.assertEqual('not_codex', status(lines, current_block(lines)))

    def test_status_stuck_input_for_user_entered_explain_on_idle_worker(self) -> None:
        lines = ['────', 'done', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertTrue(can_submit_stuck_input(lines))

    def test_status_stuck_input_for_user_entered_implement_on_idle_worker(self) -> None:
        lines = ['────', 'done', '› Implement {feature}', '  gpt-5.5']
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertTrue(can_submit_stuck_input(lines))

    def test_status_ready_with_finished_background_terminal_and_input(self) -> None:
        lines = ['• Waited for background terminal · timeout 900s verifier', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_stuck_input_for_non_placeholder_input_box(self) -> None:
        lines = ['────', 'done', '› Continue `opc_pcodx_live_context_compaction_5481.md`.', '  gpt-5.5']
        self.assertEqual('Continue `opc_pcodx_live_context_compaction_5481.md`.', current_input_text(lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertTrue(can_submit_stuck_input(lines))

    def test_status_stuck_input_for_user_entered_review_command_with_details(self) -> None:
        lines = ['────', 'done', '› Run /review on my current changes and summarize findings', '  gpt-5.5']
        self.assertEqual('Run /review on my current changes and summarize findings', current_input_text(lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertTrue(can_submit_stuck_input(lines))

    def test_status_ready_for_known_placeholder_suggestion(self) -> None:
        lines = ['────', 'done', '› Summarize recent commits', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_ready_for_write_tests_placeholder_suggestion(self) -> None:
        lines = ['• Working (1m 59s • esc to interrupt)', '', '› Write tests for @filename', '', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_stuck_input_for_multiline_input_box(self) -> None:
        lines = ['────', 'done', '› Run `~/.config/getagentsmd` first.', '  Continue `x.md`.', '  - Report back.', '  gpt-5.5']
        self.assertEqual('Run `~/.config/getagentsmd` first.\n  Continue `x.md`.\n  - Report back.', current_input_text(lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))

    def test_status_running_without_worked_footer(self) -> None:
        lines = ['working', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))

    def test_old_worked_footer_does_not_make_new_block_ready(self) -> None:
        lines = ['────', 'old', '─ Worked for 1s ─', '────', 'new work', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))
        self.assertEqual(['new work'], last_output(lines))

    def test_status_error_from_output(self) -> None:
        lines = ['────', 'Traceback', '  gpt-5.5']
        self.assertEqual('error', status(lines, current_block(lines)))

    def test_inspect_reads_tmux_tail(self) -> None:
        with patch('omo_manager.omo_codex_status.tail', return_value=['────', 'done', '─ Worked for 1s ─', '  gpt-5.5']):
            report = inspect(Args('cfg:1', 20))
        self.assertEqual('ready', report.status)
        self.assertEqual(['done'], report.lines)

    def test_submit_stuck_input_if_present_sends_enter_for_stuck_input(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['› Continue task', '  gpt-5.5']), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', 'cfg:1.0', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_ignores_latest_placeholder(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['› Summarize recent commits', '  gpt-5.5']), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_stuck', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()

    def test_submit_stuck_input_if_present_ignores_latest_review_placeholder(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['› Run /review on my current changes', '  gpt-5.5']), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_stuck', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()

    def test_submit_stuck_input_if_present_sends_enter_while_latest_screen_is_busy(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['• Working', '', '› Continue task', '  gpt-5.5']), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', 'cfg:1.0', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_waits_for_compaction_then_sends_enter(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        captures = 0
        line_counts: list[int] = []
        tails = iter([
            ['• Compacting conversation', '', '› Continue task', '  gpt-5.5'],
            ['› Continue task', '  gpt-5.5'],
        ])

        def fake_tail(_: str, n_lines: int) -> list[str]:
            nonlocal captures
            captures += 1
            line_counts.append(n_lines)
            return next(tails)

        with patch('omo_manager.omo_codex_status.tail', side_effect=fake_tail), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.sleep') as sleep:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report, compaction_wait_timeout_s=10))
        self.assertEqual(2, captures)
        self.assertEqual([2000, 2000], line_counts)
        sleep.assert_called_once()
        run.assert_called_once_with(['tmux', 'send-keys', '-t', 'cfg:1.0', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_ignores_non_stuck_report(self) -> None:
        report = Report('ready', ['› Use /skills to list available skills'], 'Use /skills to list available skills', False)
        with patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()


if __name__ == '__main__':
    _ = unittest.main()
