from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_task_audit import audit


def task(status: str, runat: str, blocked_on: str = "") -> str:
    blocker = f"blocked_on: {blocked_on}\n" if blocked_on else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocker}runat: {runat}\ntool: codex\nmanagerat: manager:0\nis_manager: false\npending_task_items: []\n---\n"


class TaskAuditTests(unittest.TestCase):
    def test_deterministic_todo_target_and_disposition_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:2\na.md wl:2\nb.md wl:2\nh1.md hwl:3\nh2.md hwl:3\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            (root / "b.md").write_text(task("long_running", "wl:2"))
            (root / "h1.md").write_text(task("running", "hwl:3"))
            (root / "h2.md").write_text(task("blocked", "hwl:3", "human"))
            (root / "done.md").write_text(task("done", "wl:8"))
            (root / "successor.md").write_text(task("blocked", "wl:9", "replacement.md"))
            (root / "orphan.md").write_text(task("blocked", "wl:10", "human"))
            (root / "active.md").write_text(task("running", "wl:11"))

            findings = audit(root, include_terminal=True)
            kinds = {(finding.kind, finding.key, finding.action) for finding in findings}
            self.assertIn(("duplicate_todo", "a.md", "owner_reconciliation"), kinds)
            self.assertIn(("duplicate_runat", "wl:2", "owner_reconciliation"), kinds)
            self.assertIn(("human_runat_conflict", "hwl:3", "report_only"), kinds)
            self.assertIn(("terminal_no_todo", "done.md", "none"), kinds)
            self.assertIn(("successor_blocked_no_todo", "successor.md", "verify_successor"), kinds)
            self.assertIn(("blocked_no_todo", "orphan.md", "disposition_required"), kinds)
            self.assertIn(("zero_todo", "active.md", "owner_reconciliation"), kinds)
            self.assertEqual(findings, tuple(sorted(findings)))

    def test_target_canonicalization_structured_successor_and_escaped_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs"
            root.mkdir()
            (root / "TODO.md").write_text("current:\na.md wl:2\nb.md wl:2.0\nc.md wl:3.1\nd.md wl:3.2\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            (root / "b.md").write_text(task("running", "wl:2.0"))
            (root / "c.md").write_text(task("running", "wl:3.1"))
            (root / "d.md").write_text(task("running", "wl:3.2"))
            v2 = """---
version: v2.0.0
task_id: task_00000000-0000-7000-8000-000000000001
status: blocked
resume_status: running
runat: wl:4
tool: codex
managerat: manager:0
is_manager: false
pending_task_items: []
resolved_task_items: []
blocked_on:
  - kind: task
    task: successor.md
    reason: waiting
---
"""
            (root / "structured.md").write_text(v2)
            (root / "human.md").write_text(v2.replace("task_00000000-0000-7000-8000-000000000001", "task_00000000-0000-7000-8000-000000000002").replace("runat: wl:4", "runat: wl:6").replace("  - kind: task\n    task: successor.md\n    reason: waiting", "  - kind: human\n    reason: Waiting for a review of notes.md before continuing"))
            (root / "successor.md").write_text(task("running", "wl:5"))
            outside = base / "outside.md"
            outside.write_text(task("running", "wl:9"))
            (root / "escaped.md").symlink_to(outside)

            findings = audit(root)
            conflicts = {(finding.kind, finding.key) for finding in findings if "runat" in finding.kind}
            self.assertIn(("duplicate_runat", "wl:2"), conflicts)
            self.assertNotIn(("duplicate_runat", "wl:3"), conflicts)
            self.assertIn("structured.md", {finding.key for finding in findings if finding.kind == "successor_blocked_no_todo"})
            self.assertIn("human.md", {finding.key for finding in findings if finding.kind == "blocked_no_todo"})
            self.assertNotIn("human.md", {finding.key for finding in findings if finding.kind == "successor_blocked_no_todo"})
            self.assertNotIn("escaped.md", {task_name for finding in findings for task_name in finding.tasks})

    def test_terminal_findings_are_summarized_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            (root / "a.md").write_text(task("done", "wl:1"))
            (root / "b.md").write_text(task("done", "wl:2"))
            findings = audit(root)
            self.assertEqual(1, len(findings))
            self.assertEqual("terminal_no_todo_summary", findings[0].kind)
            self.assertEqual("count=2", findings[0].detail)


if __name__ == "__main__":
    unittest.main()
