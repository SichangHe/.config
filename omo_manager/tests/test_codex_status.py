import subprocess
import unittest
from unittest.mock import patch

from omo_manager.omo_codex_status import Args, PlanPromptRecovery, Report, can_submit_stuck_input, current_block, current_input_text, dismiss_plan_prompt_if_present, dismiss_skills_menu_if_present, exact_pane_id, final_assistant_output, has_active_skills_menu, has_compacting_indicator, has_cursor_followups_overlay, has_resume_paused_goal_prompt, has_terminal_enter_prompt_after_codex_footer, has_waiting_subagent_prompt, inspect, interrupt_waiting_subagent_if_present, last_output, report_from_lines, status, submit_stuck_input_if_present, tail, tail_pane_id, visible_error_lines
from omo_manager.omo_tmux_send import error_signature, exact_capacity_error


def cursor_agent_status_lines(prompt: str = 'Add a follow-up', *, running: bool = False) -> list[str]:
    follow = f'  → {prompt}'
    if running:
        follow = f'{follow}                                                                                            ctrl+c to stop'
    lines = [
        'previous output',
        ' ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄',
        follow,
        ' ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀',
    ]
    if running:
        lines.append('  1 task')
    lines.extend(
        [
            '  Cursor Grok 4.6 Extra High · 77.7% · 9 files edited                                                          Run Everything',
            '  /ssd1/sichangheagent/work_logs · main',
        ]
    )
    return lines


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

    def test_inspect_distinguishes_missing_target_from_non_codex_process(self) -> None:
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(False, [])):
            report = inspect(Args('cfg:404', 80))
        self.assertEqual('missing', report.status)

    def test_inspect_reports_missing_when_target_disappears_after_capture(self) -> None:
        with patch('omo_manager.omo_codex_status.exact_pane_id', side_effect=['%1', '']), patch(
            'omo_manager.omo_codex_status.tail_pane_id', return_value=['────', 'done', '  gpt-5.6-terra']
        ):
            report = inspect(Args('cfg:404', 80))
        self.assertEqual('missing', report.status)

    def test_inspect_keeps_active_bunx_codex_running_when_queue_overlay_hides_footer(self) -> None:
        lines = [
            '• Queued follow-up inputs',
            '  ↳ Capacity advisory: use another model.',
            '› Explain this codebase',
            '  esc again to edit previous message',
        ]
        process = subprocess.CompletedProcess(
            ['tmux'], 0, '%7\tbunx\t"export OMO_AGENT_TMUX_TARGET=vlcliimprove:0.0 && exec bunx @openai/codex --model gpt-5.6-sol"\n', ''
        )
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, lines)), patch(
            'omo_manager.omo_codex_status.exact_pane_id', return_value='%7'
        ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
            report = inspect(Args('vlcliimprove:0', 80))
        self.assertEqual('running', report.status)
        self.assertFalse(report.can_submit_input)

    def test_inspect_keeps_active_cursor_agent_running_without_codex_footer(self) -> None:
        lines = ['Cursor Agent is working']
        cases = (
            '%7\tagent\t"export OMO_AGENT_TMUX_TARGET=cur:1 && exec agent --workspace /tmp --model gpt-5-medium"\n',
            '%7\tagent\t\n',
        )
        for tmux_output in cases:
            with self.subTest(tmux_output=tmux_output):
                process = subprocess.CompletedProcess(['tmux'], 0, tmux_output, '')
                with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, lines)), patch(
                    'omo_manager.omo_codex_status.exact_pane_id', return_value='%7'
                ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
                    report = inspect(Args('cur:1', 80))
                self.assertEqual('running', report.status)
                self.assertFalse(report.can_submit_input)

    def test_current_input_text_reads_cursor_follow_up_composer(self) -> None:
        self.assertEqual('Add a follow-up', current_input_text(cursor_agent_status_lines()))
        self.assertEqual('Add a follow-up', current_input_text(cursor_agent_status_lines(running=True)))
        self.assertEqual('Handle pending watcher delivery', current_input_text(cursor_agent_status_lines('Handle pending watcher delivery')))
        self.assertEqual('', current_input_text(['  → Add a follow-up', 'shell prompt']))

    def test_has_cursor_followups_overlay_requires_header_and_send_now(self) -> None:
        overlay = [
            'previous output',
            ' ┌─ follow-ups ────────────────────────────────────────────┐',
            ' │ ○ [Pasted text #8 +59 lines]',
            ' │ enter send now · ↑ select/edit · esc cancel',
            ' └─────────────────────────────────────────────────────────┘',
            *cursor_agent_status_lines(running=True)[1:],
        ]
        self.assertTrue(has_cursor_followups_overlay(overlay))
        self.assertFalse(has_cursor_followups_overlay(cursor_agent_status_lines(running=True)))
        self.assertFalse(has_cursor_followups_overlay(['┌─ follow-ups', 'enter send now', 'shell']))

    def test_inspect_live_cursor_agent_reads_follow_up_placeholder(self) -> None:
        lines = cursor_agent_status_lines()
        process = subprocess.CompletedProcess(['tmux'], 0, '%9\tagent\t"exec agent --force"\n', '')
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, lines)), patch(
            'omo_manager.omo_codex_status.exact_pane_id', return_value='%9'
        ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
            report = inspect(Args('wl:1', 80))
        self.assertEqual('ready', report.status)
        self.assertEqual('Add a follow-up', report.input_text)
        self.assertFalse(report.can_submit_input)

    def test_inspect_running_cursor_agent_keeps_running_with_follow_up_placeholder(self) -> None:
        lines = cursor_agent_status_lines(running=True)
        process = subprocess.CompletedProcess(['tmux'], 0, '%9\tagent\t"exec agent --force"\n', '')
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, lines)), patch(
            'omo_manager.omo_codex_status.exact_pane_id', return_value='%9'
        ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
            report = inspect(Args('wl:1', 80))
        self.assertEqual('running', report.status)
        self.assertEqual('Add a follow-up', report.input_text)

    def test_inspect_cursor_tui_without_agent_process_stays_not_codex(self) -> None:
        process = subprocess.CompletedProcess(['tmux'], 0, '%9\tzsh\tzsh\n', '')
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, cursor_agent_status_lines())), patch(
            'omo_manager.omo_codex_status.exact_pane_id', return_value='%9'
        ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
            report = inspect(Args('wl:1', 80))
        self.assertEqual('not_codex', report.status)

    def test_status_classifies_cursor_follow_up_composer_like_codex(self) -> None:
        self.assertEqual('ready', report_from_lines(cursor_agent_status_lines()).status)
        self.assertEqual('running', report_from_lines(cursor_agent_status_lines(running=True)).status)
        stuck = report_from_lines(cursor_agent_status_lines('Handle pending watcher delivery'))
        self.assertEqual('stuck_input', stuck.status)
        self.assertEqual('Handle pending watcher delivery', stuck.input_text)
        self.assertTrue(stuck.can_submit_input)
        overlay = [
            'previous output',
            ' ┌─ follow-ups ────────────────────────────────────────────┐',
            ' │ ○ [Pasted text #8 +59 lines]',
            ' │ enter send now · ↑ select/edit · esc cancel',
            ' └─────────────────────────────────────────────────────────┘',
            *cursor_agent_status_lines(running=True)[1:],
        ]
        self.assertEqual('stuck_input', report_from_lines(overlay).status)
        history = [' ┌─ follow-ups', 'enter send now', '3 tasks', *(['old output'] * 50), *cursor_agent_status_lines()]
        self.assertEqual('ready', report_from_lines(history).status)
        self.assertEqual('ready', report_from_lines(['3 tasks', *cursor_agent_status_lines()]).status)
        quoted = ['quoted:   Cursor Grok 4.6 Low · 51.3%', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', report_from_lines(quoted).status)
        self.assertNotEqual((), error_signature(['■ Error: 429', *quoted]))

    def test_status_classifies_cursor_usage_limit_as_error(self) -> None:
        lines = cursor_agent_status_lines()
        footer = lines.pop()
        workdir = lines.pop()
        lines.extend(
            [
                '  124 files edited',
                workdir,
                '',
                '  Error: Increase limits for faster responses',
                "  You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
                '',
                footer,
            ]
        )

        report = report_from_lines(lines)

        self.assertEqual('error', report.status)
        self.assertEqual(
            [
                'Error: Increase limits for faster responses',
                "You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
            ],
            visible_error_lines(lines),
        )

    def test_status_ignores_stale_cursor_usage_limit_before_current_screen(self) -> None:
        lines = [
            '  Error: Increase limits for faster responses',
            "  You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
            '',
            *cursor_agent_status_lines(),
        ]

        report = report_from_lines(lines)

        self.assertEqual('ready', report.status)
        self.assertEqual([], visible_error_lines(lines))

    def test_unmarked_cursor_usage_words_do_not_broaden_codex_errors(self) -> None:
        lines = [
            'Error: Increase limits for faster responses',
            "You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
            '› Use /skills to list available skills',
            '  gpt-5.5',
        ]

        self.assertEqual('ready', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(lines, include_unmarked=False))

    def test_stale_cursor_usage_scrollback_does_not_error_current_codex_pane(self) -> None:
        lines = [
            *cursor_agent_status_lines(),
            '  Error: Increase limits for faster responses',
            "  You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
            '────',
            '› Use /skills to list available skills',
            '  gpt-5.5',
        ]

        self.assertEqual('ready', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(lines, include_unmarked=False))

    def test_cursor_transcript_failures_are_not_codex_errors(self) -> None:
        history = cursor_agent_status_lines()
        history[0] = 'omo_task_edit.py: error: --owner-task-file is invalid.'
        pasted = cursor_agent_status_lines('Fix the pending watcher error handling')
        pasted[0] = 'omo_task_edit.py: error: --owner-task-file is invalid.'
        self.assertEqual((), error_signature(history))
        self.assertEqual(error_signature(history), error_signature(pasted))
        self.assertEqual((), error_signature(cursor_agent_status_lines('Fix the pending watcher error handling')))

    def test_inspect_does_not_broaden_shell_editor_or_similar_launcher(self) -> None:
        cases = (
            ('zsh', 'zsh'),
            ('vim', 'vim README.md'),
            ('bunx', 'bunx @example/codex'),
            ('bunx', 'bunx @openai/codex-lookalike'),
            ('node', 'node /tmp/agent.js'),
        )
        for current_command, start_command in cases:
            with self.subTest(current_command=current_command, start_command=start_command):
                process = subprocess.CompletedProcess(['tmux'], 0, f'%7\t{current_command}\t{start_command}\n', '')
                with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, ['$ shell prompt'])), patch(
                    'omo_manager.omo_codex_status.exact_pane_id', return_value='%7'
                ), patch('omo_manager.omo_codex_status.subprocess.run', return_value=process):
                    report = inspect(Args('cfg:1', 80))
                self.assertEqual('not_codex', report.status)

    def test_paused_goal_resume_snapshot_is_submit_safe_stuck_input(self) -> None:
        lines = [
            '• The vlexp:6 delivery actually succeeded despite the stale verification error: the manager is live Codex and responded to the',
            '  exact alert. Both workers’ blockers are real. I’m checking the named replacement-review chain and preservation coordinator so',
            '  the blockers are not merely parked without an owner.',
            '',
            '⚠ Selected model is at capacity. Please try a different model.',
            '',
            '',
            '› <agent_message>',
            '  Capacity advisory: models currently capacity-limited: gpt-5.6-sol. Prioritize work using other models for now.',
            '  </agent_message>',
            '',
            '',
            '■ Conversation interrupted - tell the model what to do differently. Something went wrong? Hit `/feedback` to report the issue.',
            '',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`. Consider switching back to `gpt-5.6-',
            '  sol` as it may affect Codex performance.',
            '',
            '',
            '  Resume paused goal?',
            '  Goal: Complete the two open `/shagent` migration items through the staged local-only inventory, independent reviews, revers',
            '',
            '› 1. Resume goal   Mark it active and continue when idle',
            '  2. Leave paused  Keep it paused; use /goal resume later',
            '',
            '  Press enter to confirm or esc to go back',
        ]

        report = report_from_lines(lines)

        self.assertTrue(has_resume_paused_goal_prompt(lines))
        self.assertEqual('stuck_input', report.status)
        self.assertTrue(report.can_submit_input)
        self.assertEqual('', report.input_blocker)

    def test_paused_goal_resume_chooser_allows_modest_copy_variation(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session started with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra` after 47 seconds.',
            'Resume the paused goal?',
            'Goal: Finish the bounded task.',
            '1. Resume goal and continue automatically',
            '› 2. Leave paused until /goal resume is used',
            'Press Return to confirm or Escape to go back',
        ]

        report = report_from_lines(lines)

        self.assertEqual('stuck_input', report.status)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('resume_goal_not_selected', report.input_blocker)

    def test_paused_goal_menu_in_completed_output_does_not_override_ready_state(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal',
            '2. Leave paused',
            'Press enter to confirm or esc to go back',
            '─ Worked for 1s ─',
            '› Use /skills to list available skills',
            '  gpt-5.6-terra',
        ]

        report = report_from_lines(lines)

        self.assertFalse(has_resume_paused_goal_prompt(lines))
        self.assertEqual('ready', report.status)
        self.assertFalse(report.can_submit_input)

    def test_incomplete_paused_goal_menu_is_not_submit_safe(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal',
            'Press enter to confirm or esc to go back',
        ]

        report = report_from_lines(lines)

        self.assertFalse(has_resume_paused_goal_prompt(lines))
        self.assertEqual('not_codex', report.status)
        self.assertFalse(report.can_submit_input)

    def test_paused_goal_menu_requires_capacity_resume_warning_and_goal_context(self) -> None:
        menu = [
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal',
            '2. Leave paused',
            'Press enter to confirm or esc to go back',
        ]
        cases = (
            menu,
            ['⚠ Selected model is at capacity. Please try a different model.', *menu],
            ['⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.', *menu],
            [
                '⚠ Selected model is at capacity. Please try a different model.',
                '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
                *[line for line in menu if not line.startswith('Goal:')],
            ],
        )

        for lines in cases:
            with self.subTest(lines=lines):
                self.assertFalse(has_resume_paused_goal_prompt(lines))
                self.assertFalse(report_from_lines(lines).can_submit_input)

    def test_paused_goal_menu_with_conflicting_selection_is_not_submit_safe(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal',
            '› 2. Leave paused',
            'Press enter to confirm or esc to go back',
        ]

        report = report_from_lines(lines)

        self.assertEqual('stuck_input', report.status)
        self.assertFalse(report.can_submit_input)
        self.assertEqual('resume_goal_not_selected', report.input_blocker)

    def test_exact_file_search_overlay_is_recoverable_stuck_input(self) -> None:
        lines = [
            '› Previous worker is running; output=› Find and fix a bug in @filename',
            '',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes                 [All Results] Filesystem Only Plugins',
        ]

        report = report_from_lines(lines)

        self.assertEqual('stuck_input', report.status)
        self.assertTrue(report.can_submit_input)
        self.assertEqual('Previous worker is running; output=› Find and fix a bug in @filename', report.input_text)

    def test_wrapped_file_search_overlay_is_also_recoverable(self) -> None:
        lines = [
            '› Manager notice includes @filename',
            'no matches',
            'enter insert · esc close · ←/→ switch search modes',
            '[All Results] Filesystem Only Plugins',
        ]

        self.assertEqual('stuck_input', report_from_lines(lines).status)

    def test_file_search_overlay_near_misses_remain_not_codex(self) -> None:
        cases = (
            [
                '• Working (8s • esc to interrupt)',
                '› ordinary terminal text without a file token',
                'no matches',
                'enter insert · esc close · ←/→ switch search modes',
                '[All Results] Filesystem Only Plugins',
            ],
            [
                'shell printed @filename',
                'no matches',
                'enter insert · esc close · ←/→ switch search modes',
                '[All Results] Filesystem Only Plugins',
            ],
            [
                '• Working (8s • esc to interrupt)',
                '› terminal mentioned @filename',
                'no matches',
                'enter insert · esc close',
                '[All Results] Filesystem Only Plugins',
            ],
        )
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertEqual('not_codex', report_from_lines(lines).status)

    def test_exact_file_search_overlay_needs_no_other_codex_evidence(self) -> None:
        lines = [
            '› terminal merely printed @filename',
            'no matches',
            'enter insert · esc close · ←/→ switch search modes',
            '[All Results] Filesystem Only Plugins',
        ]

        self.assertEqual('stuck_input', report_from_lines(lines).status)

    def test_status_ready_from_current_worked_footer(self) -> None:
        lines = ['────', 'done', '─ Worked for 1s ─', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_ready_from_idle_input_footer(self) -> None:
        lines = ['────', 'done', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_running_when_message_is_queued(self) -> None:
        lines = ['• Messages to be submitted after next tool call (press esc to interrupt and send immediately)', '› Use /skills to list available skills', '  gpt-5.5']
        self.assertEqual('running', status(lines, current_block(lines)))

    def test_status_waiting_subagent_when_exact_queued_wait_pattern_is_visible(self) -> None:
        lines = [
            '• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9',
            '• Working (21s • esc to interrupt)',
            '• Messages to be submitted after next tool call (press esc to interrupt and send immediately)',
            '',
            '› Implement {feature}',
            '',
            '  gpt-5.5 xhigh · ~/.config · 71.7M used',
        ]
        self.assertTrue(has_waiting_subagent_prompt(lines))
        self.assertEqual('running', report_from_lines(lines).status)
        self.assertEqual('waiting_subagent', report_from_lines(lines, detect_waiting_subagent=True).status)

    def test_status_waiting_subagent_requires_pending_message_line(self) -> None:
        lines = [
            '• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9',
            '• Working (21s • esc to interrupt)',
            '',
            '› Implement {feature}',
            '',
            '  gpt-5.5 xhigh · ~/.config · 71.7M used',
        ]
        self.assertFalse(has_waiting_subagent_prompt(lines))
        self.assertEqual('running', report_from_lines(lines).status)

    def test_status_waiting_subagent_ignores_stale_scrollback_pattern(self) -> None:
        lines = [
            '• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9',
            '• Working (21s • esc to interrupt)',
            '• Messages to be submitted after next tool call (press esc to interrupt and send immediately)',
            '',
            '› Implement {feature}',
            '',
            '─ Worked for 1s ─',
            '',
            '› Use /skills to list available skills',
            '',
            '  gpt-5.5 xhigh · ~/.config · 71.7M used',
        ]
        self.assertFalse(has_waiting_subagent_prompt(lines))
        self.assertEqual('ready', report_from_lines(lines).status)

    def test_status_waiting_subagent_ignores_completed_wait_turn(self) -> None:
        lines = [
            '• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9',
            '• Working (21s • esc to interrupt)',
            '• Messages to be submitted after next tool call (press esc to interrupt and send immediately)',
            '',
            '─ Worked for 1s ─',
            '  gpt-5.5 xhigh · ~/.config · 71.7M used',
        ]
        self.assertFalse(has_waiting_subagent_prompt(lines))
        self.assertEqual('ready', report_from_lines(lines).status)

    def test_interrupt_waiting_subagent_sends_escape_after_recheck(self) -> None:
        lines = [
            '• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9',
            '• Working (21s • esc to interrupt)',
            '• Messages to be submitted after next tool call (press esc to interrupt and send immediately)',
            '› Implement {feature}',
            '  gpt-5.5 xhigh · ~/.config · 71.7M used',
        ]
        report = report_from_lines(lines, detect_waiting_subagent=True)
        with patch('omo_manager.omo_codex_status.tail', return_value=lines), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_escape', interrupt_waiting_subagent_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Escape'], capture_output=True, text=True, timeout=5, check=False)

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

    def test_status_stuck_input_ignores_working_indicator_from_completed_turn(self) -> None:
        lines = [
            '• Working (19m 47s • esc to interrupt)',
            '• Finished the previous request.',
            '─ Worked for 19m 48s ─',
            '',
            '› Continue with the queued request',
            '  tab to queue message                                                                                    26% context left',
        ]
        report = report_from_lines(lines)
        self.assertEqual('stuck_input', report.status)
        self.assertTrue(report.can_submit_input)

    def test_status_running_with_current_background_terminal_and_queued_footer(self) -> None:
        lines = [
            '• Finished the previous request.',
            '─ Worked for 1m 2s ─',
            '• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close',
            '',
            '› Continue after the terminal finishes',
            '  tab to queue message                                                                                    26% context left',
        ]
        report = report_from_lines(lines)
        self.assertEqual('running', report.status)
        self.assertFalse(report.can_submit_input)

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

    def test_dismiss_plan_prompt_sends_one_escape_with_before_after_evidence(self) -> None:
        modal = ['› Continue task', '', '  Create a plan?  shift + tab use Plan mode   esc dismiss']
        after = ['› Continue task', '  gpt-5.5']
        report = report_from_lines(modal)
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.tail_pane_id', side_effect=[modal, after]), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            recovery = dismiss_plan_prompt_if_present('cfg:1.0', report)
        self.assertEqual(PlanPromptRecovery('sent_escape', 'plan_prompt', 'stuck_input'), recovery)
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Escape'], capture_output=True, text=True, timeout=5, check=False)

    def test_dismiss_plan_prompt_fails_closed_for_human_target(self) -> None:
        modal = ['› Continue task', '', '  Create a plan?  shift + tab use Plan mode   esc dismiss']
        with patch('omo_manager.omo_codex_status.exact_pane_id') as resolve, patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_plan_prompt_if_present('human:1.0', report_from_lines(modal))
        self.assertEqual(PlanPromptRecovery('not_safe:human_target', 'plan_prompt', 'not_checked'), recovery)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_dismiss_plan_prompt_fails_closed_for_stale_evidence(self) -> None:
        modal = ['› Continue task', '', '  Create a plan?  shift + tab use Plan mode   esc dismiss']
        latest = ['› Continue task', '  gpt-5.5']
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.tail_pane_id', return_value=latest), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_plan_prompt_if_present('cfg:1.0', report_from_lines(modal))
        self.assertEqual(PlanPromptRecovery('not_safe:stale_evidence', 'plan_prompt', 'stuck_input'), recovery)
        run.assert_not_called()

    def test_dismiss_plan_prompt_fails_closed_for_other_modal(self) -> None:
        other = ['Resume the paused goal?', '› 1. Resume goal', '  2. Leave paused', 'Press enter to confirm or esc to go back']
        with patch('omo_manager.omo_codex_status.exact_pane_id') as resolve, patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_plan_prompt_if_present('cfg:1.0', Report('stuck_input', other))
        self.assertEqual(PlanPromptRecovery('not_safe:not_plan_prompt', 'stuck_input', 'not_checked'), recovery)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_dismiss_plan_prompt_fails_closed_for_historical_signature(self) -> None:
        historical = [
            '• The screen said:',
            '  Create a plan?  shift + tab use Plan mode   esc dismiss',
            '› Continue task',
            '  gpt-5.5',
        ]
        report = report_from_lines(historical)
        self.assertEqual('stuck_input', report.status)
        with patch('omo_manager.omo_codex_status.exact_pane_id') as resolve, patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_plan_prompt_if_present('cfg:1.0', report)
        self.assertEqual(PlanPromptRecovery('not_safe:not_plan_prompt', 'stuck_input', 'not_checked'), recovery)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_dismiss_plan_prompt_fails_closed_for_ambiguous_pane(self) -> None:
        modal = ['› Continue task', '', '  Create a plan?  shift + tab use Plan mode   esc dismiss']
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value=''), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_plan_prompt_if_present('cfg:1.0', report_from_lines(modal))
        self.assertEqual(PlanPromptRecovery('not_safe:ambiguous_pane', 'plan_prompt', 'not_checked'), recovery)
        run.assert_not_called()

    def test_dismiss_skills_menu_sends_one_escape_with_before_after_evidence(self) -> None:
        menu = ['Skills', 'Choose an action', '› 1. List skills            Tip: press @ to open this list directly.', '  2. Enable/Disable Skills  Enable or disable skills.', 'Press enter to confirm or esc to go back']
        after = ['› Continue task', '  gpt-5.6-terra']
        self.assertTrue(has_active_skills_menu(menu))
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.pane_has_exact_codex_process', return_value=True), patch('omo_manager.omo_codex_status.tail_pane_id', side_effect=[menu, after]), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            recovery = dismiss_skills_menu_if_present('cfg:1.0', report_from_lines(menu))
        self.assertEqual(PlanPromptRecovery('sent_escape', 'skills_menu', 'stuck_input'), recovery)
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Escape'], capture_output=True, text=True, timeout=5, check=False)

    def test_dismiss_skills_menu_fails_closed_when_menu_is_historical(self) -> None:
        historical = ['Skills', 'Choose an action', '› 1. List skills', '  2. Enable/Disable Skills', 'Press enter to confirm or esc to go back', '› Continue task', '  gpt-5.6-terra']
        self.assertFalse(has_active_skills_menu(historical))
        with patch('omo_manager.omo_codex_status.exact_pane_id') as resolve, patch('omo_manager.omo_codex_status.subprocess.run') as run:
            recovery = dismiss_skills_menu_if_present('cfg:1.0', report_from_lines(historical))
        self.assertEqual(PlanPromptRecovery('not_safe:not_skills_menu', 'stuck_input', 'not_checked'), recovery)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_dismiss_skills_menu_fails_closed_for_stale_or_human_target(self) -> None:
        menu = ['Skills', 'Choose an action', '› 1. List skills', '  2. Enable/Disable Skills', 'Press enter to confirm or esc to go back']
        latest = ['› Continue task', '  gpt-5.6-terra']
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.pane_has_exact_codex_process', return_value=True), patch('omo_manager.omo_codex_status.tail_pane_id', return_value=latest), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            stale = dismiss_skills_menu_if_present('cfg:1.0', report_from_lines(menu))
        self.assertEqual(PlanPromptRecovery('not_safe:stale_evidence', 'skills_menu', 'stuck_input'), stale)
        run.assert_not_called()
        with patch('omo_manager.omo_codex_status.exact_pane_id') as resolve:
            human = dismiss_skills_menu_if_present('hcfg:1.0', report_from_lines(menu))
        self.assertEqual(PlanPromptRecovery('not_safe:human_target', 'skills_menu', 'not_checked'), human)
        resolve.assert_not_called()

    def test_dismiss_skills_menu_requires_codex_process_and_stable_target(self) -> None:
        menu = ['Skills', 'Choose an action', '› 1. List skills', '  2. Enable/Disable Skills', 'Press enter to confirm or esc to go back']
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.pane_has_exact_codex_process', return_value=False), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            not_codex = dismiss_skills_menu_if_present('cfg:1.0', report_from_lines(menu))
        self.assertEqual(PlanPromptRecovery('not_safe:not_codex_process', 'skills_menu', 'not_checked'), not_codex)
        run.assert_not_called()
        with patch('omo_manager.omo_codex_status.exact_pane_id', side_effect=['%7', '%8']), patch('omo_manager.omo_codex_status.pane_has_exact_codex_process', return_value=True), patch('omo_manager.omo_codex_status.tail_pane_id', return_value=menu), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            rebound = dismiss_skills_menu_if_present('cfg:1.0', report_from_lines(menu))
        self.assertEqual(PlanPromptRecovery('not_safe:target_rebound', 'skills_menu', 'not_checked'), rebound)
        run.assert_not_called()

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

    def test_unsupported_chatgpt_model_is_exact_capacity_error(self) -> None:
        for model in ('gpt-5.6-sol', 'gpt-5.7_codex.preview'):
            with self.subTest(model=model):
                error = f'''■ {{"detail":"The '{model}' model is not supported when using Codex with a ChatGPT account."}}'''
                lines = ['────', error, '› Use /skills to list available skills', '  gpt-5.5']
                self.assertEqual('error', report_from_lines(lines).status)
                self.assertEqual([error], visible_error_lines(current_block(lines).lines))
                self.assertTrue(exact_capacity_error(lines))

    def test_other_unsupported_model_errors_are_not_capacity_errors(self) -> None:
        errors = (
            '''■ {"detail":"The 'gpt-5.6-sol' model is not supported when using the API."}''',
            '''■ {"detail":"The 'gpt 5.6 sol' model is not supported when using Codex with a ChatGPT account."}''',
            '''■ {"detail":"The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.","code":"unsupported"}''',
        )
        for error in errors:
            with self.subTest(error=error):
                lines = ['────', error, '› Use /skills to list available skills', '  gpt-5.5']
                self.assertFalse(exact_capacity_error(lines))

    def test_status_error_from_wake_execution_budget_refusal(self) -> None:
        for refusal in [
            "• I can’t safely complete another wake prompt in the remaining execution budget.",
            "I can't safely complete another wake prompt in the remaining execution budget.",
            "I cannot safely handle this wake prompt within the remaining execution time available.",
            "I am unable to safely execute the wake prompt in the remaining execution budget.",
            "I can't safely complete a wake prompt within the remaining execution budget",
            "I cannot safely handle wake prompt in the remaining execution time!",
        ]:
            with self.subTest(refusal=refusal):
                lines = ['────', refusal, '› Use /skills to list available skills', '  gpt-5.6-terra low']
                self.assertEqual('error', report_from_lines(lines).status)
                self.assertEqual([refusal], visible_error_lines(current_block(lines).lines))

    def test_wake_execution_budget_text_in_input_is_not_an_error(self) -> None:
        lines = [
            '────',
            '• Done.',
            '─ Worked for 1s ─────────────────',
            '› Quote this text: I can’t safely complete another wake prompt in the remaining execution budget.',
            '  gpt-5.6-terra low',
        ]
        self.assertEqual('stuck_input', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(current_block(lines).lines))

    def test_status_error_from_capacity_warning_after_older_manager_history(self) -> None:
        lines = [
            '› Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:',
            '',
            '  1 blocked agents are ready; if they are not actually blocked, correct their status, otherwise make sure whatever is blocking them is being resolved:',
            '  hvl_cli_push_11754.md hvl:9 <blocked_on>waiting for separate explicit human authorization of preserved-thread segment 9 before any continuation</blocked_on>',
            '',
            '• hvl:9 remains correctly blocked solely on explicit segment 9 authorization. No new authorization exists, so no state change or duplicate email was needed.',
            '',
            '› Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:',
            '',
            '  1 blocked agents are ready; if they are not actually blocked, correct their status, otherwise make sure whatever is blocking them is being resolved:',
            '  hvl_cli_push_11754.md hvl:9 <blocked_on>waiting for separate explicit human authorization of preserved-thread segment 9 before any continuation</blocked_on>',
            '',
            '• Verified unchanged: hvl:9 is genuinely blocked on explicit segment 9 authorization. No duplicate email or state change was needed.',
            '',
            '› Normally record pending items and remove the consumed `(pending)` marker by running:',
            '  `omo_record_pending.py --pending-file hvl_product_pitch_12489.md --line 223 --item PENDING_ITEM_TEXT [--item ...] [--task-file TARGET_TASK.md] --ack-human`',
            '  Choose `--item` values by quoting the human\'s words as much as possible. Use `--ack-human` so the script emails the human after recording. If no new pending task item should be added, use',
            '',
            '• I’m reopening the completed pitch manager in the human-facing hvl session, recording the listenable overview and four-example portfolio as new owner items, and preserving example discovery as',
            '  delegated worker work.',
            '',
            "• Ran tmux list-windows -t hvl -F '#{window_index} #{window_name} active=#{window_active}' | sort -n | tail -n 30",
            "  │ sed -n '1,12p' hvl_product_pitch_12489.md",
            '  └ 0 nvim active=1',
            '    1 vl_doc_review_9758 active=0',
            '    … +19 lines (ctrl + t to view transcript)',
            '    Run `~/.config/getagentsmd` first and follow `MANAGER.md` plus repository-local instructions. Stay at manager level: delegate asset inventory, investor narrative/visual design, technical Q&A, and',
            '    adversarial claim review. Preserve experiment evidence and unrelated work.',
            '',
            '• Added /tmp/hvl_product_pitch_resume_12489.prompt.md (+7 -0)',
            '    1 +Resume the completed pitch-manager context for the human\'s new follow-up.',
            '    2 +',
            '    3 +Start by emailing the human a listenable overview of how the pitch works and which primary example was selected, including what it demonstrates and why it was chosen. Write for listening, not sca',
            '       nning; explain the story and evidence boundaries in plain language.',
            '    4 +',
            '    5 +Then manage a four-example portfolio with varied complexity. Preserve the reviewed Byteorder primary and TOML backup unless fresh evidence warrants a clearly reviewed change. Delegate independent',
            '        workers to identify at least two additional evidence-backed examples at different complexity levels. Prepare the primary example deeply; prepare lighter but usable backup packets for the other e',
            '       xamples. Require exact evidence, claim boundaries, visual potential, audience fit, and honest limitations. Do not launch new experiments without separate authorization; first use existing reviewe',
            '       d evidence.',
            '    6 +',
            '    7 +Stay at manager level, delegate example discovery, and report milestones privately. External presentation, distribution, outreach, transcript/TOML display, and experiment gates remain open.',
            '',
            '⚠ Selected model is at capacity. Please try a different model.',
            '',
            '› Explain this codebase',
            '',
            '  gpt-5.6-sol medium · /ssd1/sichangheagent/work_logs · 37.1M used · Context 86% used · Main [default]',
        ]
        self.assertEqual('error', report_from_lines(lines).status)
        warning = '⚠ Selected model is at capacity. Please try a different model.'
        self.assertEqual([warning], visible_error_lines(current_block(lines).lines))
        self.assertEqual((warning,), error_signature(lines))
        self.assertTrue(exact_capacity_error(lines))

    def test_status_ready_when_capacity_warning_is_from_older_completed_turn(self) -> None:
        lines = [
            '────',
            '› Retry the manager task',
            '⚠ Selected model is at capacity. Please try a different model.',
            '› Continue with the fallback model',
            '• Completed the manager task.',
            '─ Worked for 12s ─────────────────',
            '› Explain this codebase',
            '  gpt-5.5',
        ]
        self.assertEqual('ready', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertFalse(exact_capacity_error(lines))

    def test_status_ready_when_truncated_tail_has_capacity_warning_before_worked_boundary(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '─ Worked for 12s ─────────────────',
            '› Explain this codebase',
            '  gpt-5.5',
        ]
        self.assertEqual('ready', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertFalse(exact_capacity_error(lines))

    def test_status_error_when_capacity_warning_follows_worked_boundary(self) -> None:
        lines = [
            '─ Worked for 12s ─────────────────',
            '⚠ Selected model is at capacity. Please try a different model.',
            '› Explain this codebase',
            '  gpt-5.5',
        ]
        warning = '⚠ Selected model is at capacity. Please try a different model.'
        self.assertEqual('error', report_from_lines(lines).status)
        self.assertEqual([warning], visible_error_lines(current_block(lines).lines))
        self.assertTrue(exact_capacity_error(lines))

    def test_visible_error_lines_include_warning_and_square_marker_above_input(self) -> None:
        lines = ['────', '■ Error: 429 Too Many Requests', 'detail', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual(['■ Error: 429 Too Many Requests'], visible_error_lines(current_block(lines).lines))
        warning = ['────', '⚠ Selected model is at capacity. Please try a different model.', '› Explain this codebase', '  gpt-5.5']
        self.assertEqual(['⚠ Selected model is at capacity. Please try a different model.'], visible_error_lines(current_block(warning).lines))

    def test_status_error_when_codex_hides_current_content(self) -> None:
        for notice in ("ⓘ This content can't be shown", "ⓘ This content can’t be shown.", "ⓘ This content can't be shown?"):
            with self.subTest(notice=notice):
                lines = [notice, '', '› Summarize recent commits', '  gpt-5.6-sol']
                self.assertEqual('error', status(lines, current_block(lines)))
                self.assertEqual([notice], visible_error_lines(current_block(lines).lines))

    def test_content_hidden_error_overrides_file_search_overlay(self) -> None:
        lines = [
            "ⓘ This content can't be shown",
            '› Continue with @filename',
            '',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes                 [All Results] Filesystem Only Plugins',
        ]
        self.assertEqual('error', status(lines, current_block(lines)))

    def test_content_hidden_text_in_current_input_is_not_an_error(self) -> None:
        lines = ["› Explain why ⓘ This content can't be shown appeared", '  gpt-5.6-sol']
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertEqual('stuck_input', status(lines, current_block(lines)))

    def test_content_hidden_text_without_codex_ui_is_not_codex(self) -> None:
        lines = ["ⓘ This content can't be shown"]
        self.assertEqual('not_codex', status(lines, current_block(lines)))

    def test_other_information_notice_is_not_an_error(self) -> None:
        lines = ['ⓘ This content is available in the transcript.', '', '› Summarize recent commits', '  gpt-5.6-sol']
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertEqual('ready', status(lines, current_block(lines)))

    def test_status_keeps_abbreviated_codex_apps_failures_visible(self) -> None:
        for error in (
            '■ codex_apps startup failed: HTTP 401; no available accounts',
            '■ codex_apps startup failed: 401; no available accounts',
            '■ codex_apps startup failed: HTTP 401',
            '■ codex_apps startup failed: HTTP 500; no available accounts',
            '■ another_connector startup failed: HTTP 401; no available accounts',
            '■ codex_apps startup failed: no available accounts; HTTP 401; slack startup failed HTTP 500',
            '■ codex_apps startup failed: HTTP 401; no available accounts; slack startup failed HTTP 401',
            '■ codex_apps request failed: HTTP 401; no available accounts',
            '■ codex_apps startup failed: HTTP 401; no available accounts; error: cache corrupt',
            '■ codex_apps startup failed: HTTP 401; no available accounts; slack failed',
        ):
            with self.subTest(error=error):
                lines = ['────', error, '› Use /skills to list available skills', '  gpt-5.5']
                self.assertEqual('error', report_from_lines(lines).status)
                self.assertEqual([error], visible_error_lines(current_block(lines).lines))

    def test_status_ignores_only_complete_codex_apps_transport_startup_warning(self) -> None:
        failure = '⚠ MCP client for `codex_apps` failed to start: MCP startup failed: handshaking with MCP server failed: Send message error Transport'
        unexpected = '  [rmcp::transport::worker::WorkerTransport<rmcp::transport::streamable_http_client::StreamableHttpClientWorker'
        unexpected_continued = '  <codex_rmcp_client::http_client_adapter::StreamableHttpClientAdapter>>] error: unexpected'
        response = '  server response: HTTP 401: {"error":{"message":"No available accounts","type":"proxy_error","code":401}}, when send initialize request'
        incomplete = '⚠ MCP startup incomplete (failed: codex_apps)'
        harmless = ['────', failure, unexpected, unexpected_continued, response, '', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra']
        self.assertEqual('ready', report_from_lines(harmless).status)
        self.assertEqual([], visible_error_lines(current_block(harmless).lines))

        for lines in (
            ['────', failure, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, unexpected, unexpected_continued, response.replace('HTTP 401', 'HTTP 500'), incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, unexpected, unexpected_continued, response, '■ Error: cache corrupt', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, unexpected, unexpected_continued, response, 'error: cache corrupt', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, unexpected, unexpected_continued, response, 'database error: cache corrupt', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, unexpected, unexpected_continued, 'slack failed', response, '', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', f'{failure}; slack failed', unexpected, unexpected_continued, response, '', incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
            ['────', failure, '⚠ MCP client for `slack` failed to start: HTTP 401', response, incomplete, '› Use /skills to list available skills', '  gpt-5.6-terra'],
        ):
            with self.subTest(lines=lines):
                self.assertEqual('error', report_from_lines(lines).status)
                self.assertNotEqual([], visible_error_lines(current_block(lines).lines))

        wrapped_boundaries = [
            '────',
            '⚠ MCP client for `codex_apps` failed to',
            '  start: MCP startup failed: handshaking with MCP server failed: Send message error Transport',
            unexpected,
            unexpected_continued,
            response,
            '⚠ MCP startup incomplete (failed:',
            '  codex_apps)',
            '› Use /skills to list available skills',
            '  gpt-5.6-terra',
        ]
        self.assertEqual('ready', report_from_lines(wrapped_boundaries).status)
        self.assertEqual([], visible_error_lines(current_block(wrapped_boundaries).lines))

        token_wrapped = [line.replace('HTTP 401', 'HTT\nP 401').replace('⚠ ', '⚠️ ') for line in harmless]
        token_wrapped = [part for line in token_wrapped for part in line.splitlines()]
        self.assertEqual('ready', report_from_lines(token_wrapped).status)
        self.assertEqual([], visible_error_lines(current_block(token_wrapped).lines))

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
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertFalse(exact_capacity_error(lines))

    def test_status_stuck_input_when_capacity_warning_text_is_multiline_input(self) -> None:
        lines = ['────', 'done', '› Note this exact text:', '  Selected model is at capacity. Please try a different model.', '  gpt-5.5']
        self.assertEqual('stuck_input', report_from_lines(lines).status)
        self.assertEqual([], visible_error_lines(current_block(lines).lines))
        self.assertFalse(exact_capacity_error(lines))

    def test_status_not_codex_for_capacity_warning_without_codex_footer(self) -> None:
        lines = ['Selected model is at capacity. Please try a different model.']
        self.assertEqual('not_codex', report_from_lines(lines).status)

    def test_status_output_error_count_does_not_make_pane_error(self) -> None:
        lines = ['────', 'agent-status: not_codex=0 running=1 error=0 ready=0 stuck_input=0 done-registry-stale=0 pruned=0', 'running: task=active.md evidence=target=cfg:1.0 output=working', '  gpt-5.5']
        self.assertEqual('running', report_from_lines(lines).status)

    def test_inspect_reads_tmux_tail(self) -> None:
        with patch('omo_manager.omo_codex_status.exact_tail', return_value=(True, ['────', 'done', '─ Worked for 1s ─', '  gpt-5.5'])):
            report = inspect(Args('cfg:1', 20))
        self.assertEqual('ready', report.status)
        self.assertEqual(['done'], report.lines)

    def test_exact_pane_id_normalizes_omitted_pane_to_zero(self) -> None:
        result = subprocess.CompletedProcess(['tmux'], 0, 'vl:20.0\t%33515\n', '')
        with patch('omo_manager.omo_codex_status.subprocess.run', return_value=result) as run:
            self.assertEqual('%33515', exact_pane_id('vl:20'))
        run.assert_called_once_with(
            ['tmux', 'display-message', '-p', '-t', 'vl:20.0', '#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_exact_pane_id_rejects_tmux_prefix_resolution(self) -> None:
        result = subprocess.CompletedProcess(['tmux'], 0, 'wl:18.0\t%11117\n', '')
        with patch('omo_manager.omo_codex_status.subprocess.run', return_value=result):
            self.assertEqual('', exact_pane_id('wl:1.0'))

    def test_tail_does_not_capture_prefix_resolved_pane(self) -> None:
        with patch('omo_manager.omo_codex_status.exact_pane_id', return_value=''), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual([], tail('wl:1.0', 20))
        run.assert_not_called()

    def test_tail_pane_id_captures_only_canonical_pane(self) -> None:
        result = subprocess.CompletedProcess(['tmux'], 0, 'ready  \n\n', '')
        with patch('omo_manager.omo_codex_status.subprocess.run', return_value=result) as run:
            self.assertEqual(['ready'], tail_pane_id('%42', 20))
            self.assertEqual([], tail_pane_id('cfg:1.0', 20))
        run.assert_called_once_with(['tmux', 'capture-pane', '-p', '-t', '%42', '-S', '-20'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_sends_enter_for_stuck_input(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['› Continue task', '  gpt-5.5']), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_recovers_exact_search_overlay_then_submits_prompt(self) -> None:
        overlay = [
            '• Working (8s • esc to interrupt)',
            '› Previous worker is running; output=› Find and fix a bug in @filename',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes       [All Results] Filesystem Only Plugins',
        ]
        underlying = [
            '› Previous worker is running; output=› Find and fix a bug in @filename',
            '  gpt-5.5',
        ]
        report = report_from_lines(overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[overlay, overlay, underlying]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.sleep'):
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        expected = ['tmux', 'send-keys', '-t', '%7', 'Enter']
        self.assertEqual([expected, expected], [call.args[0] for call in run.call_args_list])

    def test_submit_stuck_input_recovers_changed_search_overlay(self) -> None:
        expected_overlay = [
            '• Working (8s • esc to interrupt)',
            '› Expected prompt @filename',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes       [All Results] Filesystem Only Plugins',
        ]
        changed_overlay = [
            '• Working (8s • esc to interrupt)',
            '› Different prompt @filename',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes       [All Results] Filesystem Only Plugins',
        ]
        underlying = ['› Different prompt @filename', '  gpt-5.5']
        report = report_from_lines(expected_overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[changed_overlay, underlying]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        self.assertEqual(2, run.call_count)

    def test_submit_stuck_input_submits_changed_prompt_after_search_overlay(self) -> None:
        overlay = [
            '• Working (8s • esc to interrupt)',
            '› Expected prompt @filename',
            '  no matches',
            '  enter insert · esc close · ←/→ switch search modes       [All Results] Filesystem Only Plugins',
        ]
        changed_prompt = ['› Different prompt @filename', '  gpt-5.5']
        report = report_from_lines(overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[overlay, changed_prompt]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.sleep'):
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        self.assertEqual(2, run.call_count)

    def test_submit_search_overlay_returns_success_when_ready_after_recovery(self) -> None:
        overlay = [
            '› Manager notice includes @filename',
            'no matches',
            'enter insert · esc close · ←/→ switch search modes  [All Results] Filesystem Only Plugins',
        ]
        ready = ['────', 'done', '─ Worked for 1s ─', '  gpt-5.5']
        report = report_from_lines(overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[overlay, ready]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        run.assert_called_once()

    def test_submit_search_overlay_accepts_running_stock_placeholder_after_recovery(self) -> None:
        overlay = [
            '• Working (8s • esc to interrupt)',
            '› Manager notice includes @filename',
            'no matches',
            'enter insert · esc close · ←/→ switch search modes  [All Results] Filesystem Only Plugins',
        ]
        running = ['• Working', '› Find and fix a bug in @filename', '  gpt-5.5']
        report = report_from_lines(overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[overlay, overlay, running]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.sleep'):
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_search_overlay_sends_one_recovery_enter_while_frames_stay_stale(self) -> None:
        overlay = [
            '• Working (8s • esc to interrupt)',
            '› Manager notice includes @filename',
            'no matches',
            'enter insert · esc close · ←/→ switch search modes  [All Results] Filesystem Only Plugins',
        ]
        report = report_from_lines(overlay)

        with patch('omo_manager.omo_codex_status.tail', side_effect=[overlay, overlay]), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.monotonic', side_effect=[0.0, 0.0, 2.0]):
            self.assertEqual('not_safe:file_search_overlay', submit_stuck_input_if_present('cfg:1.0', report, compaction_wait_timeout_s=1.0))

        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_sends_enter_for_terminal_enter_prompt(self) -> None:
        report = Report('stuck_input', ['Press Enter to continue...'], '', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['────', 'done', '─ Worked for 1s ─', '  gpt-5.5', 'Press Enter to continue...']), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_confirms_selected_resume_goal(self) -> None:
        lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal   Mark it active and continue when idle',
            '  2. Leave paused  Keep it paused; use /goal resume later',
            'Press enter to confirm or esc to go back',
        ]
        report = report_from_lines(lines)

        with patch('omo_manager.omo_codex_status.tail', return_value=lines), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))

        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_rechecks_changed_resume_selection(self) -> None:
        resume_lines = [
            '⚠ Selected model is at capacity. Please try a different model.',
            '⚠ This session was recorded with model `gpt-5.6-sol` but is resuming with `gpt-5.6-terra`.',
            'Resume paused goal?',
            'Goal: Finish the bounded task.',
            '› 1. Resume goal',
            '  2. Leave paused',
            'Press enter to confirm or esc to go back',
        ]
        leave_lines = [line.replace('› 1.', '  1.').replace('  2.', '› 2.') for line in resume_lines]
        report = report_from_lines(resume_lines)

        with patch('omo_manager.omo_codex_status.tail', return_value=leave_lines), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_safe:resume_goal_not_selected', submit_stuck_input_if_present('cfg:1.0', report))

        run.assert_not_called()

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
        with patch('omo_manager.omo_codex_status.tail', return_value=lines), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_reports_compaction_timeout_as_unsafe(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.wait_while_compacting', side_effect=TimeoutError), patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('not_safe:compacting', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()

    def test_submit_stuck_input_if_present_sends_enter_while_latest_screen_is_busy(self) -> None:
        report = Report('stuck_input', ['› Continue task'], 'Continue task', True)
        with patch('omo_manager.omo_codex_status.tail', return_value=['• Working', '', '› Continue task', '  gpt-5.5']), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

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

        with patch('omo_manager.omo_codex_status.tail', side_effect=fake_tail), patch('omo_manager.omo_codex_status.exact_pane_id', return_value='%7'), patch('omo_manager.omo_codex_status.subprocess.run', return_value=subprocess.CompletedProcess(['tmux'], 0)) as run, patch('omo_manager.omo_codex_status.time.sleep') as sleep:
            self.assertEqual('sent_enter', submit_stuck_input_if_present('cfg:1.0', report, compaction_wait_timeout_s=10))
        self.assertEqual(2, captures)
        self.assertEqual([2000, 2000], line_counts)
        sleep.assert_called_once()
        run.assert_called_once_with(['tmux', 'send-keys', '-t', '%7', 'Enter'], capture_output=True, text=True, timeout=5, check=False)

    def test_submit_stuck_input_if_present_ignores_non_stuck_report(self) -> None:
        report = Report('ready', ['› Use /skills to list available skills'], 'Use /skills to list available skills', False)
        with patch('omo_manager.omo_codex_status.subprocess.run') as run:
            self.assertEqual('', submit_stuck_input_if_present('cfg:1.0', report))
        run.assert_not_called()


if __name__ == '__main__':
    _ = unittest.main()
