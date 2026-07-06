import subprocess
import unittest
from unittest.mock import patch

from omo_manager.omo_codex_status import Args, Report, can_submit_stuck_input, current_block, current_input_text, final_assistant_output, has_compacting_indicator, has_terminal_enter_prompt_after_codex_footer, inspect, last_output, report_from_lines, status, submit_stuck_input_if_present, visible_error_lines


class CodexStatusTests(unittest.TestCase):
    def test_extracts_last_output_from_current_block(self) -> None:
        lines = ['old', '────', ' kept  ', '', '─ Worked for 1m 2s ─', '  gpt-5.5']
        self.assertEqual([' kept'], last_output(lines))

    def test_extracts_final_assistant_output_before_placeholder_prompt(self) -> None:
        lines = [
            '────',
            '• Implemented and privately reported.',
            '',
            '─ Worked for 12s ───────────────────────────────────────────────────────────────────────────────────────────────────────',
            '',
            '',
            '› Write tests for @filename',
            '',
            '  gpt-5.5 medium · /home/sichangheagent/.config',
        ]
        report = report_from_lines(lines)
        self.assertEqual(['• Implemented and privately reported.'], final_assistant_output(lines))
        self.assertEqual('ready', report.status)
        self.assertEqual('Write tests for @filename', report.input_text)
        self.assertEqual(['• Implemented and privately reported.'], report.lines)

    def test_report_keeps_live_block_when_running_after_completed_turn(self) -> None:
        lines = [
            '────',
            '• Implemented and privately reported.',
            '',
            '─ Worked for 12s ───────────────────────────────────────────────────────────────────────────────────────────────────────',
            '',
            '• Working (2m 13s • esc to interrupt)',
            '',
            '› Write tests for @filename',
            '',
            '  gpt-5.5 medium · /home/sichangheagent/.config',
        ]
        report = report_from_lines(lines)
        self.assertEqual('running', report.status)
        self.assertIn('• Working (2m 13s • esc to interrupt)', report.lines)
        self.assertIn('› Write tests for @filename', report.lines)

    def test_report_keeps_pending_input_evidence_for_stuck_input(self) -> None:
        lines = [
            '────',
            '• Implemented and privately reported.',
            '',
            '─ Worked for 12s ───────────────────────────────────────────────────────────────────────────────────────────────────────',
            '',
            '',
            '› Continue with the next private step',
            '',
            '  gpt-5.5 medium · /home/sichangheagent/.config',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('Continue with the next private step', report.input_text)
        self.assertIn('› Continue with the next private step', report.lines)

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

    def test_status_stuck_input_with_idle_queued_pasted_content_footer(self) -> None:
        lines = [
            '› [Pasted Content 1024 chars][Pasted Content 1024 chars] #2[Pasted Content 1024 chars] #3',
            '  tab to queue message                                                                                    26% context left',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('[Pasted Content 1024 chars][Pasted Content 1024 chars] #2[Pasted Content 1024 chars] #3', report.input_text)
        self.assertTrue(report.can_submit_input)

    def test_status_stuck_input_for_plan_prompt_without_model_footer(self) -> None:
        lines = [
            '› [Pasted Content 1024 chars][Pasted Content 1024 chars]',
            '  manager-doc-or-task-change: repo=/ssd1/sichangheagent/work_logs status=M',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertIn('[Pasted Content 1024 chars]', report.input_text)
        self.assertTrue(report.can_submit_input)
        self.assertEqual('', report.input_blocker)

    def test_status_plan_prompt_without_input_is_not_submit_safe(self) -> None:
        lines = [
            '› ',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('', report.input_text)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('empty_input', report.input_blocker)

    def test_status_plan_prompt_with_placeholder_is_not_submit_safe(self) -> None:
        lines = [
            '› Summarize recent commits',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('Summarize recent commits', report.input_text)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('placeholder_input', report.input_blocker)

    def test_status_plan_prompt_with_model_footer_without_input_is_not_ready(self) -> None:
        lines = [
            '› ',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
            '  gpt-5.5',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('', report.input_text)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('empty_input', report.input_blocker)

    def test_status_plan_prompt_with_model_footer_placeholder_is_not_ready(self) -> None:
        lines = [
            '› Summarize recent commits',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
            '  gpt-5.5',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('Summarize recent commits', report.input_text)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('placeholder_input', report.input_blocker)

    def test_status_ready_with_idle_queued_placeholder_footer(self) -> None:
        lines = [
            '› Summarize recent commits',
            '  tab to queue message                                                                                    26% context left',
        ]
        report = report_from_lines(lines)
        self.assertEqual('ready', report.status)
        self.assertEqual('Summarize recent commits', report.input_text)
        self.assertEqual([], report.lines)
        self.assertFalse(report.can_submit_input)

    def test_status_stuck_input_while_compacting_but_not_safe_to_submit_immediately(self) -> None:
        lines = ['• Compacting conversation', '', '› Continue task', '  gpt-5.5']
        self.assertEqual('Continue task', current_input_text(lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))
        self.assertTrue(has_compacting_indicator(lines))
        self.assertEqual('compacting', report_from_lines(lines).input_blocker)

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

    def test_status_stuck_input_for_terminal_enter_prompt_after_codex_footer(self) -> None:
        lines = ['────', 'done', '─ Worked for 1s ─', '  gpt-5.5', 'Press Enter to continue...']
        report = report_from_lines(lines)
        self.assertTrue(has_terminal_enter_prompt_after_codex_footer(lines))
        self.assertEqual('stuck_input', report.status)
        self.assertTrue(report.can_submit_input)

    def test_status_not_codex_for_terminal_enter_prompt_not_after_codex_footer(self) -> None:
        lines = ['shell command output', 'Press Enter to continue...']
        report = report_from_lines(lines)
        self.assertFalse(has_terminal_enter_prompt_after_codex_footer(lines))
        self.assertEqual('not_codex', report.status)
        self.assertFalse(report.can_submit_input)

    def test_status_not_codex_when_terminal_enter_prompt_follows_text_mentioning_model(self) -> None:
        lines = ['tool output mentions  gpt-5.5', 'Press Enter to continue...']
        report = report_from_lines(lines)
        self.assertFalse(has_terminal_enter_prompt_after_codex_footer(lines))
        self.assertEqual('not_codex', report.status)
        self.assertFalse(report.can_submit_input)

    def test_status_not_codex_when_terminal_enter_prompt_follows_busy_footer_without_completed_turn(self) -> None:
        lines = ['• Working (19m 47s • esc to interrupt)', '  gpt-5.5', 'Press Enter to continue...']
        report = report_from_lines(lines)
        self.assertFalse(has_terminal_enter_prompt_after_codex_footer(lines))
        self.assertEqual('not_codex', report.status)
        self.assertFalse(report.can_submit_input)

    def test_status_ready_for_idle_explain_placeholder(self) -> None:
        lines = ['────', 'done', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

    def test_status_ready_for_idle_implement_placeholder(self) -> None:
        lines = ['────', 'done', '› Implement {feature}', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))
        self.assertFalse(can_submit_stuck_input(lines))

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

    def test_status_stuck_input_for_verulaw_language_update_mail_snapshot(self) -> None:
        lines = [
            '• Acknowledged manager_mail/8952.txt by email with subject Using GPT for stricter VL experiments.',
            '',
            '  I recorded the instruction privately: OpenRouter should not block valuable VL experiments when GPT is available, and',
            '  future experiment packets need the corrected verifier-backed/proof-gap standard instead of answer-only outline',
            '  scoring.',
            '',
            '─ Worked for 1m 11s ────────────────────────────────────────────────────────────────────────────────────────────────────',
            '',
            '',
            '› VeruLaw language update landed in commit 1dcbc7c. For human-facing communication, reports, prompts, and handoffs,',
            '  read /ssd1/sichangheagent/VeruLaw/docs/ubiquitous-language.md first and use its canonical naming; root AGENTS.md now',
            '  records this.',
            '',
            '',
            '  gpt-5.5 medium · /ssd1/sichangheagent/work_logs · 1.03M used · Context 35% used',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertIn('VeruLaw language update landed in commit 1dcbc7c.', report.input_text)

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

    def test_status_error_from_api_error_text(self) -> None:
        for message in ['OpenAI API error: rate limit', 'OpenAI API error: 429 Too Many Requests', 'Error: 429 Too Many Requests']:
            with self.subTest(message=message):
                lines = ['────', message, '  gpt-5.5']
                self.assertEqual('error', report_from_lines(lines).status)

    def test_status_error_from_selected_model_capacity_warning(self) -> None:
        for warning in ['⚠ Selected model is at capacity. Please try a different model.', '⚠️ Selected model is at capacity. Please try a different model.', 'Selected model is at capacity. Please try a different model.']:
            with self.subTest(warning=warning):
                lines = ['────', warning, '› Use /skills to list available skills', '  gpt-5.5']
                self.assertEqual('error', report_from_lines(lines).status)

    def test_visible_error_lines_include_warning_and_square_marker_above_input(self) -> None:
        lines = ['────', '■ Error: 429 Too Many Requests', 'detail', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual(['■ Error: 429 Too Many Requests'], visible_error_lines(current_block(lines).lines))
        warning = ['────', '⚠ Selected model is at capacity. Please try a different model.', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual(['⚠ Selected model is at capacity. Please try a different model.'], visible_error_lines(current_block(warning).lines))

    def test_ready_input_stays_ready_after_benign_error_text(self) -> None:
        lines = ['────', 'No error found', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', report_from_lines(lines).status)

    def test_non_error_marker_above_input_stays_ready(self) -> None:
        lines = ['────', '■ Build summary complete', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', report_from_lines(lines).status)

    def test_status_stuck_input_when_capacity_warning_has_real_input(self) -> None:
        lines = ['────', '⚠ Selected model is at capacity. Please try a different model.', '', '› Continue the private manager task', '  gpt-5.5']
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertEqual('Continue the private manager task', report.input_text)

    def test_status_ready_for_placeholder_after_completed_turn(self) -> None:
        lines = [
            '• Working (2m 13s • esc to interrupt)',
            '',
            '• Done.',
            '',
            '─ Worked for 5m 17s ─────────────────',
            '',
            '',
            '› Implement {feature}',
            '',
            '  gpt-5.5 medium · /ssd1/sichangheagent/VeruLaw',
        ]
        report = report_from_lines(lines)
        self.assertEqual('ready', report.status)
        self.assertEqual('Implement {feature}', report.input_text)

    def test_status_running_when_running_indicator_is_newer_than_completed_turn(self) -> None:
        lines = [
            '• Done.',
            '',
            '─ Worked for 5m 17s ─────────────────',
            '',
            '• Working (2m 13s • esc to interrupt)',
            '',
            '› Implement {feature}',
            '',
            '  gpt-5.5 medium · /ssd1/sichangheagent/VeruLaw',
        ]
        report = report_from_lines(lines)
        self.assertEqual('running', report.status)
        self.assertEqual('Implement {feature}', report.input_text)

    def test_status_running_when_newer_running_indicator_is_far_above_input(self) -> None:
        lines = [
            '• Done.',
            '',
            '─ Worked for 5m 17s ─────────────────',
            '',
            '• Working (2m 13s • esc to interrupt)',
            *[f'log line {idx}' for idx in range(30)],
            '',
            '› Implement {feature}',
            '',
            '  gpt-5.5 medium · /ssd1/sichangheagent/VeruLaw',
        ]
        report = report_from_lines(lines)
        self.assertEqual('running', report.status)
        self.assertEqual('Implement {feature}', report.input_text)

    def test_status_stuck_input_when_capacity_warning_text_is_typed(self) -> None:
        lines = ['────', 'done', '› Selected model is at capacity. Please try a different model.', '  gpt-5.5']
        self.assertEqual('stuck_input', report_from_lines(lines).status)

    def test_status_stuck_input_when_capacity_warning_text_is_multiline_input(self) -> None:
        lines = ['────', 'done', '› Note this exact text:', '  Selected model is at capacity. Please try a different model.', '  gpt-5.5']
        self.assertEqual('stuck_input', report_from_lines(lines).status)

    def test_status_not_codex_for_capacity_warning_without_codex_footer(self) -> None:
        lines = ['Selected model is at capacity. Please try a different model.']
        self.assertEqual('not_codex', report_from_lines(lines).status)

    def test_status_output_error_count_does_not_make_pane_error(self) -> None:
        lines = ['────', 'agent-status: not_codex=0 running=1 error=0 ready=0 stuck_input=0 done-registry-stale=0 pruned=0', 'running: task=active.md evidence=target=cfg:1.0 output=working', '  gpt-5.5']
        self.assertEqual('running', report_from_lines(lines).status)

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

    def test_submit_stuck_input_if_present_sends_enter_for_terminal_enter_prompt(self) -> None:
        report = Report('stuck_input', ['Press Enter to continue...'], '', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['────', 'done', '─ Worked for 1s ─', '  gpt-5.5', 'Press Enter to continue...']), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
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

    def test_submit_stuck_input_if_present_reports_latest_unsafe_input(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        latest = Report('stuck_input', ['• Compacting conversation', '', '› Continue task'], 'Continue task', False, 'compacting')
        with patch('omo_manager.omo_codex_status.wait_while_compacting', return_value=latest), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_safe:compacting', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()

    def test_submit_stuck_input_if_present_submits_plan_prompt(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        lines = [
            '› [Pasted Content 1024 chars][Pasted Content 1024 chars]',
            '',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
        ]
        with patch('omo_manager.omo_codex_status.tail', return_value=lines), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', 'cfg:1.0', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_reports_compaction_timeout_as_unsafe(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.wait_while_compacting', side_effect=TimeoutError), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_safe:compacting', submit_stuck_input_if_present('cfg:1.0', report))
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
