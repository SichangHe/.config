from __future__ import annotations

import tempfile
import threading
import unittest
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task_audit import Finding, TerminalDispositionError, audit, audit_and_write_reconciliation_queue, findings_introduced_since_git_head, load_terminal_dispositions, main, parse_task_lines, selected_findings, validate_selected_tasks, validate_selected_todo_refs, write_reconciliation_queue
from omo_manager.omo_task_metadata import TaskFrontmatterError


def task(status: str, runat: str, blocked_on: str = "") -> str:
    blocker = f"blocked_on: {blocked_on}\n" if blocked_on else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocker}runat: {runat}\ntool: codex\nmanagerat: manager:0\nis_manager: false\npending_task_items: []\n---\n"


class TaskAuditTests(unittest.TestCase):
    def test_bare_command_fails_only_for_findings_introduced_since_git_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:2\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.name", "Audit Test"],
                ["git", "config", "user.email", "audit@example.test"],
                ["git", "add", "TODO.md", "a.md"],
                ["git", "commit", "-qm", "baseline"],
            ):
                subprocess.run(command, cwd=root, check=True)
            (root / "TODO.md").write_text("current:\na.md wl:2\nb.md wl:2\n")
            (root / "b.md").write_text(task("running", "wl:2"))
            current = audit(root)

            introduced = findings_introduced_since_git_head(root, current)

            self.assertEqual({"duplicate_runat"}, {finding.kind for finding in introduced})
            output = StringIO()
            with patch("omo_manager.omo_task_audit.DEFAULT_ROOT", root), patch("sys.argv", ["omo_task_audit.py"]), redirect_stdout(output):
                self.assertEqual(1, main())
            self.assertIn("duplicate_runat", output.getvalue())

            subprocess.run(["git", "add", "TODO.md", "b.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "accept baseline"], cwd=root, check=True)
            output = StringIO()
            with patch("omo_manager.omo_task_audit.DEFAULT_ROOT", root), patch("sys.argv", ["omo_task_audit.py"]), redirect_stdout(output):
                self.assertEqual(0, main())
            self.assertEqual("", output.getvalue())

    def test_root_defaults_to_configured_work_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:2\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            output = StringIO()

            with patch("omo_manager.omo_task_audit.DEFAULT_ROOT", root), patch("sys.argv", ["omo_task_audit.py", "--task", "a.md", "--check"]), redirect_stdout(output):
                self.assertEqual(0, main())

            self.assertEqual("", output.getvalue())

    def test_reviewed_nineteen_record_manifest_distinguishes_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            manifest = Path(__file__).parent / "fixtures" / "blocked_record_dispositions_v1.yaml"
            dispositions = load_terminal_dispositions(manifest)
            closures = {name for name, disposition in dispositions.items() if disposition == "supported_closure"}
            owners = {name for name, disposition in dispositions.items() if disposition == "owner_disposition_required"}
            archives = {name for name, disposition in dispositions.items() if disposition == "archived_dependency"}
            self.assertEqual(
                {
                    "coconut_eval_mgr.md",
                    "contrib_eval_mgr.md",
                    "manager_mail_compression_20260728.md",
                    "verulaw_midas_docs.md",
                    "vl_mlexoh_10383.md",
                    "vlexp_arrayvec_0722.md",
                    "vl_ignore_exp_12243.md",
                },
                closures,
            )
            self.assertEqual(
                {
                    "ml_run_graph_cli_remove_2013.md",
                    "dw1_common_crawl_bounded_smoke_8649.md",
                    "dw1_common_crawl_dataset_enlargement_resume_8550_8649.md",
                    "pb_non_market_no_report_followup_8089.md",
                    "vl_l4_atmosphere_quota_preservation_plain_english_relaunch_8352.md",
                    "vl_submanager_current_8653.md",
                    "vl_tools_mgr_9522.md",
                    "vl_wave3_submanager_12344.md",
                    "vl_wrapper_release_10712.md",
                },
                owners,
            )
            self.assertEqual({"midas_artifact_clean.md", "pb_public_reusable_split_4033.md", "shmig_dd8_live.md"}, archives)
            for index, name in enumerate(dispositions):
                (root / name).write_text(task("blocked", f"wl:{index + 2}", "dependency.md"))

            findings = audit(root, terminal_dispositions=dispositions)
            by_task = {finding.key: (finding.kind, finding.action) for finding in findings if finding.key.endswith(".md")}
            self.assertEqual(19, len(by_task))
            for name in closures:
                self.assertEqual(("blocked_no_todo", "supported_closure"), by_task[name])
            for name in owners:
                self.assertEqual(("blocked_no_todo", "disposition_required"), by_task[name])
            for name in archives:
                self.assertEqual(("archived_dependency_no_todo", "none"), by_task[name])

    def test_terminal_disposition_manifest_fails_closed_on_drift_and_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\nactive.md wl:2\n")
            (root / "active.md").write_text(task("blocked", "wl:2", "human"))
            manifest = root / "dispositions.yaml"
            manifest.write_text("version: v1.0.0\nrecords:\n  - task: active.md\n    disposition: archived_dependency\n    evidence: reviewed\n")
            finding = audit(root, terminal_dispositions=load_terminal_dispositions(manifest))[0]
            self.assertEqual(("terminal_disposition_mismatch", "disposition_required"), (finding.kind, finding.action))

            for invalid in ("../escape.md", "./active.md", "sub//active.md"):
                manifest.write_text(f"version: v1.0.0\nrecords:\n  - task: {invalid}\n    disposition: archived_dependency\n    evidence: reviewed\n")
                with self.assertRaises(TerminalDispositionError):
                    load_terminal_dispositions(manifest)

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

    def test_legacy_existing_non_task_markdown_is_not_a_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            (root / "legacy-artifact.md").write_text(task("blocked", "wl:2", "paused and routed to task-data.md"))
            (root / "legacy-missing.md").write_text(task("blocked", "wl:3", "replacement.md"))
            (root / "task-data.md").write_text("preserved task data\n")
            (root / "structured.md").write_text(
                """---
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
    task: task-data.md
    reason: waiting
---
"""
            )

            findings = audit(root)
            by_task = {finding.key: (finding.kind, finding.action) for finding in findings}

            self.assertEqual(("blocked_no_todo", "disposition_required"), by_task["legacy-artifact.md"])
            self.assertEqual(("successor_blocked_no_todo", "verify_successor"), by_task["legacy-missing.md"])
            self.assertEqual(("successor_blocked_no_todo", "verify_successor"), by_task["structured.md"])

    def test_todo_target_must_match_frontmatter_runat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:3\n")
            (root / "a.md").write_text(task("running", "wl:2"))

            findings = audit(root)

            self.assertIn(
                Finding("todo_runat_mismatch", "a.md", ("a.md",), "todo=wl:3 frontmatter=wl:2", "owner_reconciliation"),
                findings,
            )

    def test_done_task_with_pending_items_requires_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("previous:\na.md wl:2\n")
            (root / "a.md").write_text(task("done", "wl:2").replace("pending_task_items: []", "pending_task_items:\n  - stale work"))

            findings = audit(root)

            self.assertIn(
                Finding("done_pending_items", "a.md", ("a.md",), "items=1", "owner_reconciliation"),
                findings,
            )

    def test_monthly_index_makes_line_only_archives_historical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\nlive.md wl:2\n\nprevious:\nstale.md wl:2\n")
            (root / "live.md").write_text(task("running", "wl:2"))
            (root / "stale.md").write_text(task("blocked", "wl:2", "paused"))
            (root / "done.md").write_text(task("done", "wl:3").replace("pending_task_items: []", "pending_task_items:\n  - preserved evidence"))
            month = root / "202608"
            month.mkdir()
            (month / "old_todos.md").write_text("done.md\n")

            findings = audit(root)

            self.assertNotIn("duplicate_runat", {finding.kind for finding in findings})
            self.assertNotIn("done_pending_items", {finding.kind for finding in findings})
            self.assertNotIn("blocked_no_todo", {finding.kind for finding in findings})

    def test_physical_month_archive_is_historical_without_an_index_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            month = root / "202607"
            month.mkdir()
            (month / "done.md").write_text(task("done", "wl:2").replace("pending_task_items: []", "pending_task_items:\n  - preserved evidence"))

            findings = audit(root)

            self.assertNotIn("done_pending_items", {finding.kind for finding in findings})

    def test_invalid_month_directory_is_not_historical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            month = root / "202613"
            month.mkdir()
            (month / "blocked.md").write_text(task("blocked", "wl:2", "human"))
            (month / "old_todos.md").write_text("blocked.md wl:2\n")

            findings = audit(root)

            self.assertIn("blocked_no_todo", {finding.kind for finding in findings})

    def test_archive_index_requires_one_matching_task_and_target(self) -> None:
        for label, archive_rows in (
            ("target", "orphan.md wl:9\n"),
            ("duplicate", "orphan.md wl:2\norphan.md wl:2\n"),
            ("prose", "- orphan.md was only mentioned in a note\n"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "TODO.md").write_text("current:\n")
                (root / "orphan.md").write_text(task("blocked", "wl:2", "human"))
                month = root / "202608"
                month.mkdir()
                (month / "old_todos.md").write_text(archive_rows)

                findings = audit(root)

                self.assertIn("blocked_no_todo", {finding.kind for finding in findings})

    def test_todo_row_requires_a_valid_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\nmissing.md wl:2\ninvalid.md wl:3\n")
            (root / "invalid.md").write_text("not task frontmatter\n")

            findings = audit(root)

            self.assertIn(Finding("todo_missing_task", "missing.md", ("missing.md",), "rows=1", "owner_reconciliation"), findings)
            self.assertIn(Finding("todo_invalid_task", "invalid.md", ("invalid.md",), "rows=1", "owner_reconciliation"), findings)

    def test_missing_root_todo_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(TaskFrontmatterError, "cannot read root TODO file"):
                audit(root)

            with patch("sys.argv", ["omo_task_audit.py", "--root", str(root), "--check"]), patch(
                "sys.stderr", new_callable=StringIO
            ), self.assertRaises(SystemExit) as raised:
                main()

            self.assertEqual(2, raised.exception.code)

    def test_todo_change_during_scan_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            todo.write_text("current:\na.md wl:2\n")
            (root / "a.md").write_text(task("running", "wl:2"))

            def changed_rows(path: Path):
                todo.write_text("current:\na.md wl:2\n../escaping.md wl:3\n")
                return parse_task_lines(path)

            with patch("omo_manager.omo_task_audit.parse_task_lines", side_effect=changed_rows), self.assertRaisesRegex(
                TaskFrontmatterError, "changed during the scan"
            ):
                audit(root)

    def test_todo_row_rejects_escape_and_symlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs"
            root.mkdir()
            (root / "TODO.md").write_text("current:\n../outside.md wl:2\nlink.md wl:3\n")
            (base / "outside.md").write_text(task("running", "wl:2"))
            (root / "actual.md").write_text(task("running", "wl:3"))
            (root / "link.md").symlink_to("actual.md")

            findings = audit(root)

            self.assertIn(
                Finding("todo_invalid_task_path", "../outside.md", ("../outside.md",), "path escapes audit root", "owner_reconciliation"),
                findings,
            )
            self.assertIn(
                Finding("todo_invalid_task_path", "link.md", ("actual.md", "link.md"), "canonical=actual.md", "owner_reconciliation"),
                findings,
            )
            self.assertEqual(
                (Finding("todo_invalid_task_path", "link.md", ("actual.md", "link.md"), "canonical=actual.md", "owner_reconciliation"),),
                selected_findings(findings, ("actual.md",)),
            )

    def test_selected_task_must_be_a_valid_existing_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\n")
            (root / "invalid.md").write_text("not task frontmatter\n")

            for selected in ("missing.md", "invalid.md"):
                with self.subTest(selected=selected), self.assertRaisesRegex(TaskFrontmatterError, selected):
                    validate_selected_tasks(root, (selected,))

            (root / "actual.md").write_text(task("running", "wl:2"))
            (root / "link.md").symlink_to("actual.md")
            with self.assertRaisesRegex(TaskFrontmatterError, "link.md"):
                validate_selected_tasks(root, ("link.md",))

            with patch("sys.argv", ["omo_task_audit.py", "--root", str(root), "--task", "missing.md", "--check"]), patch(
                "sys.stderr", new_callable=StringIO
            ), self.assertRaises(SystemExit) as raised:
                main()

            self.assertEqual(2, raised.exception.code)

    def test_exact_todo_reference_supports_scoped_missing_and_escape_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\nmissing.md wl:2\n../outside.md wl:3\n")
            output = StringIO()

            validate_selected_todo_refs(root, ("missing.md", "../outside.md"))
            with patch("sys.argv", ["omo_task_audit.py", "--root", str(root), "--todo-ref", "missing.md", "--check"]), redirect_stdout(output):
                self.assertEqual(1, main())
            self.assertIn("todo_missing_task", output.getvalue())

            with self.assertRaisesRegex(TaskFrontmatterError, "unknown.md"):
                validate_selected_todo_refs(root, ("unknown.md",))

    def test_selected_findings_support_scoped_check(self) -> None:
        findings = (
            Finding("duplicate_runat", "wl:2", ("a.md", "b.md"), "claimants=2", "owner_reconciliation"),
            Finding("zero_todo", "c.md", ("c.md",), "status=running", "owner_reconciliation"),
        )
        self.assertEqual((findings[0],), selected_findings(findings, ("b.md",)))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:2\nb.md wl:2\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            (root / "b.md").write_text(task("running", "wl:2"))
            output = StringIO()
            with patch("sys.argv", ["omo_task_audit.py", "--root", str(root), "--task", "a.md", "--check"]), redirect_stdout(output):
                self.assertEqual(1, main())
            self.assertIn("duplicate_runat", output.getvalue())

    def test_scoped_check_ignores_unrelated_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md wl:2\nb.md wl:4\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            (root / "b.md").write_text(task("running", "wl:3"))
            output = StringIO()

            with patch("sys.argv", ["omo_task_audit.py", "--root", str(root), "--task", "a.md", "--check"]), redirect_stdout(output):
                self.assertEqual(0, main())

            self.assertEqual("", output.getvalue())

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

    def test_reconciliation_queue_is_stable_and_actionable_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "queue.json"
            findings = (
                Finding("zero_todo", "a.md", ("a.md",), "status=running", "owner_reconciliation"),
                Finding("zero_todo", "b.md", ("b.md",), "status=running", "owner_reconciliation"),
                Finding("human_runat_conflict", "hwl:3", ("h.md",), "claimants=2", "report_only"),
            )
            write_reconciliation_queue(path, findings)
            first = path.read_bytes()
            before = path.stat()
            write_reconciliation_queue(path, tuple(reversed(findings)))
            self.assertEqual(first, path.read_bytes())
            self.assertEqual(before.st_ino, path.stat().st_ino)
            self.assertNotIn(b"human_runat_conflict", first)

    def test_reconciliation_queue_serializes_audit_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            (root / "TODO.md").write_text("current:\n")
            (root / "a.md").write_text(task("running", "wl:2"))
            path = Path(tmp) / "state" / "queue.json"
            barrier = threading.Barrier(3)

            def publish() -> None:
                barrier.wait()
                audit_and_write_reconciliation_queue(root, path)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(
                b'[{"action":"owner_reconciliation","detail":"status=running","key":"a.md","kind":"zero_todo","tasks":["a.md"]}]\n',
                path.read_bytes(),
            )
            self.assertFalse(tuple(path.parent.glob(f".{path.name}.*.tmp")))


if __name__ == "__main__":
    unittest.main()
