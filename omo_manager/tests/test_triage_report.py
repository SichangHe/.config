from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from omo_manager.omo_triage_report import decision, find_status


class TriageReportTests(unittest.TestCase):
    def test_concise_report_status_comes_from_report_file_name(self) -> None:
        digest = hashlib.sha256(b"body").hexdigest()
        report_file = Path(f"/tmp/omo-agent-messages-1000/agent_done_{digest}.md")
        text = "\n".join(
            [
                "(sent from agent via omo_report.sh tmux=cfg:7 time=17:15 task-file=task.md)",
                "[message-sha256: 0123]",
                "message:",
                "implemented the change",
                "",
            ]
        )
        self.assertEqual("done", find_status(text, report_file))
        self.assertEqual("record-status", decision(text, report_file))

    def test_concise_report_header_tmux_does_not_make_report_trivial(self) -> None:
        digest = hashlib.sha256(b"body").hexdigest()
        report_file = Path(f"/tmp/omo-agent-messages-1000/agent_in-progress_{digest}.md")
        text = "\n".join(
            [
                "(sent from agent via omo_report.sh tmux=cfg:7 time=17:15 task-file=task.md)",
                "[message-sha256: 0123]",
                "message:",
                "working through the implementation",
                "",
            ]
        )
        self.assertEqual("inspect-report", decision(text, report_file))

    def test_concise_report_file_name_status_overrides_body_status_text(self) -> None:
        digest = hashlib.sha256(b"body").hexdigest()
        report_file = Path(f"/tmp/omo-agent-messages-1000/agent_blocked_{digest}.md")
        text = "\n".join(
            [
                "(sent from agent via omo_report.sh tmux=cfg:7 time=17:15 task-file=task.md)",
                "[message-sha256: 0123]",
                "message:",
                "status=done?",
                "",
            ]
        )
        self.assertEqual("blocked", find_status(text, report_file))
        self.assertEqual("ask-agent-clarify", decision(text, report_file))

    def test_legacy_header_status_stops_before_closing_bracket(self) -> None:
        text = "\n".join(
            [
                "[omo-message-source: origin=agent agent=agent via=omo_report.sh status=done]",
                "(from agent agent via omo_report.sh status=done)",
                "message:",
                "implemented the change",
                "",
            ]
        )
        self.assertEqual("done", find_status(text))
        self.assertEqual("record-status", decision(text))


if __name__ == "__main__":
    unittest.main()
