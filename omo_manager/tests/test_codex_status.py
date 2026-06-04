import unittest
from unittest.mock import patch

from omo_manager.omo_codex_status import Args, current_block, inspect, last_output, status


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


if __name__ == '__main__':
    _ = unittest.main()
