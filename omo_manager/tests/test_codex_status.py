import unittest
from unittest.mock import patch

from omo_manager.omo_codex_status import Args, inspect, last_output, status


class CodexStatusTests(unittest.TestCase):
    def test_extracts_last_output_between_separator_and_worked_line(self) -> None:
        lines = ['old', '────', ' kept  ', '', '─ Worked for 1m 2s ─', '  gpt-5.5']
        self.assertEqual([' kept'], last_output(lines))

    def test_status_requires_codex_marker_in_last_line(self) -> None:
        self.assertEqual('not_codex', status(['shell'], []))

    def test_status_ready_from_worked_line(self) -> None:
        self.assertEqual('ready', status(['────', 'done', '─ Worked for 1s ─', '  gpt-5.5'], ['done']))

    def test_status_running_without_worked_line(self) -> None:
        self.assertEqual('running', status(['working', '  gpt-5.5'], ['working']))

    def test_status_error_from_output(self) -> None:
        self.assertEqual('error', status(['────', 'Traceback', '  gpt-5.5'], ['Traceback']))

    def test_inspect_reads_tmux_tail(self) -> None:
        with patch('omo_manager.omo_codex_status.tail', return_value=['────', 'done', '─ Worked for 1s ─', '  gpt-5.5']):
            report = inspect(Args('cfg:1', 20))
        self.assertEqual('ready', report.status)
        self.assertEqual(['done'], report.lines)


if __name__ == '__main__':
    _ = unittest.main()
