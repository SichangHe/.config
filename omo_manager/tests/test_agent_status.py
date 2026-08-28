import hashlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

from omo_manager.omo_agent_status import Args, TaskFrontmatterError, TaskState, active_task_targets, classify_task, format_problem_summary, format_summary, is_authoritative_human_blocked_ready_task, is_targetless_low_priority_custody, load_local_env, load_task_state, main, parse_task_lines, parse_task_metadata, persistent_blocked_task_lines, registry_prune, report_output_evidence, scan_task_state, session_records
from omo_manager.omo_agent_status import BLOCKED_DELIVERY_ITEMS
from omo_manager.omo_agent_status import target_resolution_state
from omo_manager.omo_agent_status import SessionRecord, StatusRow, TaskLine
from omo_manager.omo_codex_status import Args as CodexStatusArgs, PlanPromptRecovery, Report, report_from_lines


def task_frontmatter(status: str, runat: str = "cfg:1", managerat: str = "mgr:1", *, is_manager: bool = False, pending_items: tuple[str, ...] = (), blocked_on: str = "") -> str:
    if status == "long_running" and not blocked_on:
        blocked_on = "persistent role"
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
    ]
    if blocked_on:
        lines.append(f"blocked_on: {blocked_on}")
    lines.extend(
        [
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
        ]
    )
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


class AgentStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmux_list_panes_patch = patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[])
        self.tmux_list_panes_patch.start()
        self.addCleanup(self.tmux_list_panes_patch.stop)

    def test_frontmatter_status_overrides_legacy_prose_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(f"{task_frontmatter('done')}(running)\n", encoding="utf-8")

            current, done, human_pending = load_task_state(root)

            self.assertEqual({}, current)
            self.assertEqual({"active.md"}, done)
            self.assertEqual(set(), human_pending)

    def test_retired_targetless_low_priority_custody_is_not_a_missing_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            registry.write_text('{"sessions":[]}', encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\nlow priority:\nparked.md\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
            task = task_frontmatter(
                "blocked",
                runat="cfg:8",
                blocked_on="direct human shutdown",
                pending_items=("preserve work",),
            ).replace("runat: cfg:8", "runat: retired")
            task += "\n(historical tmux target retired: cfg:8; authority: manager_mail/request.txt:3-3)\n"
            (root / "parked.md").write_text(task, encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_output_evidence_strips_complete_benign_codex_apps_warning(self) -> None:
        lines = [
            '⚠ MCP client for `codex_apps` failed to start: MCP startup failed: handshaking with MCP server failed: Send message error Transport',
            '  [rmcp::transport::worker::WorkerTransport<rmcp::transport::streamable_http_client::StreamableHttpClientWorker',
            '  <codex_rmcp_client::http_client_adapter::StreamableHttpClientAdapter>>] error: unexpected',
            '  server response: HTTP 401: {"error":{"message":"No available accounts","type":"proxy_error","code":401}}, when send initialize request',
            '',
            '⚠ MCP startup incomplete (failed: codex_apps)',
            '• Waiting for work.',
        ]

        evidence = report_output_evidence(Report("ready", lines))

        self.assertEqual(" output=• Waiting for work.", evidence)
        self.assertNotIn("codex_apps", evidence)

    def test_agent_status_rejects_v2_task_blocker_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "linked").symlink_to(Path(outside), target_is_directory=True)
            (root / "TODO.md").write_text("current:\nactive.md cfg:2\n", encoding="utf-8")
            (root / "active.md").write_text(
                """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000021
status: blocked
resume_status: running
runat: cfg:2
tool: codex
managerat: mgr:1
is_manager: false
blocked_on:
  - kind: task
    task: linked/task.md
    reason: escaped dependency
pending_task_items: []
resolved_task_items: []
---
""",
                encoding="utf-8",
            )

            current, done, human_pending = load_task_state(root)

            self.assertEqual({}, current)
            self.assertEqual(set(), done)
            self.assertEqual(set(), human_pending)

    def test_manager_frontmatter_rejects_self_routing(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "must be different"):
            parse_task_metadata(task_frontmatter("running", runat="wl:16", managerat="wl:16.0", is_manager=True))

    def test_worker_frontmatter_rejects_self_routing(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "must be different"):
            parse_task_metadata(task_frontmatter("running", runat="wl:16", managerat="wl:16.0"))

    def test_legacy_task_file_without_frontmatter_is_not_status_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")

            current, done, human_pending = load_task_state(root)

            self.assertEqual({}, current)
            self.assertEqual(set(), done)
            self.assertEqual(set(), human_pending)

    def test_problems_only_reports_malformed_active_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nbroken_manager.md cfg 7\n", encoding="utf-8")
            malformed = task_frontmatter("long_running", runat="cfg:7", is_manager=True).replace("tool: codex\n", "")
            _ = (root / "broken_manager.md").write_text(malformed, encoding="utf-8")
            out = StringIO()

            report = Report("stuck_input", ["queued input"], "queued input", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as submit, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            submit.assert_not_called()

            text = out.getvalue()
            self.assertIn("agent-problems: malformed_task=1", text)
            self.assertIn("malformed_task: task=broken_manager.md", text)
            self.assertIn("missing task frontmatter field: tool", text)
            self.assertEqual(1, text.count("malformed_task: task=broken_manager.md"))
            self.assertIn("untracked_agent: task=broken_manager.md", text)
            self.assertIn("unstick=disabled:malformed_task_present", text)

    def test_problems_only_reports_malformed_active_low_priority_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nbroken_manager.md cfg 7\n", encoding="utf-8")
            malformed = task_frontmatter("long_running", runat="cfg:7", is_manager=True).replace("tool: codex\n", "")
            _ = (root / "broken_manager.md").write_text(malformed, encoding="utf-8")
            out = StringIO()

            with redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            text = out.getvalue()
            self.assertIn("agent-problems: malformed_task=1", text)
            self.assertIn("malformed_task: task=broken_manager.md", text)
            self.assertIn("missing task frontmatter field: tool", text)

    def test_malformed_active_aliases_report_once_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nbroken.md cfg 8\n./broken.md cfg 8\n", encoding="utf-8")
            malformed = task_frontmatter("long_running", runat="cfg:8").replace("tool: codex\n", "")
            _ = (root / "broken.md").write_text(malformed, encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["queued input"], "queued input", True)

            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as submit, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            submit.assert_not_called()
            text = out.getvalue()
            self.assertEqual(1, text.count("malformed_task: task=broken.md"))
            self.assertIn("unstick=disabled:malformed_task_present", text)

    def test_malformed_manager_target_does_not_interrupt_waiting_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nbroken_manager.md cfg 8\n./broken_manager.md cfg 9\n", encoding="utf-8")
            malformed = task_frontmatter("long_running", runat="cfg:8", is_manager=True).replace("tool: codex\n", "")
            _ = (root / "broken_manager.md").write_text(malformed, encoding="utf-8")
            report = Report("waiting_subagent", ["waiting for reviewer"])
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.interrupt_waiting_subagent_if_present") as interrupt, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "cfg:9"]))

            interrupt.assert_not_called()
            text = out.getvalue()
            self.assertIn("malformed_task: task=broken_manager.md", text)
            self.assertIn("manager_waiting_subagent: task=manager", text)
            self.assertIn("interrupt=disabled:no_auto_unstick", text)

    def test_targetless_malformed_task_does_not_unstick_raw_tmux_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nbroken.md\n", encoding="utf-8")
            malformed = task_frontmatter("long_running", runat="cfg:9").replace("tool: codex\n", "")
            _ = (root / "broken.md").write_text(malformed, encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["queued input"], "queued input", True)

            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as submit, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["cfg:9"]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            submit.assert_not_called()
            text = out.getvalue()
            self.assertIn("malformed_task: task=broken.md", text)
            self.assertIn("untracked_agent: task=tmux:cfg:9", text)
            self.assertIn("unstick=disabled:malformed_task_present", text)

    def test_frontmatter_pending_task_items_report_after_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\ndone.md cfg 1\n", encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", pending_items=("preserve human request",)), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            text = out.getvalue()
            self.assertIn("agent-problems: human_request=1", text)
            self.assertIn("human_request: task=done.md evidence=pending_item=preserve human request", text)

    def test_frontmatter_accepts_long_running_with_blocked_on(self) -> None:
        metadata = parse_task_metadata(task_frontmatter("long_running", blocked_on="persistent contact"))

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("long_running", metadata.status)
        self.assertEqual("persistent contact", metadata.blocked_on)

    def test_frontmatter_accepts_long_running_without_blocked_on(self) -> None:
        metadata = parse_task_metadata(task_frontmatter("running").replace("status: running", "status: long_running"))

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("", metadata.blocked_on)

    def test_long_running_ready_is_quiet_but_error_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            registry.write_text('{"sessions":[]}', encoding="utf-8")
            (root / "TODO.md").write_text("current:\ncontact.md cfg:5\n", encoding="utf-8")
            (root / "contact.md").write_text(task_frontmatter("long_running", runat="cfg:5"), encoding="utf-8")
            ready = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(ready):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", ready.getvalue())
            failed = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["failed"])), redirect_stdout(failed):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=contact.md", failed.getvalue())

    def test_load_task_state_prefers_reopened_active_over_stale_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n\nprevious:\ndone.md cfg 2 (done)\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Complete, delivered to human\n- `active.md` complete.\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:2"), encoding="utf-8")
            current, done, _human_pending = load_task_state(root)
            self.assertIn("active.md", current)
            self.assertNotIn("active.md", done)
            self.assertIn("done.md", done)

    def test_load_task_state_ignores_legacy_manager_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Active / waiting\n- `tracker.md` (`pb:4`, port `18941`): running\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            current, done, human_pending = load_task_state(root)
            self.assertEqual({"active.md"}, set(current))
            self.assertEqual(set(), done)
            self.assertEqual(set(), human_pending)

    def test_load_task_state_uses_latest_task_file_tag_not_todo_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("human pending:\nreview.md (done)\n\nprevious:\nold.md (blocked)\n", encoding="utf-8")
            _ = (root / "review.md").write_text(f"{task_frontmatter('done', runat='pb:4')}(blocked)\n", encoding="utf-8")
            _ = (root / "old.md").write_text(task_frontmatter("running", runat="wl:2"), encoding="utf-8")
            current, done, human_pending = load_task_state(root)
            self.assertEqual({"old.md"}, set(current))
            self.assertEqual({"review.md"}, done)
            self.assertEqual(set(), human_pending)
            self.assertEqual("wl:2", current["old.md"].target)

    def test_load_task_state_prefers_runat_over_historical_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current.md vl 15\n", encoding="utf-8")
            _ = (root / "vl_submanager_current.md").write_text(
                f"{task_frontmatter('blocked', runat='vl:15', blocked_on='persistent role VL submanager remains active')}"
                "(done: worker report consumed)\n"
                "(manager closed Codex agent 07-02 10:35 PDT; tmux target `vl:10`; session_id: `old`.)\n"
                "(blocked: persistent role VL submanager remains active)\n",
                encoding="utf-8",
            )
            current, _done, human_pending = load_task_state(root)
            self.assertEqual(set(), set(current))
            self.assertEqual({"vl_submanager_current.md"}, human_pending)
            standby = persistent_blocked_task_lines(root)
            self.assertEqual("vl:15", standby[0].target)

    def test_problems_only_prefers_latest_runat_over_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current.md vl 15\n", encoding="utf-8")
            _ = (root / "vl_submanager_current.md").write_text(
                f"{task_frontmatter('blocked', runat='vl:15', blocked_on='persistent role VL submanager remains active')}"
                "runat: vl:10 codex\n"
                "(done: old worker closed)\n"
                "(manager closed Codex agent 07-02 10:35 PDT; tmux target `vl:10`; session_id: `old`.)\n"
                "runat: vl:15 codex\n"
                "(blocked: persistent role VL submanager remains active)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=vl_submanager_current.md evidence=target=vl:15", text)
            self.assertNotIn("target=vl:10", text)

    def test_blocked_retired_frontmatter_is_targetless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current.md\n", encoding="utf-8")
            _ = (root / "vl_submanager_current.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="retired",
                    managerat="wl:1",
                    is_manager=True,
                    blocked_on="persistent role waiting on lower manager reports; idle pane retired",
                ),
                encoding="utf-8",
            )
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            current, _done, human_pending = load_task_state(root)
            self.assertEqual({}, current)
            self.assertEqual({"vl_submanager_current.md"}, human_pending)
            self.assertEqual("", out.getvalue())

    def test_retired_runat_requires_blocked_status(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "only valid"):
            parse_task_metadata(task_frontmatter("running", runat="retired"))

    def test_fake_blocked_runat_is_invalid(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "tmux target or `retired`"):
            parse_task_metadata(task_frontmatter("blocked", runat="pb:blocked", blocked_on="waiting"))

    def test_persistent_blocked_task_lines_marks_role_from_latest_blocked_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(running)\n\n(done)\n\n(blocked: persistent VL proof-analysis role waiting for follow-up)\n", encoding="utf-8")
            current, _done, _human_pending = load_task_state(root)
            standby = persistent_blocked_task_lines(root)
            self.assertNotIn("work_manager_role.md", current)
            self.assertEqual("work_manager_role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)
            self.assertEqual("blocked", standby[0].status)

    def test_persistent_blocked_task_lines_accepts_separate_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(running)\n\n(blocked) (persistent role waiting for followup)\n", encoding="utf-8")
            standby = persistent_blocked_task_lines(root)
            self.assertEqual("work_manager_role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)

    def test_persistent_blocked_task_lines_accepts_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(running)\n\n(blocked)\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            standby = persistent_blocked_task_lines(root)
            self.assertEqual("work_manager_role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)

    def test_persistent_blocked_task_lines_rejects_nonadjacent_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(blocked)\nplain prose between status and note\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            self.assertEqual([], persistent_blocked_task_lines(root))

    def test_persistent_blocked_task_lines_rejects_blank_line_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(blocked)\n\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            self.assertEqual([], persistent_blocked_task_lines(root))

    def test_persistent_blocked_task_lines_deduplicates_todo_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nwork_manager_role.md cfg 1\nwork_manager_role.md cfg 1\n", encoding="utf-8")
            _ = (root / "work_manager_role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            standby = persistent_blocked_task_lines(root)
            self.assertEqual(1, len(standby))

    def test_parse_task_line_extracts_target_and_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\n- `task.md` (`pb:4`, port `18941`): running\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual("task.md", tasks[0].task_file)
            self.assertEqual("pb:4", tasks[0].target)
            self.assertEqual(18941, tasks[0].port)

    def test_parse_task_line_extracts_multiple_task_target_port_tuples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\n- Active batch: `one.md` (`wl:1`, port `18930`), `two.md` (`opc:4`, port `18929`), and `three.md` (`pb:0`, port `18927`).\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["one.md", "two.md", "three.md"], [task.task_file for task in tasks])
            self.assertEqual(["wl:1", "opc:4", "pb:0"], [task.target for task in tasks])
            self.assertEqual([18930, 18929, 18927], [task.port for task in tasks])

    def test_parse_task_line_ignores_artifact_paths_after_task_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text(
                "previous:\n"
                "vl_human_followup_proof_standards_8767.md vl:2 (done; documented `docs/vl-proof-standards.md`, response `manager_mail/8767_response.md`)\n"
                "trim_metadata.md vl:83 (done; `ANSWER.md`, `PROCESS.md`, and `TELEMETRY.md` now agree)\n",
                encoding="utf-8",
            )
            tasks = parse_task_lines(path)
            self.assertEqual(["vl_human_followup_proof_standards_8767.md", "trim_metadata.md"], [task.task_file for task in tasks])

    def test_parse_task_line_ignores_live_note_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text(
                "current:\n"
                "notes: see docs/vl-proof-standards.md\n"
                "notes: send manager_mail/8767_response.md to vl:2\n"
                "- note: worker output has ANSWER.md, PROCESS.md, and TELEMETRY.md\n",
                encoding="utf-8",
            )
            self.assertEqual([], parse_task_lines(path))

    def test_parse_task_line_recovers_real_task_after_artifact_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nnotes: see ANSWER.md; followup.md wl:2\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["followup.md"], [task.task_file for task in tasks])

    def test_parse_task_line_recovers_real_task_after_comma_artifact_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nnotes: see ANSWER.md, followup.md wl:2\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["followup.md"], [task.task_file for task in tasks])

    def test_parse_task_line_recovers_real_task_after_inline_artifact_comma(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nreal_task.md wl:1 (output ANSWER.md), followup.md wl:2\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["real_task.md", "followup.md"], [task.task_file for task in tasks])

    def test_parse_task_line_recovers_real_task_after_inline_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nreal_task.md wl:1 (note: report ANSWER.md); followup.md wl:2\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["real_task.md", "followup.md"], [task.task_file for task in tasks])

    def test_parse_task_line_ignores_shorthand_output_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nreal_task.md wl:1 (ANSWER.md, PROCESS.md, TELEMETRY.md)\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["real_task.md"], [task.task_file for task in tasks])

    def test_parse_task_line_keeps_multiple_no_target_task_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nfirst_task.md and second_task.md need setup\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["first_task.md", "second_task.md"], [task.task_file for task in tasks])

    def test_parse_task_line_keeps_semicolon_separated_real_task_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nactive.md wl:1; followup.md\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual(["active.md", "followup.md"], [task.task_file for task in tasks])

    def test_parse_todo_line_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("## Active / waiting\n- `verulaw_docker_resume_monitor_4081.md` has NR/OS/IR quota-limited until May 31.\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual("verulaw_docker_resume_monitor_4081.md", tasks[0].task_file)
            self.assertEqual("", tasks[0].target)

    def test_parse_task_line_extracts_loose_todo_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TODO.md"
            _ = path.write_text("current:\nloose.md cfg 1 (running)\n", encoding="utf-8")
            tasks = parse_task_lines(path)
            self.assertEqual("cfg:1", tasks[0].target)

    def test_load_local_env_reads_work_logs_root_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            local_env = home / ".config" / "omo_manager" / "local.env"
            local_env.parent.mkdir(parents=True)
            _ = local_env.write_text('export OMO_WORK_LOGS_ROOT="/tmp/current-root"\n', encoding="utf-8")
            with patch.dict("os.environ", {"HOME": str(home)}, clear=True):
                self.assertEqual("/tmp/current-root", load_local_env()["OMO_WORK_LOGS_ROOT"])

    def test_session_records_reads_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            _ = path.write_text('{"sessions":[{"task_file":"task.md","tmux_target":"cfg:1.0","port":18947,"url":"http://127.0.0.1:18947","session_id":"ses_1","started_at_s":10}]}', encoding="utf-8")
            records = session_records(path)
            self.assertEqual("task.md", records[0].task_file)
            self.assertEqual("cfg:1.0", records[0].target)

    def test_prune_completed_refuses_active_reopened_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0"},{"task_file":"done.md","tmux_target":"cfg:2.0"}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n\nprevious:\nactive.md cfg 1 (old done)\ndone.md cfg 2 (done)\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:2"), encoding="utf-8")
            current, done, _human_pending = load_task_state(root)
            self.assertIn("active.md", current)
            self.assertEqual({"done.md"}, done)
            removed = registry_prune(Args(root, registry, True, False), done)
            self.assertEqual(1, removed)
            text = registry.read_text(encoding="utf-8")
            self.assertIn("active.md", text)
            self.assertNotIn("done.md", text)
            self.assertTrue((root / "sessions.json.bak").exists())

    def test_classify_uses_codex_status_helper(self) -> None:
        task = TaskLine("task.md", "todo:current", "task.md cfg 1", "cfg:1", None)
        record = SessionRecord("task.md", "cfg:1", 18947, 1.0)
        with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["done"])):
            row = classify_task(task, record)
        self.assertEqual("ready", row.status)
        self.assertIn("done", row.evidence)

    def test_running_evidence_uses_output_before_stock_input_placeholder(self) -> None:
        task = TaskLine("task.md", "todo:current", "task.md wl 6", "wl:6", None)
        record = SessionRecord("task.md", "wl:6", 18947, 1.0)
        report = Report(
            "running",
            [
                "• Ran git status --short",
                "  └ M omo_manager/omo_agent_status.py",
                "",
                "› Find and fix a bug in @filename",
            ],
            "Find and fix a bug in @filename",
        )

        with patch("omo_manager.omo_agent_status.inspect", return_value=report):
            row = classify_task(task, record)

        self.assertEqual("running", row.status)
        self.assertIn("output=• Ran git status --short /   └ M omo_manager/omo_agent_status.py", row.evidence)
        self.assertNotIn("@filename", row.evidence)

    def test_problem_summary_includes_only_problem_rows(self) -> None:
        rows = [
            StatusRow("ok.md", "running", "target=cfg:1"),
            StatusRow("bad.md", "error", "target=cfg:2 output=traceback"),
            StatusRow("idle.md", "ready", "target=cfg:3 output=ready"),
            StatusRow("gone.md", "not_codex", "target=cfg:4"),
            StatusRow("input.md", "stuck_input", "target=cfg:5 output=› Continue task"),
        ]
        text = format_problem_summary(rows, {"done.md"})
        self.assertIn("agent-problems: not_codex=1 error=1 ready=1 stuck_input=1 done-registry-stale=1", text)
        self.assertIn("error: task=bad.md", text)
        self.assertIn("ready: task=idle.md", text)
        self.assertIn("not_codex: task=gone.md", text)
        self.assertIn("stuck_input: task=input.md", text)
        self.assertIn("done-stale: task=done.md", text)
        self.assertNotIn("ok.md", text)

    def test_problem_summary_reports_not_codex_blocked_persistent_roles(self) -> None:
        rows = [
            StatusRow("standby.md", "ready", "target=cfg:1 persistent_role=true task_status=blocked", True, "blocked"),
            StatusRow("broken.md", "error", "target=cfg:2 persistent_role=true task_status=blocked", True, "blocked"),
            StatusRow("gone.md", "not_codex", "target=cfg:3 persistent_role=true task_status=blocked", True, "blocked"),
            StatusRow("wrong-marker.md", "ready", "target=cfg:5 persistent_role=true task_status=running", True, "running"),
            StatusRow("ordinary.md", "ready", "target=cfg:4"),
        ]
        text = format_problem_summary(rows, set())
        self.assertIn("agent-problems: not_codex=1 error=1 ready=2", text)
        self.assertNotIn("stuck_input=0", text)
        self.assertNotIn("done-registry-stale=0", text)
        self.assertNotIn("standby.md", text)
        self.assertIn("error: task=broken.md", text)
        self.assertIn("not_codex: task=gone.md", text)
        self.assertIn("ready: task=ordinary.md", text)
        self.assertIn("ready: task=wrong-marker.md", text)

    def test_summaries_keep_blocked_active_rows_quiet(self) -> None:
        rows = [
            StatusRow("active.md", "running", "target=cfg:1 task_status=running", task_status="running"),
            StatusRow("blocked-running.md", "running", "target=cfg:2 task_status=blocked", task_status="blocked"),
            StatusRow("blocked-ready.md", "ready", "target=cfg:3 task_status=blocked", task_status="blocked"),
            StatusRow("blocked-idle.md", "blocked_idle", "target=cfg:4 task_status=blocked", task_status="blocked"),
            StatusRow("blocked-error.md", "error", "target=cfg:5 task_status=blocked", task_status="blocked"),
            StatusRow("blocked-gone.md", "not_codex", "target=cfg:6 task_status=blocked", task_status="blocked"),
            StatusRow("blocked-input.md", "stuck_input", "target=cfg:7 task_status=blocked", task_status="blocked"),
        ]

        status_text = format_summary(rows, 0, 0)
        self.assertIn("agent-status: not_codex=1 running=1 blocked_idle=1 error=1 ready=0 stuck_input=1", status_text)
        self.assertNotIn("blocked-running.md", status_text)
        self.assertNotIn("blocked-ready.md", status_text)

        problem_text = format_problem_summary(rows, set())
        self.assertIn("agent-problems: not_codex=1 blocked_idle=1 error=1 stuck_input=1", problem_text)
        self.assertNotIn("blocked-running.md", problem_text)
        self.assertNotIn("blocked-ready.md", problem_text)
        self.assertIn("blocked_idle: task=blocked-idle.md", problem_text)
        self.assertIn("error: task=blocked-error.md", problem_text)
        self.assertIn("not_codex: task=blocked-gone.md", problem_text)
        self.assertIn("stuck_input: task=blocked-input.md", problem_text)

    def test_problems_only_stays_quiet_when_all_active_agents_are_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_stays_quiet_for_review_placeholder_during_background_terminal_wait(self) -> None:
        pane = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertEqual("", out.getvalue())

    def test_problems_only_stays_quiet_for_explain_placeholder_during_work(self) -> None:
        pane = ['• Working (4m 34s • esc to interrupt)', '', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertEqual("", out.getvalue())

    def test_problems_only_stays_quiet_for_implement_placeholder_during_work(self) -> None:
        pane = ['• Working (4m 34s • esc to interrupt)', '', '› Implement {feature}', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertEqual("", out.getvalue())

    def test_codex_status_treats_ask_codex_prompt_as_empty_ready_input(self) -> None:
        pane = [
            '─ Worked for 34m 12s ─',
            '',
            '',
            '› Ask Codex to do anything',
            '',
            '  gpt-5.5 xhigh · /work · 1.74M used · Context 86% used',
        ]

        report = report_from_lines(pane)

        self.assertEqual("ready", report.status)
        self.assertEqual("Ask Codex to do anything", report.input_text)
        self.assertFalse(report.can_submit_input)

    def test_problems_only_reports_blocked_idle_for_blocked_ask_codex_placeholder(self) -> None:
        pane = [
            '─ Worked for 34m 12s ─',
            '',
            '',
            '› Ask Codex to do anything',
            '',
            '  gpt-5.5 xhigh · /work · 1.74M used · Context 86% used',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="helper receipt stall"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=1", text)
            self.assertIn("blocked_idle: task=role.md", text)
            self.assertNotIn("stuck_input", text)

    def test_problems_only_stays_quiet_for_queued_input_footer_during_work(self) -> None:
        pane = [
            '• Working (19m 47s • esc to interrupt)',
            '',
            '› [Pasted Content 1020 chars] as hypothesis-generating, not reassuring proof.',
            '  - Add population and marker tables.',
            '',
            '',
            '  tab to queue message                                                                                    28% context left',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_selected_model_capacity_warning_as_error(self) -> None:
        pane = ['────', '⚠ Selected model is at capacity. Please try a different model.', '› Use /skills to list available skills', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=active.md", text)
            self.assertNotIn("ready: task=active.md", text)

    def test_problems_only_treats_codex_apps_no_account_warning_as_ready(self) -> None:
        pane = [
            '────',
            '⚠ MCP client for `codex_apps` failed to start: MCP startup failed: handshaking with MCP server failed: Send message error Transport',
            '  [rmcp::transport::worker::WorkerTransport<rmcp::transport::streamable_http_client::StreamableHttpClientWorker',
            '  <codex_rmcp_client::http_client_adapter::StreamableHttpClientAdapter>>] error: unexpected',
            '  server response: HTTP 401: {"error":{"message":"No available accounts","type":"proxy_error","code":401}}, when send initialize request',
            '',
            '⚠ MCP startup incomplete (failed: codex_apps)',
            '› Use /skills to list available skills',
            '  gpt-5.5',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("ready: task=active.md", out.getvalue())
            self.assertNotIn("error: task=active.md", out.getvalue())

    def test_problems_only_recovers_capacity_error_before_trailing_goal_footer(self) -> None:
        pane = [
            '• Context compacted',
            '',
            '⚠ Selected model is at capacity. Please try a different model.',
            '',
            '› Use /skills to list available skills',
            '',
            '  gpt-5.5 medium · ~/.config',
            '  Goal blocked (/goal resume)',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=active.md", text)
            self.assertIn("output=⚠ Selected model is at capacity. Please try a different model.", text)
            self.assertNotIn("not_codex: task=active.md", text)

    def test_error_evidence_prefers_warning_line_above_input_box(self) -> None:
        pane = ['────', '⚠ Selected model is at capacity. Please try a different model.', 'note', 'detail', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("output=⚠ Selected model is at capacity. Please try a different model.", text)
            self.assertIn("output_tail=note / detail / › Explain this codebase", text)

    def test_error_evidence_prefers_square_marker_line_above_input_box(self) -> None:
        pane = ['────', '■ Error: 429 Too Many Requests', 'note', 'detail', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("output=■ Error: 429 Too Many Requests", text)
            self.assertIn("output_tail=note / detail / › Explain this codebase", text)

    def test_error_evidence_omits_benign_unmarked_error_text_when_marked_error_exists(self) -> None:
        pane = ['────', 'No error found in cache warmup', '■ Error: 429 Too Many Requests', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present"), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("output=■ Error: 429 Too Many Requests", text)
            self.assertNotIn("output=No error found", text)

    def test_problems_only_does_not_report_status_output_error_count_as_error(self) -> None:
        pane = ['────', 'agent-status: not_codex=0 running=1 error=0 ready=0 stuck_input=0 done-registry-stale=0 pruned=0', 'running: task=active.md evidence=target=cfg:1.0 output=working', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_stays_quiet_for_idle_explain_placeholder(self) -> None:
        pane = ['────', 'done', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertIn("ready: task=active.md", out.getvalue())
            self.assertNotIn("stuck_input: task=active.md", out.getvalue())

    def test_problems_only_stays_quiet_for_idle_implement_placeholder(self) -> None:
        pane = ['────', 'done', '› Implement {feature}', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertIn("ready: task=active.md", out.getvalue())
            self.assertNotIn("stuck_input: task=active.md", out.getvalue())

    def test_problems_only_reports_user_entered_review_input_during_background_terminal_wait(self) -> None:
        pane = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes and summarize findings', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once()
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstuck: target=cfg:1.0 task=active.md action=sent_enter", text)

    def test_problems_only_auto_unsticks_safe_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue task"], "Continue task", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("cfg:1.0", report)
            self.assertIn("stuck_input: task=active.md", out.getvalue())
            self.assertIn("unstick=sent_enter", out.getvalue())

    def test_problems_only_dismisses_plan_prompt_with_audit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            modal = ['› Continue task', '', '  Create a plan?  shift + tab use Plan mode   esc dismiss']
            report = Report("stuck_input", modal, "Continue task", True)
            out = StringIO()
            recovery = PlanPromptRecovery("sent_escape", "plan_prompt", "stuck_input")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.dismiss_plan_prompt_if_present", return_value=recovery) as dismiss, patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as submit, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            dismiss.assert_called_once_with("cfg:1.0", report)
            submit.assert_not_called()
            text = out.getvalue()
            self.assertIn("recovery=plan_prompt->stuck_input unstick=sent_escape", text)
            self.assertIn("unstuck: target=cfg:1.0 task=active.md action=sent_escape", text)

    def test_problems_only_unsticks_each_target_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1},{"task_file":"other.md","tmux_target":"cfg:1.0","started_at_s":2}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\nother.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            _ = (root / "other.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue task"], "Continue task", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("cfg:1.0", report)
            text = out.getvalue()
            self.assertEqual(1, text.count("unstuck: target=cfg:1.0"))
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstick=already_sent", text)

    def test_problems_only_unsticks_alias_target_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1","started_at_s":1},{"task_file":"other.md","tmux_target":"cfg:1.0","started_at_s":2}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\nother.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            _ = (root / "other.md").write_text(task_frontmatter("running", runat="cfg:1.0"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue task"], "Continue task", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("cfg:1", report)
            text = out.getvalue()
            self.assertEqual(1, text.count("unstuck: target=cfg:1"))
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstick=already_sent", text)

    def test_problems_only_auto_unsticks_manager_target_without_direct_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Reply to human"], "Reply to human", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:1.0"]))
            unstick.assert_called_once()
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertIn("stuck_input: task=manager evidence=target=wl:1.0 role=manager", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstuck: target=wl:1.0 task=manager action=sent_enter", text)

    def test_problems_only_auto_unsticks_manager_target_alias_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"wl:1","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md wl 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="wl:1", managerat="wl:2"), encoding="utf-8")
            report = Report("stuck_input", ["› manager status text"], "manager status text", True)
            out = StringIO()
            def fake_inspect(args: object, **_: object) -> Report:
                return report if getattr(args, "target") == "wl:1" else Report("running", ["working"])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:2"]))
            unstick.assert_called_once()
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=wl:1", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstuck: target=wl:1 task=active.md action=sent_enter", text)

    def test_problems_only_omits_ready_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["mgr:1"]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_interrupts_manager_waiting_on_subagent_without_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            report = Report("waiting_subagent", ["• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9", "• Working (21s • esc to interrupt)", "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)"])
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report) as inspect_call, patch("omo_manager.omo_agent_status.interrupt_waiting_subagent_if_present", return_value="sent_escape") as interrupt, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            inspect_call.assert_called_once_with(CodexStatusArgs("mgr:1.0", 80), detect_waiting_subagent=True)
            interrupt.assert_called_once_with("mgr:1.0", report)
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_manager_waiting_on_subagent_when_escape_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            report = Report("waiting_subagent", ["• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9", "• Working (21s • esc to interrupt)", "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)"])
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report) as inspect_call, patch("omo_manager.omo_agent_status.interrupt_waiting_subagent_if_present", return_value="failed"), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            inspect_call.assert_called_once_with(CodexStatusArgs("mgr:1.0", 80), detect_waiting_subagent=True)
            text = out.getvalue()
            self.assertIn("agent-problems: manager_waiting_subagent=1", text)
            self.assertIn("manager_waiting_subagent: task=manager evidence=target=mgr:1.0 role=manager", text)
            self.assertIn("interrupt=failed", text)

    def test_problems_only_does_not_interrupt_worker_waiting_on_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1.0", managerat="mgr:1"), encoding="utf-8")
            out = StringIO()

            def fake_inspect(args: object, **_: object) -> Report:
                target = getattr(args, "target")
                if target == "cfg:1.0":
                    return Report("waiting_subagent", ["• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9", "• Working (21s • esc to interrupt)", "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)"])
                return Report("running", ["manager active"])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.interrupt_waiting_subagent_if_present") as interrupt, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            interrupt.assert_not_called()
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_manager_compaction_reread_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "", "› Continue managing"], "Continue managing", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            text = out.getvalue()
            self.assertIn("agent-problems: manager_compaction=1", text)
            self.assertIn("manager-action: manager_compaction>0 reread MANAGER.md", text)
            self.assertIn("manager_compaction: task=manager evidence=target=mgr:1.0 role=manager", text)

    def test_problems_only_skips_manager_compaction_when_reread_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "Will reread MANAGER.md after compacting"], "", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_manager_compaction_even_when_manager_target_is_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"mgr:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md mgr 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="mgr:1.0", managerat="wl:1"), encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "", "› Continue managing"], "Continue managing", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertIn("manager_compaction: task=manager evidence=target=mgr:1.0 role=manager", out.getvalue())

    def test_problems_only_does_not_treat_negative_reread_text_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "did not reread MANAGER.md yet"], "", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertIn("manager_compaction: task=manager evidence=target=mgr:1.0 role=manager", out.getvalue())

    def test_problems_only_does_not_treat_passive_negative_reread_text_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            for line in ("MANAGER.md was not reread after compaction", "MANAGER.md is not reread yet", "No need to reread MANAGER.md after compaction"):
                with self.subTest(line=line):
                    out = StringIO()
                    report = Report("running", ["• Compacting conversation", line], "", False, "compacting")
                    with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                        self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
                    self.assertIn("manager_compaction: task=manager evidence=target=mgr:1.0 role=manager", out.getvalue())

    def test_problems_only_does_not_treat_question_reread_text_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "should I reread MANAGER.md?"], "", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertIn("manager_compaction: task=manager evidence=target=mgr:1.0 role=manager", out.getvalue())

    def test_problems_only_accepts_first_person_completed_reread_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            report = Report("running", ["• Compacting conversation", "I reread MANAGER.md after compaction"], "", False, "compacting")
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_ready_running_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("ready: task=active.md", out.getvalue())

    def test_problems_only_reports_missing_exact_main_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_codex_status.exact_pane_id", return_value=""), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:1.0"]))

            self.assertIn("missing: task=manager evidence=target=wl:1.0 role=manager", out.getvalue())

    def test_problems_only_reports_ready_running_persistent_role_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("ready: task=role.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL spec-analysis role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=role.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_manager_without_concrete_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"manager.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg 1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="persistent submanager waiting on lower manager and human follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=manager.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_nonpersistent_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"manager.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg 1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="waiting on worker report"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=manager.md", out.getvalue())

    def test_problems_only_reports_error_for_blocked_manager_with_recorded_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"manager.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg 1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="persistent submanager waiting on lower manager and human follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=manager.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_blocked_manager_with_recorded_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"manager.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg 1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="persistent submanager waiting on lower manager and human follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=manager.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_blocked_manager_with_concrete_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_ab_prep_mgr_11375.md vl:3\n", encoding="utf-8")
            _ = (root / "vl_ab_prep_mgr_11375.md").write_text(
                task_frontmatter("blocked", runat="vl:3", managerat="vl:2", is_manager=True, blocked_on="vl_closefix_11375.md"),
                encoding="utf-8",
            )
            _ = (root / "vl_closefix_11375.md").write_text(task_frontmatter("running", runat="vl:12", managerat="vl:3"), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", ["intentional shell"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]), out.getvalue())

            self.assertIn("not_codex: task=vl_ab_prep_mgr_11375.md", out.getvalue())

    def test_packet_manager_stays_quiet_until_one_of_four_running_dependencies_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            dependencies = (
                ("vlp_ironkv_11375.md", "vl:5"),
                ("vlp_authrepair_11375.md", "vl:12"),
                ("vlp_evalctr_11375.md", "vl:14"),
                ("vlp_review3_11375.md", "vl:16"),
            )
            todo = ["current:", "vl_pkt_mgr_11375.md vl:10", *(f"{name} {target}" for name, target in dependencies)]
            _ = (root / "TODO.md").write_text("\n".join(todo) + "\n", encoding="utf-8")
            _ = (root / "vl_pkt_mgr_11375.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl:10",
                    managerat="vl:3",
                    is_manager=True,
                    blocked_on=", ".join(name for name, _target in dependencies),
                ),
                encoding="utf-8",
            )
            for name, target in dependencies:
                _ = (root / name).write_text(task_frontmatter("running", runat=target, managerat="vl:10"), encoding="utf-8")

            def inspect_target(args: CodexStatusArgs, **_kwargs: object) -> Report:
                return Report("ready", ["parked manager"]) if args.target == "vl:10" else Report("running", ["Working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

            changed = root / dependencies[0][0]
            _ = changed.write_text(task_frontmatter("done", runat=dependencies[0][1], managerat="vl:10"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_pkt_mgr_11375.md", out.getvalue())

            _ = changed.write_text(
                f"{task_frontmatter('running', runat=dependencies[0][1], managerat='vl:10')}(pending)\nchild report\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_pkt_mgr_11375.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_manager_with_stale_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg:1\n\nprevious:\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="cfg:9", is_manager=True, blocked_on="dependency.md"), encoding="utf-8")
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="cfg:1"), encoding="utf-8")

            def inspect_target(args: CodexStatusArgs, **_kwargs: object) -> Report:
                return Report("ready", ["parked manager"]) if args.target == "cfg:1" else Report("running", ["Working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertIn("blocked_idle: task=manager.md", out.getvalue())

    def test_prep_manager_accepts_blocked_packet_manager_with_four_running_leaves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            packet_dependencies = (
                ("vlp_ironkv_11375.md", "vl:5"),
                ("vlp_authrepair_11375.md", "vl:12"),
                ("vlp_evalctr_11375.md", "vl:14"),
                ("vlp_review3_11375.md", "vl:16"),
            )
            prep_running_dependencies = (
                ("vlprep_runtime_11375.md", "vl:7"),
                ("vlprep_mlexeval_11375.md", "vl:8"),
                ("vlprep_admit_11375.md", "vl:18"),
            )
            todo = [
                "current:",
                "vl_ab_prep_mgr_11375.md vl:3",
                "vl_pkt_mgr_11375.md vl:10",
                *(f"{name} {target}" for name, target in prep_running_dependencies),
                *(f"{name} {target}" for name, target in packet_dependencies),
            ]
            _ = (root / "TODO.md").write_text("\n".join(todo) + "\n", encoding="utf-8")
            prep_blockers = (*prep_running_dependencies[:2], ("vl_pkt_mgr_11375.md", "vl:10"), prep_running_dependencies[2])
            _ = (root / "vl_ab_prep_mgr_11375.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl:3",
                    managerat="vl:2",
                    is_manager=True,
                    blocked_on=", ".join(name for name, _target in prep_blockers),
                ),
                encoding="utf-8",
            )
            packet_path = root / "vl_pkt_mgr_11375.md"
            packet_blockers = ", ".join(name for name, _target in packet_dependencies)
            _ = packet_path.write_text(
                task_frontmatter("blocked", runat="vl:10", managerat="vl:3", is_manager=True, blocked_on=packet_blockers),
                encoding="utf-8",
            )
            for name, target in prep_running_dependencies:
                _ = (root / name).write_text(task_frontmatter("running", runat=target, managerat="vl:3"), encoding="utf-8")
            for name, target in packet_dependencies:
                _ = (root / name).write_text(task_frontmatter("running", runat=target, managerat="vl:10"), encoding="utf-8")

            def inspect_target(args: CodexStatusArgs, **_kwargs: object) -> Report:
                return Report("ready", ["parked manager"]) if args.target in {"vl:3", "vl:10"} else Report("running", ["Working"])

            def problems() -> tuple[int, str]:
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), redirect_stdout(out):
                    status = main(["--root", str(root), "--registry", str(registry), "--problems-only"])
                return status, out.getvalue()

            self.assertEqual((0, ""), problems())

            def problems_with_missing_target(missing_target: str) -> tuple[int, str]:
                out = StringIO()

                def inspect_with_missing(args: CodexStatusArgs, **_kwargs: object) -> Report:
                    return Report("not_codex", []) if args.target == missing_target else inspect_target(args)

                with patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_with_missing), redirect_stdout(out):
                    status = main(["--root", str(root), "--registry", str(registry), "--problems-only"])
                return status, out.getvalue()

            status, text = problems_with_missing_target("vl:3")
            self.assertEqual(3, status)
            self.assertIn("not_codex: task=vl_ab_prep_mgr_11375.md", text)

            status, text = problems_with_missing_target("vl:10")
            self.assertEqual(3, status)
            self.assertIn("not_codex: task=vl_pkt_mgr_11375.md", text)

            leaf_path = root / packet_dependencies[0][0]
            _ = leaf_path.write_text(task_frontmatter("done", runat=packet_dependencies[0][1], managerat="vl:10"), encoding="utf-8")
            status, text = problems()
            self.assertEqual(3, status)
            self.assertIn("blocked_idle: task=vl_pkt_mgr_11375.md", text)
            self.assertIn("blocked_idle: task=vl_ab_prep_mgr_11375.md", text)

            _ = leaf_path.write_text(task_frontmatter("running", runat=packet_dependencies[0][1], managerat="vl:10"), encoding="utf-8")
            _ = packet_path.write_text(
                f"{task_frontmatter('blocked', runat='vl:10', managerat='vl:3', is_manager=True, blocked_on=packet_blockers)}(pending)\nchild report\n",
                encoding="utf-8",
            )
            status, text = problems()
            self.assertEqual(3, status)
            self.assertIn("blocked_idle: task=vl_ab_prep_mgr_11375.md", text)

            _ = packet_path.write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl:10",
                    managerat="vl:3",
                    is_manager=True,
                    blocked_on=f"{packet_dependencies[0][0]}, {packet_dependencies[0][0]}",
                ),
                encoding="utf-8",
            )
            status, text = problems()
            self.assertEqual(3, status)
            self.assertIn("blocked_idle: task=vl_ab_prep_mgr_11375.md", text)

            _ = packet_path.write_text(
                task_frontmatter("blocked", runat="vl:10", managerat="vl:3", is_manager=True, blocked_on=packet_blockers),
                encoding="utf-8",
            )
            _ = leaf_path.write_text(task_frontmatter("running", runat=packet_dependencies[0][1], managerat="vl:99"), encoding="utf-8")
            status, text = problems()
            self.assertEqual(3, status)
            self.assertIn("blocked_idle: task=vl_ab_prep_mgr_11375.md", text)

    def test_problems_only_reports_not_codex_for_dependency_mixed_with_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_manager.md vl:3\n", encoding="utf-8")
            _ = (root / "vl_manager.md").write_text(
                task_frontmatter("blocked", runat="vl:3", managerat="vl:2", is_manager=True, blocked_on="persistent VL manager role waiting on vl_closefix.md"),
                encoding="utf-8",
            )
            _ = (root / "vl_closefix.md").write_text(task_frontmatter("running", runat="vl:12"), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertIn("not_codex: task=vl_manager.md", out.getvalue())

    def test_problems_only_still_reports_not_codex_for_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_nonactionable_blocker(self) -> None:
        for name, blocker in (("arbitrary", "waiting"), ("missing", "missing.md"), ("self", "worker.md")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
                _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on=blocker), encoding="utf-8")
                out = StringIO()

                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

                self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_suppresses_deliberately_closed_manager_with_live_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="dependency.md replacement custody pending")
                + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `cfg:1`; session_id: `old`.)\n",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="cfg:1"), encoding="utf-8")
            out = StringIO()

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", [])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertEqual("", out.getvalue())

    def test_problems_only_suppresses_missing_closed_manager_with_live_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="dependency.md replacement custody pending")
                + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `cfg:1`; session_id: `old`.)\n",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="cfg:1"), encoding="utf-8")

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("missing", [])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(StringIO()) as out:
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_suppresses_long_running_closed_manager_with_live_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvlexp_replenish_safe.md vl:4\nvl_rebuild_mgr.md vl:5\n", encoding="utf-8")
            _ = (root / "vlexp_replenish_safe.md").write_text(
                task_frontmatter("long_running", runat="vl:4", is_manager=True, blocked_on="vl_rebuild_mgr.md replacement custody pending")
                + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `vl:4`; session_id: `old`.)\n",
                encoding="utf-8",
            )
            _ = (root / "vl_rebuild_mgr.md").write_text(task_frontmatter("long_running", runat="vl:5", managerat="vl:4", is_manager=True, blocked_on="persistent role"), encoding="utf-8")
            out = StringIO()

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:5" else Report("not_codex", [])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertEqual("", out.getvalue())

    def test_problems_only_suppresses_closed_manager_in_previous_with_current_live_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_rebuild_mgr.md vl:5\n\nprevious:\nvlexp_replenish_safe.md vl:4\n", encoding="utf-8")
            _ = (root / "vlexp_replenish_safe.md").write_text(
                task_frontmatter("long_running", runat="vl:4", is_manager=True, blocked_on="vl_rebuild_mgr.md replacement custody pending")
                + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `vl:4`; session_id: `old`.)\n",
                encoding="utf-8",
            )
            _ = (root / "vl_rebuild_mgr.md").write_text(task_frontmatter("long_running", runat="vl:5", managerat="vl:4", is_manager=True, blocked_on="persistent role"), encoding="utf-8")
            out = StringIO()

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:5" else Report("not_codex", [])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertEqual("", out.getvalue())

    def test_problems_only_keeps_closed_long_running_manager_alerts(self) -> None:
        cases = {
            "visible_output": ("vl_rebuild_mgr.md", Report("not_codex", ["shell prompt"])),
            "error": ("vl_rebuild_mgr.md", Report("error", ["failure"])),
            "stuck_input": ("vl_rebuild_mgr.md", Report("stuck_input", ["queued input"])),
            "pending_dependency": ("vl_rebuild_mgr.md", Report("not_codex", [])),
            "missing": ("missing.md", Report("not_codex", [])),
            "terminal": ("vl_rebuild_mgr.md", Report("not_codex", [])),
            "self": ("vlexp_replenish_safe.md", Report("not_codex", [])),
            "duplicate": ("vl_rebuild_mgr.md, vl_rebuild_mgr.md", Report("not_codex", [])),
            "target_reuse": ("vl_rebuild_mgr.md", Report("not_codex", [])),
        }
        for name, (blocker, manager_report) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                todo = "current:\nvlexp_replenish_safe.md vl:4\n"
                if name not in {"missing", "self"}:
                    todo += "vl_rebuild_mgr.md vl:5\n"
                if name == "target_reuse":
                    todo += "reused.md vl:5\n"
                _ = (root / "TODO.md").write_text(todo, encoding="utf-8")
                _ = (root / "vlexp_replenish_safe.md").write_text(
                    task_frontmatter("long_running", runat="vl:4", is_manager=True, blocked_on=f"{blocker} replacement custody pending")
                    + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `vl:4`; session_id: `old`.)\n",
                    encoding="utf-8",
                )
                if name not in {"missing", "self"}:
                    dependency_status = "done" if name == "terminal" else "long_running"
                    dependency_text = task_frontmatter(dependency_status, runat="vl:5", managerat="vl:4", is_manager=True, blocked_on="persistent role")
                    if name == "pending_dependency":
                        dependency_text += "(pending)\n"
                    _ = (root / "vl_rebuild_mgr.md").write_text(dependency_text, encoding="utf-8")
                if name == "target_reuse":
                    _ = (root / "reused.md").write_text(task_frontmatter("running", runat="vl:5"), encoding="utf-8")
                out = StringIO()

                def fake_inspect(args: object, **_: object) -> Report:
                    return manager_report if getattr(args, "target") == "vl:4" else Report("running", ["working"])

                with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

                self.assertIn("task=vlexp_replenish_safe.md", out.getvalue())

    def test_problems_only_suppresses_closed_manager_with_v2_task_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg:1\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(
                """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000021
status: blocked
resume_status: running
runat: cfg:1
tool: codex
managerat: mgr:1
is_manager: true
blocked_on:
  - kind: task
    task: dependency.md
    reason: replacement custody pending
pending_task_items: []
resolved_task_items: []
---
(manager closed Codex agent 07-29 17:20 PDT; tmux target `cfg:1`; session_id: `old`.)
""",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("long_running", runat="cfg:2", managerat="cfg:1", blocked_on="persistent role"), encoding="utf-8")

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", [])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_closed_manager_with_self_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg:1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="manager.md replacement custody pending")
                + "(manager closed Codex agent 07-29 17:20 PDT; tmux target `cfg:1`; session_id: `old`.)\n",
                encoding="utf-8",
            )
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=manager.md", out.getvalue())

    def test_problems_only_suppresses_resumable_stopped_worker_with_running_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:99\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="cfg:99",
                    managerat="mgr:1",
                    pending_items=("finish preserved work",),
                    blocked_on="dependency.md",
                )
                + "This stopped record-only role has preserved Codex session 019f64f1-a087-7e32-baba-e4bc07455f86.\n"
                + "On authorized resume, use that exact session.\n",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="mgr:1"), encoding="utf-8")

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", [])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_resumable_stopped_worker_when_target_still_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:99\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="cfg:99",
                    managerat="mgr:1",
                    pending_items=("finish preserved work",),
                    blocked_on="dependency.md",
                )
                + "This stopped record-only role has preserved Codex session 019f64f1-a087-7e32-baba-e4bc07455f86.\n"
                + "On authorized resume, use that exact session.\n",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="mgr:1"), encoding="utf-8")

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", [])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=True), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_reports_resumable_stopped_worker_with_visible_not_codex_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:99\ndependency.md cfg:2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="cfg:99",
                    managerat="mgr:1",
                    pending_items=("finish preserved work",),
                    blocked_on="dependency.md",
                )
                + "This stopped record-only role has preserved Codex session 019f64f1-a087-7e32-baba-e4bc07455f86.\n"
                + "On authorized resume, use that exact session.\n",
                encoding="utf-8",
            )
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="mgr:1"), encoding="utf-8")

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", ["fish prompt"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("not_codex: task=worker.md", text)
            self.assertIn("output=fish prompt", text)

    def test_problems_only_reports_invalid_resumable_stopped_worker_records(self) -> None:
        cases = (
            ("missing dependency", "missing.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), True, ("finish preserved work",), "current"),
            ("malformed blocker", "waiting on dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), True, ("finish preserved work",), "current"),
            ("stale dependency", "dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), True, ("finish preserved work",), "previous"),
            ("inactive dependency", "dependency.md", (("dependency.md", "done", "cfg:2", "mgr:1", ""),), True, ("finish preserved work",), "current"),
            ("cyclic dependency", "dependency.md", (("dependency.md", "blocked", "cfg:2", "mgr:1", "worker.md"),), True, ("finish preserved work",), "current"),
            ("absent pending item", "dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), True, (), "current"),
            ("absent resume evidence", "dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), False, ("finish preserved work",), "current"),
            ("frontmatter-only resume evidence", "dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), False, ("preserved Codex session 019f64f1-a087-7e32-baba-e4bc07455f86; resume later",), "current"),
            ("unrelated uuid evidence", "dependency.md", (("dependency.md", "running", "cfg:2", "mgr:1", ""),), "uuid", ("finish preserved work",), "current"),
        )
        resume_text = (
            "This stopped record-only role has preserved Codex session 019f64f1-a087-7e32-baba-e4bc07455f86.\n"
            "On authorized resume, use that exact session.\n"
        )
        for name, blocker, dependencies, include_resume, pending_items, dependency_section in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                current_lines = ["current:", "worker.md cfg:99"]
                previous_lines = ["previous:"]
                for task_file, _status, target, _managerat, _blocked_on in dependencies:
                    entry = f"{task_file} {target}"
                    if dependency_section == "current":
                        current_lines.append(entry)
                    else:
                        previous_lines.append(entry)
                _ = (root / "TODO.md").write_text("\n".join([*current_lines, "", *previous_lines]) + "\n", encoding="utf-8")
                _ = (root / "worker.md").write_text(
                    task_frontmatter(
                        "blocked",
                        runat="cfg:99",
                        managerat="mgr:1",
                        pending_items=pending_items,
                        blocked_on=blocker,
                    )
                    + (resume_text if include_resume is True else "This stopped record-only role preserves external id 019f64f1-a087-7e32-baba-e4bc07455f86 and says resume later.\n" if include_resume == "uuid" else "This worker is blocked on dependency repair.\n"),
                    encoding="utf-8",
                )
                for task_file, status, target, managerat, blocked_on in dependencies:
                    _ = (root / task_file).write_text(
                        task_frontmatter(status, runat=target, managerat=managerat, is_manager=status == "blocked", blocked_on=blocked_on),
                        encoding="utf-8",
                    )

                def fake_inspect(args: object, **_: object) -> Report:
                    return Report("running", ["working"]) if getattr(args, "target") == "cfg:2" else Report("not_codex", [])

                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_completed_or_circular_dependency(self) -> None:
        for name, dependency_status, dependency_blocker in (("completed", "done", ""), ("circular", "blocked", "worker.md")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
                _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="dependency.md"), encoding="utf-8")
                _ = (root / "dependency.md").write_text(
                    task_frontmatter(dependency_status, runat="cfg:2", managerat="cfg:1", is_manager=name == "circular", blocked_on=dependency_blocker),
                    encoding="utf-8",
                )
                out = StringIO()

                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

                self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_still_reports_not_codex_for_malformed_blocked_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1"), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_reports_stuck_input_for_blocked_manager_with_recorded_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"manager.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager.md cfg 1\n", encoding="utf-8")
            _ = (root / "manager.md").write_text(task_frontmatter("blocked", runat="cfg:1", is_manager=True, blocked_on="persistent submanager waiting on lower manager and human follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["typed request"], "typed request", True)), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("stuck_input: task=manager.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_persistent_role_separate_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent role waiting for followup"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=role.md", out.getvalue())

    def test_problems_only_reports_stuck_input_for_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL spec-analysis role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Continue follow-up"], "Continue follow-up", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("stuck_input: task=role.md", out.getvalue())

    def test_problems_only_reports_blocked_persistent_role_idle_explain_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            report = Report("stuck_input", ["› Explain this codebase"], "Explain this codebase", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertIn("blocked_idle: task=role.md", out.getvalue())

    def test_problems_only_stays_quiet_for_repeated_blocked_persistent_role_review_placeholders(self) -> None:
        pane = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            with patch("omo_manager.omo_codex_status.exact_tail", return_value=(True, pane)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick:
                for _ in range(2):
                    out = StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                    self.assertEqual("", out.getvalue())
            unstick.assert_not_called()

    def test_problems_only_reports_error_for_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=role.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_persistent_role_without_concrete_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent role waiting for followup"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=role.md", out.getvalue())

    def test_problems_only_reports_split_note_not_codex_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=role.md", out.getvalue())

    def test_problems_only_reports_blocked_idle_vl_supervisor_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text(
                "current:\nmanager_vl_watchdog.md wl 5\n\nprevious:\nvl_supervisor_current_7404.md vl 7\nvl_worker.md vl 9\n",
                encoding="utf-8",
            )
            _ = (root / "manager_vl_watchdog.md").write_text(task_frontmatter("running", runat="wl:5"), encoding="utf-8")
            _ = (root / "vl_supervisor_current_7404.md").write_text(
                task_frontmatter("blocked", runat="vl:7", managerat="vl:15", is_manager=True, blocked_on="persistent supervisor waiting on `vl_worker.md`; image lacks codex"),
                encoding="utf-8",
            )
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:9", managerat="vl:15", blocked_on="Docker image lacks codex; no worker output was produced"), encoding="utf-8")
            def fake_inspect(args: CodexStatusArgs) -> Report:
                return Report("running", ["working"]) if args.target == "wl:5" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=2", text)
            self.assertIn("manager-action: blocked_idle>0 inspect blocked agents", text)
            self.assertIn("blocked_idle: task=vl_supervisor_current_7404.md", text)
            self.assertIn("owner_target=vl:15", text)
            self.assertIn("blocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl_dependency", text)
            self.assertIn("image lacks codex", text)

    def test_problems_only_reports_non_human_blocked_idle_to_explicit_owner_regardless_of_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting on proof owner"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=worker.md", text)
            self.assertIn("reason=waiting on proof owner", text)
            self.assertIn("owner_target=wl:16", text)

    def test_problems_only_reports_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting on human response"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            self.assertIn("blocked_idle: task=worker.md", out.getvalue())

    def test_problems_only_skips_ready_human_pending_task_with_one_durable_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="cfg:1",
                    managerat="mgr:1",
                    blocked_on="exact durable human source for commit or retain decision",
                    pending_items=("Obtain an exact authoritative human source for commit or an explicit retain decision.",),
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_ready_human_pending_task_with_multiple_globally_blocked_goals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nmail_one_per_task.md opsmail0802:22\n", encoding="utf-8")
            _ = (root / "mail_one_per_task.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="opsmail0802:22",
                    managerat="opsmail0802:1",
                    blocked_on="human authorization for PB-specific consolidation, VL no-contact exception, and current-route replacements",
                    pending_items=(
                        "Execute the current mailbox-compression request.",
                        "Redo the mailbox compression adequately.",
                    ),
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["blocked completion evidence"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_durable_human_question_requires_correct_index_queue_and_blocker(self) -> None:
        cases = (
            ("current", "exact durable human source for approval", ("Obtain authoritative human approval.",), False, False),
            ("human pending", "exact durable human source for approval", (), False, True),
            ("human pending", "waiting on proof owner", ("Obtain authoritative human approval.",), False, False),
            ("human pending", "exact durable human source for approval", ("Obtain authoritative human approval.", "Obtain human review."), False, True),
            ("human pending", "exact durable human source for approval", ("Finish unrelated implementation.",), False, True),
            ("human pending", "human approval after upstream proof arrives", ("Obtain human approval for branding.",), False, True),
            ("human pending", "human approval for database deletion decision", ("Obtain human approval for database migration decision.",), False, True),
            ("human pending", "approval of human-readable database deletion protocol", ("Obtain approval of human-readable database deletion protocol.",), False, False),
            ("human pending", "human approval to delete production database", ("Obtain human approval to not delete production database.",), False, True),
            ("human pending", "exact durable human source for approval", ("Obtain authoritative human approval.",), True, False),
        )
        for section, blocker, items, pending_marker, expected in cases:
            with self.subTest(section=section, blocker=blocker, items=items, pending_marker=pending_marker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _ = (root / "TODO.md").write_text(f"{section}:\nreview.md cfg 1\n", encoding="utf-8")
                body = "(pending)\n" if pending_marker else ""
                _ = (root / "review.md").write_text(task_frontmatter("blocked", blocked_on=blocker, pending_items=items) + body, encoding="utf-8")
                task = parse_task_lines(root / "TODO.md")[0]
                state = scan_task_state(root / "review.md", root)
                self.assertIsNotNone(state)
                self.assertEqual(expected, is_authoritative_human_blocked_ready_task(root, task, state))

    def test_problems_only_reports_non_human_gate_phrases(self) -> None:
        for section, blocker, goal in (
            ("human pending", "non-human approval for protocol", "Obtain non-human approval for protocol."),
            ("human pending", "human-independent approval for protocol", "Obtain human-independent approval for protocol."),
            ("current", "human approval after upstream proof arrives", "Obtain human approval after upstream proof arrives."),
            ("human pending", "machine approval, not human decision", "Obtain machine approval, not human decision."),
        ):
            with self.subTest(blocker=blocker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text(f"{section}:\nreview.md cfg 1\n", encoding="utf-8")
                _ = (root / "review.md").write_text(
                    task_frontmatter("blocked", blocked_on=blocker, pending_items=(goal,)),
                    encoding="utf-8",
                )
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertIn("blocked_idle: task=review.md", out.getvalue())

    def test_durable_human_question_requires_matching_index_target(self) -> None:
        for todo_target, expected in (("", False), ("cfg:2", False), ("cfg:1.0", True)):
            with self.subTest(todo_target=todo_target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                suffix = f" {todo_target}" if todo_target else ""
                _ = (root / "TODO.md").write_text(f"human pending:\nreview.md{suffix}\n", encoding="utf-8")
                _ = (root / "review.md").write_text(
                    task_frontmatter("blocked", blocked_on="human source for commit decision", pending_items=("Obtain human source for commit decision.",)),
                    encoding="utf-8",
                )
                task = parse_task_lines(root / "TODO.md")[0]
                state = scan_task_state(root / "review.md", root)
                self.assertIsNotNone(state)
                self.assertEqual(expected, is_authoritative_human_blocked_ready_task(root, task, state))

    def test_durable_human_question_rejects_duplicate_alias_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n./review.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", blocked_on="human source for commit decision", pending_items=("Obtain human source for commit decision.",)),
                encoding="utf-8",
            )
            task = parse_task_lines(root / "TODO.md")[0]
            state = scan_task_state(root / "review.md", root)
            self.assertIsNotNone(state)
            self.assertFalse(is_authoritative_human_blocked_ready_task(root, task, state))

    def test_durable_human_question_requires_one_concrete_v2_human_blocker(self) -> None:
        blockers = (
            ("  - kind: human\n    reason: human source for commit decision", True),
            ("  - kind: human\n    reason: human attention", True),
            ("  - kind: human\n    reason: human source for commit decision\n  - kind: human\n    reason: human review for release decision", True),
            ("  - kind: task\n    task: dependency.md\n    reason: human source for commit decision", False),
        )
        for blocker_rows, expected in blockers:
            with self.subTest(blocker_rows=blocker_rows), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
                _ = (root / "review.md").write_text(
                    f"""---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000021
status: blocked
resume_status: running
runat: cfg:1
tool: codex
managerat: mgr:1
is_manager: false
blocked_on:
{blocker_rows}
pending_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000022
    text: Obtain human source for commit decision.
    blocked_on: []
    notices: []
resolved_task_items: []
---
""",
                    encoding="utf-8",
                )
                task = parse_task_lines(root / "TODO.md")[0]
                state = scan_task_state(root / "review.md", root)
                self.assertIsNotNone(state)
                self.assertEqual(expected, is_authoritative_human_blocked_ready_task(root, task, state))

    def test_authoritative_human_blocked_ready_task_rejects_live_v2_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg:1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000021
status: blocked
resume_status: running
runat: cfg:1
tool: codex
managerat: mgr:1
is_manager: false
blocked_on:
  - kind: human
    reason: authorize release
pending_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000022
    text: Release the artifact.
    blocked_on: []
    notices:
      - id: wake_019f0000-0000-7000-8000-000000000023
        kind: ready
        state: pending
        recipient_task_id: task_019f0000-0000-7000-8000-000000000021
        target_snapshot: cfg:1
        attempt_count: 0
        retry_after: null
        escalated_at: null
resolved_task_items: []
---
""",
                encoding="utf-8",
            )
            task = parse_task_lines(root / "TODO.md")[0]
            state = scan_task_state(root / "review.md", root)
            self.assertIsNotNone(state)
            self.assertFalse(is_authoritative_human_blocked_ready_task(root, task, state))

    def test_durable_human_question_does_not_hide_non_ready_faults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter(
                    "blocked",
                    blocked_on="exact durable human source for approval",
                    pending_items=("Obtain authoritative human approval.",),
                ),
                encoding="utf-8",
            )
            for fault in ("error", "missing", "not_codex", "stuck_input"):
                with self.subTest(fault=fault):
                    out = StringIO()
                    with patch("omo_manager.omo_agent_status.inspect", return_value=Report(fault, ["problem"])), redirect_stdout(out):
                        self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                    self.assertIn(f"{fault}: task=review.md", out.getvalue())

    def test_problems_only_reports_manager_ops_future_request_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops_submanager_8653.md wl 16\n", encoding="utf-8")
            _ = (root / "manager_ops_submanager_8653.md").write_text(task_frontmatter("blocked", runat="wl:16", managerat="wl:1", is_manager=True, blocked_on="waiting for future human or watcher manager-ops request"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=manager_ops_submanager_8653.md", out.getvalue())

    def test_problems_only_reports_error_for_manager_ops_future_request_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops_submanager_8653.md wl 16\n", encoding="utf-8")
            _ = (root / "manager_ops_submanager_8653.md").write_text(task_frontmatter("blocked", runat="wl:16", managerat="wl:1", is_manager=True, blocked_on="waiting for future human or watcher manager-ops request"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=manager_ops_submanager_8653.md", out.getvalue())

    def test_problems_only_reports_stuck_input_for_manager_ops_future_request_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops_submanager_8653.md wl 16\n", encoding="utf-8")
            _ = (root / "manager_ops_submanager_8653.md").write_text(task_frontmatter("blocked", runat="wl:16", managerat="wl:1", is_manager=True, blocked_on="waiting for future human or watcher manager-ops request"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue"], "Continue", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("stuck_input: task=manager_ops_submanager_8653.md", text)
            self.assertIn("unstick=disabled:blocked_idle_blocked", text)
            unstick.assert_not_called()

    def test_problems_only_reports_non_request_manager_ops_future_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops_submanager_8653.md wl 16\n", encoding="utf-8")
            _ = (root / "manager_ops_submanager_8653.md").write_text(task_frontmatter("blocked", runat="wl:16", managerat="wl:1", is_manager=True, blocked_on="waiting for future human or watcher manager-ops config"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=manager_ops_submanager_8653.md", text)
            self.assertIn("reason=waiting for future human or watcher manager-ops config", text)

    def test_problems_only_reports_human_helper_direction_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nhelper_audit_agent_9580.md hcfg 1\n", encoding="utf-8")
            _ = (root / "helper_audit_agent_9580.md").write_text(task_frontmatter("blocked", runat="hcfg:1", managerat="wl:16", blocked_on="future human/helper audit direction"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            self.assertIn("blocked_idle: task=helper_audit_agent_9580.md", out.getvalue())

    def test_problems_only_reports_error_for_human_helper_direction_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nhelper_audit_agent_9580.md hcfg 1\n", encoding="utf-8")
            _ = (root / "helper_audit_agent_9580.md").write_text(task_frontmatter("blocked", runat="hcfg:1", managerat="wl:16", blocked_on="future human/helper audit direction"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            self.assertIn("error: task=helper_audit_agent_9580.md", out.getvalue())

    def test_problems_only_reports_stuck_input_for_human_helper_direction_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nhelper_audit_agent_9580.md hcfg 1\n", encoding="utf-8")
            _ = (root / "helper_audit_agent_9580.md").write_text(task_frontmatter("blocked", runat="hcfg:1", managerat="wl:16", blocked_on="future human/helper audit direction"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue"], "Continue", True)
            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "wl:16" else report

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            text = out.getvalue()
            self.assertIn("stuck_input: task=helper_audit_agent_9580.md", text)
            self.assertIn("unstick=disabled:blocked_idle_blocked", text)
            unstick.assert_not_called()

    def test_problems_only_reports_non_audit_human_helper_direction_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="human/helper deployment direction"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=worker.md", text)
            self.assertIn("reason=human/helper deployment direction", text)

    def test_problems_only_reports_ambiguous_human_object_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting on human API key"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=worker.md", text)
            self.assertIn("reason=waiting on human API key", text)

    def test_problems_only_reports_ambiguous_human_object_direction_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting on human API key direction"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=worker.md", text)
            self.assertIn("reason=waiting on human API key direction", text)

    def test_problems_only_reports_non_hvl_not_codex_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting for a person's approval"), encoding="utf-8")
            out = StringIO()
            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "wl:17" else Report("not_codex", [])

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            self.assertIn("not_codex: task=worker.md", out.getvalue())

    def test_problems_only_skips_intentionally_stopped_human_blocked_worker(self) -> None:
        blockers = (
            "human coordination: pause the external cleanup process repeatedly removing wl:31",
            "waiting for a person's approval of the archive",
            "waiting on the human to approve the archive",
        )
        for absent_status in ("missing", "not_codex"):
            for blocker in blockers:
                with self.subTest(absent_status=absent_status, blocker=blocker), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry = root / "sessions.json"
                    _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                    _ = (root / "TODO.md").write_text("current:\narchive_old_todos.md wl:31\n", encoding="utf-8")
                    _ = (root / "archive_old_todos.md").write_text(
                        task_frontmatter(
                            "blocked",
                            runat="wl:31",
                            managerat="opsmail0802:0",
                            blocked_on=blocker,
                            pending_items=("Complete the monthly archive after human coordination.",),
                        )
                        + "(manager closed Codex agent after worker completion; tmux target `wl:31`.)\n",
                        encoding="utf-8",
                    )
                    out = StringIO()
                    with patch("omo_manager.omo_agent_status.inspect", return_value=Report(absent_status, [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                        self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                    self.assertEqual("", out.getvalue())

    def test_problems_only_skips_exact_direct_human_shutdown_pauses(self) -> None:
        paused = (
            ("cpt_strategy.md", "cptw:0", False, "task record"),
            ("memory_research_mgr.md", "wl:32", True, "pending queue"),
            ("periph_submgr_c_0729.md", "wl:9", True, "pending queue"),
        )
        for absent_status in ("missing", "not_codex"):
            for task_file, target, is_manager, preserved in paused:
                with self.subTest(absent_status=absent_status, task_file=task_file), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry = root / "sessions.json"
                    _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                    _ = (root / "TODO.md").write_text(f"current:\n{task_file} {target}\n", encoding="utf-8")
                    blocker = f"paused by direct human shutdown instruction routed to periph_misc_mgr.md; non-human pane {target} closed and {preserved} preserved for explicit resume"
                    _ = (root / task_file).write_text(
                        task_frontmatter("blocked", runat=target, managerat="wl:3", is_manager=is_manager, blocked_on=blocker)
                        + f"(manager closed Codex agent after human shutdown; tmux target `{target}`.)\n",
                        encoding="utf-8",
                    )
                    out = StringIO()
                    with patch("omo_manager.omo_agent_status.inspect", return_value=Report(absent_status, [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                        self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                    self.assertEqual("", out.getvalue())

    def test_problems_only_retains_unverified_direct_human_shutdown_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md wl:32\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="wl:32",
                    managerat="wl:3",
                    is_manager=True,
                    blocked_on="paused by direct human shutdown instruction routed to periph_misc_mgr.md; non-human pane wl:32 closed and pending queue preserved for explicit resume",
                )
                + "(manager closed Codex agent after human shutdown; tmux target `wl:31`.)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("missing: task=worker.md", out.getvalue())

    def test_problems_only_retains_reused_direct_human_shutdown_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\ncpt_strategy.md cptw:0\n", encoding="utf-8")
            blocker = "paused by direct human shutdown instruction routed to periph_misc_mgr.md; non-human pane cptw:0 closed and task record preserved for explicit resume"
            _ = (root / "cpt_strategy.md").write_text(
                task_frontmatter("blocked", runat="cptw:0", managerat="wl:3", blocked_on=blocker)
                + "(manager closed Codex agent after human shutdown; tmux target `cptw:0`.)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=True), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=cpt_strategy.md", out.getvalue())

    def test_problems_only_skips_source_bound_human_token_quota_pause_under_low_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            blocker = "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("blocked", runat="vl_build_mgr:0", managerat="vlprograms:0", is_manager=True, blocked_on=blocker),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_source_bound_human_token_quota_pause_with_visible_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            blocker = "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("blocked", runat="vl_build_mgr:0", managerat="vlprograms:0", is_manager=True, blocked_on=blocker),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", ["shell prompt"])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_source_bound_human_token_quota_pause_with_live_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            blocker = "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("blocked", runat="vl_build_mgr:0", managerat="vlprograms:0", is_manager=True, blocked_on=blocker),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=True), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_retains_unbound_human_token_quota_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl_build_mgr:0",
                    managerat="vlprograms:0",
                    is_manager=True,
                    blocked_on="human token-quota pause: keep all VL paths closed until explicit resume",
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", ["shell prompt"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=vl_build_mgr.md", out.getvalue())

    def test_problems_only_retains_out_of_scope_human_token_quota_pause(self) -> None:
        cases = (
            ("current", "vl_build_mgr.md", True, "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
            ("human pending", "vl_build_mgr.md", True, "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
            ("low priority", "other_vl_mgr.md", True, "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
            ("low priority", "vl_build_mgr.md", True, "human token-quota pause from 202607/manager_mail/85c5dff58359-728.txt: keep all VL paths closed until explicit resume"),
            ("low priority", "vl_build_mgr.md", True, "Human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
            ("low priority", "vl_build_mgr.md", True, "human token-quota pause from manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
            ("low priority", "vl_build_mgr.md", False, "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"),
        )
        for section, task_file, is_manager, blocker in cases:
            with self.subTest(section=section, task_file=task_file, is_manager=is_manager, blocker=blocker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text(f"{section}:\n{task_file} vl_build_mgr:0\n", encoding="utf-8")
                _ = (root / task_file).write_text(
                    task_frontmatter("blocked", runat="vl_build_mgr:0", managerat="vlprograms:0", is_manager=is_manager, blocked_on=blocker),
                    encoding="utf-8",
                )
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertIn(f"not_codex: task={task_file}", out.getvalue())

    def test_problems_only_retains_source_bound_quota_pause_without_frontmatter_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md hvl:1\n", encoding="utf-8")
            blocker = "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("blocked", runat="retired", managerat="vlprograms:0", is_manager=True, blocked_on=blocker),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=vl_build_mgr.md", out.getvalue())

    def test_problems_only_retains_source_bound_quota_pause_with_human_frontmatter_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            blocker = "human token-quota pause from 202607/manager_mail/85c5dff58359-729.txt: keep all VL paths closed until explicit resume"
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("blocked", runat="hvl:1", managerat="vlprograms:0", is_manager=True, blocked_on=blocker),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=vl_build_mgr.md", out.getvalue())

    def test_problems_only_retains_running_vl_build_manager_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_build_mgr.md vl_build_mgr:0\n", encoding="utf-8")
            _ = (root / "vl_build_mgr.md").write_text(
                task_frontmatter("running", runat="vl_build_mgr:0", managerat="vlprograms:0", is_manager=True),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("missing: task=vl_build_mgr.md", out.getvalue())

    def test_problems_only_retains_incomplete_or_live_required_stopped_worker_evidence(self) -> None:
        cases = {
            "no_open_queue": ("blocked", "human coordination with the archive owner", (), True, False, Report("missing", [])),
            "no_closure_record": ("blocked", "human coordination with the archive owner", ("Finish archive.",), False, False, Report("missing", [])),
            "exact_human": ("blocked", "human", ("Finish archive.",), True, False, Report("missing", [])),
            "bare_human_coordination": ("blocked", "human coordination", ("Finish archive.",), True, False, Report("missing", [])),
            "bare_human_approval": ("blocked", "human approval", ("Finish archive.",), True, False, Report("missing", [])),
            "punctuated_human_approval": ("blocked", "human approval.", ("Finish archive.",), True, False, Report("missing", [])),
            "punctuated_wait": ("blocked", "waiting on human approval.", ("Finish archive.",), True, False, Report("missing", [])),
            "punctuated_truncated_action": ("blocked", "waiting on human to.", ("Finish archive.",), True, False, Report("missing", [])),
            "malformed_blocker": ("blocked", "human-readable output unavailable", ("Finish archive.",), True, False, Report("missing", [])),
            "visible_not_codex": ("blocked", "human coordination with the archive owner", ("Finish archive.",), True, False, Report("not_codex", ["shell prompt"])),
            "target_still_exists": ("blocked", "human coordination with the archive owner", ("Finish archive.",), True, True, Report("not_codex", [])),
            "running": ("running", "", ("Finish archive.",), True, False, Report("missing", [])),
            "unblocked_long_running": ("long_running", "persistent role", ("Finish archive.",), True, False, Report("missing", [])),
        }
        for name, (status, blocker, pending_items, closed, target_exists, report) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
                closure = "(manager closed Codex agent after worker completion; tmux target `cfg:1`.)\n" if closed else ""
                _ = (root / "worker.md").write_text(
                    task_frontmatter(status, runat="cfg:1", blocked_on=blocker, pending_items=pending_items) + closure,
                    encoding="utf-8",
                )
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=target_exists), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertIn(f"{report.status}: task=worker.md", out.getvalue())

    def test_problems_only_reports_ready_hvl_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\ndocs.md hvl 5\n", encoding="utf-8")
            _ = (root / "docs.md").write_text(
                task_frontmatter("blocked", runat="hvl:5", managerat="vl:5", blocked_on="waiting for next human guidance-doc review input"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=docs.md", out.getvalue())

    def test_problems_only_reports_ready_stage_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nstages.md hvl 6\n", encoding="utf-8")
            _ = (root / "stages.md").write_text(
                task_frontmatter("blocked", runat="hvl:6", managerat="vl:9", blocked_on="waiting for next human stage-review input"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=stages.md", out.getvalue())

    def test_problems_only_reports_ready_non_hvl_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 5\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(
                task_frontmatter("blocked", runat="cfg:5", managerat="vl:9", blocked_on="human-facing review waits in the live pane"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=worker.md", out.getvalue())

    def test_problems_only_reports_hvl_human_readable_review_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nhvl.md hvl 5\n", encoding="utf-8")
            _ = (root / "hvl.md").write_text(
                task_frontmatter("blocked", runat="hvl:5", managerat="vl:9", blocked_on="waiting for a human-readable review summary"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=hvl.md", out.getvalue())

    def test_problems_only_reports_hvl_indirect_review_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_hvl_mgr_11264.md vl 5\n", encoding="utf-8")
            _ = (root / "vl_hvl_mgr_11264.md").write_text(
                task_frontmatter("blocked", runat="vl:5", managerat="vl:9", blocked_on="waiting for CI; a human will receive the review summary"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_hvl_mgr_11264.md", out.getvalue())

    def test_problems_only_reports_manager_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_hvl_mgr_11264.md vl 5\n", encoding="utf-8")
            _ = (root / "vl_hvl_mgr_11264.md").write_text(
                task_frontmatter("blocked", runat="vl:5", managerat="vl:9", blocked_on="human-facing review waits in ml_hvl_docs2_10734.md and ml_hvl_restart_10848.md", is_manager=True),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_hvl_mgr_11264.md", out.getvalue())

    def test_problems_only_reports_hvl_human_discussion_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_fail_review_9758.md hvl 2\n", encoding="utf-8")
            _ = (root / "vl_fail_review_9758.md").write_text(
                task_frontmatter("blocked", runat="hvl:2", managerat="vl:1", blocked_on="human-facing interactive 9579 failure/process review is waiting on direct human discussion in hvl:2"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_fail_review_9758.md", out.getvalue())

    def test_problems_only_reports_vl_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_late_walk_8951.md vl 17\n", encoding="utf-8")
            _ = (root / "vl_late_walk_8951.md").write_text(
                task_frontmatter("blocked", runat="vl:17", managerat="vl:1", blocked_on="human-pending after organized limitation explanation email"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_late_walk_8951.md", out.getvalue())

    def test_problems_only_skips_exact_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", managerat="mgr:1", blocked_on="human"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_closed_pane_for_exact_human_pending_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nhandbook_paper.md wl:4\n", encoding="utf-8")
            _ = (root / "handbook_paper.md").write_text(
                task_frontmatter("blocked", runat="wl:4", managerat="wl:9", blocked_on="human", pending_items=("Human review: decide whether to advance the controlled benchmark study.",))
                + "(manager closed Codex agent after worker and evaluator completion; tmux target `wl:4`.)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_unverified_closed_pane_for_exact_human_pending_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md wl:4\n", encoding="utf-8")
            _ = (root / "review.md").write_text(task_frontmatter("blocked", runat="wl:4", managerat="wl:9", blocked_on="human"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=review.md", out.getvalue())

    def test_problems_only_reports_closed_pane_for_exact_human_wait_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nreview.md wl:4\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="wl:4", managerat="wl:9", blocked_on="human")
                + "(manager closed Codex agent after worker completion; tmux target `wl:4`.)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=review.md", out.getvalue())

    def test_problems_only_reports_observed_faults_for_exact_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", managerat="mgr:1", blocked_on="human")
                + "(manager closed Codex agent after worker completion; tmux target `cfg:1`.)\n",
                encoding="utf-8",
            )
            for fault in ("error", "not_codex", "stuck_input"):
                with self.subTest(fault=fault):
                    out = StringIO()
                    with patch("omo_manager.omo_agent_status.inspect", return_value=Report(fault, ["problem"])), redirect_stdout(out):
                        self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                    self.assertIn(f"{fault}: task=review.md", out.getvalue())

    def test_problems_only_reports_close_non_human_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md cfg 1\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="cfg:1", managerat="mgr:1", blocked_on="human-readable output unavailable"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=review.md", out.getvalue())

    def test_problems_only_reports_hvl_human_approval_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_mini_trials_9748.md hvl 1\n", encoding="utf-8")
            _ = (root / "vl_mini_trials_9748.md").write_text(
                task_frontmatter("blocked", runat="hvl:1", managerat="vl:1", blocked_on="human approval of an exact GPT-5.4-mini replay packet"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_mini_trials_9748.md", out.getvalue())

    def test_problems_only_reports_human_integration_wait_outside_human_pending(self) -> None:
        for blocker in (
            "human decision whether to integrate isolated reviewed branch into the live repository",
            "human authorization to integrate reviewed isolated commits, then approve migration and controlled watcher startup",
        ):
            with self.subTest(blocker=blocker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nbidirectional_blocking.md hcfg 2\n", encoding="utf-8")
                _ = (root / "bidirectional_blocking.md").write_text(
                    task_frontmatter("blocked", runat="hcfg:2", managerat="wl:1", blocked_on=blocker),
                    encoding="utf-8",
                )
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertIn("blocked_idle: task=bidirectional_blocking.md", out.getvalue())

    def test_problems_only_reports_hvl_review_waiting_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_hvl_mgr_11264.md vl 5\n", encoding="utf-8")
            _ = (root / "vl_hvl_mgr_11264.md").write_text(
                task_frontmatter("blocked", runat="vl:5", managerat="vl:9", blocked_on="human-facing review waiting for the human"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_hvl_mgr_11264.md", out.getvalue())

    def test_problems_only_reports_hvl_review_case_variant_outside_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_hvl_mgr_11264.md vl 5\n", encoding="utf-8")
            _ = (root / "vl_hvl_mgr_11264.md").write_text(
                task_frontmatter("blocked", runat="vl:5", managerat="vl:9", blocked_on="Human-facing review waits in the live panes"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_hvl_mgr_11264.md", out.getvalue())

    def test_problems_only_reports_error_for_human_review_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\ndocs.md hvl 5\n", encoding="utf-8")
            _ = (root / "docs.md").write_text(
                task_frontmatter("blocked", runat="hvl:5", managerat="vl:5", blocked_on="waiting for next human guidance-doc review input"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=docs.md", out.getvalue())

    def test_problems_only_reports_error_for_non_hvl_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 5\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:5", managerat="wl:16", blocked_on="waiting on human review"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=worker.md", out.getvalue())

    def test_problems_only_reports_stuck_input_for_non_hvl_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 5\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:5", managerat="wl:16", blocked_on="waiting for human follow-up"), encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue"], "Continue", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("stuck_input: task=worker.md", text)
            self.assertIn("unstick=disabled:blocked_idle_blocked", text)
            unstick.assert_not_called()

    def test_problems_only_does_not_count_running_blocked_vl_target_as_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl 9\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:9", blocked_on="image lacks codex"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_includes_worker_owned_by_another_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl 15\nvl_worker.md vl 1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter("running", runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:1", managerat="vl:15", blocked_on="waiting on proof owner"), encoding="utf-8")
            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:15" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16.0"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=1", text)
            self.assertIn("blocked_idle: task=vl_worker.md evidence=target=vl:1 role=blocked_idle_vl task_status=blocked", text)
            self.assertIn("owner_target=vl:15", text)

    def test_problems_only_includes_bulleted_worker_owned_by_another_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n- `vl_submanager_current_8653.md` (`vl:15`)\n- `vl_worker.md` (`vl:1`)\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter("running", runat="vl:15", managerat="wl:16.0", is_manager=True), encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:1", managerat="vl:15", blocked_on="waiting on proof owner"), encoding="utf-8")
            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:15" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16.0"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=1", text)
            self.assertIn("blocked_idle: task=vl_worker.md evidence=target=vl:1 role=blocked_idle_vl task_status=blocked", text)
            self.assertIn("owner_target=vl:15", text)

    def test_vl_submanager_problems_only_includes_vl_worker_without_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl 15\nvl_worker.md vl 1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter("running", runat="vl:99", managerat="vl:15", is_manager=True), encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:1", managerat="vl:15", blocked_on="waiting on proof owner"), encoding="utf-8")
            def fake_inspect(args: object, **_: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:15" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "vl:15.0"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=1", text)
            self.assertIn("blocked_idle: task=vl_worker.md", text)

    def test_status_summary_reports_blocked_idle_vl_rows_and_untracked_running_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_untracked.md vl 8\n\nprevious:\nvl_supervisor_current_7404.md vl 7\n", encoding="utf-8")
            _ = (root / "vl_untracked.md").write_text(task_frontmatter("running", runat="vl:8"), encoding="utf-8")
            _ = (root / "vl_supervisor_current_7404.md").write_text(task_frontmatter("blocked", runat="vl:7", managerat="vl:15", is_manager=True, blocked_on="image lacks codex"), encoding="utf-8")
            def fake_inspect(args: CodexStatusArgs) -> Report:
                return Report("running", ["working"]) if args.target == "vl:8" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry)]))
            text = out.getvalue()
            self.assertIn("agent-status: not_codex=0 running=1 blocked_idle=1", text)
            self.assertIn("running: task=vl_untracked.md", text)
            self.assertIn("blocked_idle: task=vl_supervisor_current_7404.md", text)

    def test_problems_only_does_not_duplicate_blocked_idle_target_as_unmanaged_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl 9\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:9", blocked_on="image lacks codex"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["Selected model is at capacity"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("error: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked", text)
            self.assertIn("idle_status=error", text)
            self.assertNotIn("role=todo_unmanaged", text)

    def test_blocked_vl_target_reports_real_stuck_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text(
                "current:\nvl_submanager_current_8653.md vl:15\nvl_dirty_audit.md vl:blocked\nvl_followup.md vl:20\n",
                encoding="utf-8",
            )
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter("running", runat="vl:99", managerat="vl:15", is_manager=True), encoding="utf-8")
            _ = (root / "vl_dirty_audit.md").write_text(task_frontmatter("blocked", runat="vl:20", managerat="vl:15", blocked_on="waiting on proof owner"), encoding="utf-8")
            _ = (root / "vl_followup.md").write_text(task_frontmatter("done", runat="vl:20", managerat="vl:15"), encoding="utf-8")
            report = Report("stuck_input", ["Reported privately.", "› [Pasted Content 1024 chars][Pasted Content 1024 chars] #2", "  (pending)"], "[Pasted Content 1024 chars][Pasted Content 1024 chars] #2\n  (pending)", True)
            def fake_inspect(args: object, **_: object) -> Report:
                return report if getattr(args, "target") == "vl:20" else Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "vl:15"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertIn("stuck_input: task=vl_dirty_audit.md evidence=target=vl:20 role=blocked_idle_vl task_status=blocked", text)
            self.assertIn("idle_status=stuck_input", text)
            self.assertIn("unstick=disabled:blocked_idle_vl_blocked", text)
            self.assertNotIn("blocked_idle: task=vl_dirty_audit.md", text)
            unstick.assert_not_called()

    def test_blocked_vl_target_does_not_duplicate_current_target_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_current.md vl:20\nvl_old.md vl:blocked\n", encoding="utf-8")
            _ = (root / "vl_current.md").write_text(task_frontmatter("running", runat="vl:20"), encoding="utf-8")
            _ = (root / "vl_old.md").write_text(task_frontmatter("blocked", runat="vl:20", blocked_on="old blocker"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Continue task"], "Continue task", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertEqual(1, text.count("stuck_input: task="))
            self.assertIn("stuck_input: task=vl_current.md", text)
            self.assertNotIn("vl_old.md", text)

    def test_blocked_persistent_vl_placeholder_reports_blocked_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_role.md vl:20\n", encoding="utf-8")
            _ = (root / "vl_role.md").write_text(task_frontmatter("blocked", runat="vl:20", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Explain this codebase"], "Explain this codebase", True)), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=vl_role.md", out.getvalue())

    def test_blocked_persistent_vl_real_stuck_input_reports_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_role.md vl:20\n", encoding="utf-8")
            _ = (root / "vl_role.md").write_text(task_frontmatter("blocked", runat="vl:20", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Continue task"], "Continue task", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertEqual(1, text.count("stuck_input: task=vl_role.md"))
            self.assertIn("unstick=disabled:blocked_idle_vl_blocked", text)
            unstick.assert_not_called()

    def test_targetless_blocked_persistent_vl_is_not_idle_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_role.md\n", encoding="utf-8")
            _ = (root / "vl_role.md").write_text(
                "(blocked: persistent VL supervisor role waiting for follow-up)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_blocked_idle_deduplicates_same_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_one.md vl 9\nvl_two.md vl 9\n", encoding="utf-8")
            _ = (root / "vl_one.md").write_text(task_frontmatter("blocked", runat="vl:9", blocked_on="first blocker"), encoding="utf-8")
            _ = (root / "vl_two.md").write_text(task_frontmatter("blocked", runat="vl:9.0", blocked_on="second blocker"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=1", text)
            self.assertEqual(1, text.count("blocked_idle: task="))

    def test_exit_code_if_active_ignores_blocked_idle_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl 9\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text(task_frontmatter("blocked", runat="vl:9", blocked_on="image lacks codex"), encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))

    def test_exit_code_if_active_ignores_quiet_blocked_ready_running_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"blocked.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nblocked.md cfg 1\n", encoding="utf-8")
            _ = (root / "blocked.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="waiting on human"), encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(StringIO()):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))

    def test_problems_only_reports_registry_unmanaged_capacity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"blocked.md","tmux_target":"vl:7.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nblocked.md vl 7\n", encoding="utf-8")
            _ = (root / "blocked.md").write_text(task_frontmatter("blocked", runat="vl:7", blocked_on="blocked with no reason in latest status line"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["Selected model is at capacity. Please try a different model."])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=blocked.md evidence=target=vl:7 role=blocked_idle task_status=blocked", text)

    def test_problems_only_reports_cursor_usage_limit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md amhrev 4\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("running", runat="amhrev:4", managerat="wl:1", pending_items=("finish review",)), encoding="utf-8")
            out = StringIO()
            report = Report(
                "error",
                [
                    "Error: Increase limits for faster responses",
                    "You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.",
                ],
            )

            def fake_inspect(args: object, **_: object) -> Report:
                return Report("ready", ["manager ready"]) if getattr(args, "target") == "wl:1" else report

            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:1"]))

            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=worker.md evidence=target=amhrev:4 task_status=running", text)
            self.assertIn("Increase limits for faster responses", text)
            self.assertIn("out of usage", text)

    def test_problems_only_reports_same_task_stale_registry_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"vl:1.0","started_at_s":1},{"task_file":"active.md","tmux_target":"vl:2.0","started_at_s":2}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:2"), encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:1.0":
                    return Report("error", ["Selected model is at capacity. Please try a different model."])
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("error: task=active.md evidence=target=vl:1.0 role=registry_unmanaged task_status=running", text)
            self.assertNotIn("target=vl:2.0 role=registry_unmanaged", text)

    def test_problems_only_auto_unsticks_registry_unmanaged_done_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"old.md","tmux_target":"vl:23.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nold.md vl 23\n", encoding="utf-8")
            _ = (root / "old.md").write_text(task_frontmatter("done", runat="vl:23"), encoding="utf-8")
            report = Report("stuck_input", ["› pasted manager prompt"], "pasted manager prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("vl:23.0", report)
            text = out.getvalue()
            self.assertIn("stuck_input: task=old.md evidence=target=vl:23.0 role=registry_unmanaged task_status=done", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("done-stale: task=old.md", text)

    def test_problems_only_auto_unsticks_registry_unmanaged_running_stale_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"active.md","tmux_target":"vl:1.0","started_at_s":1}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:2"), encoding="utf-8")
            report = Report("stuck_input", ["› stale prompt"], "stale prompt", True)

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:1.0":
                    return report
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("vl:1.0", report)
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=vl:1.0 role=registry_unmanaged task_status=running", text)
            self.assertIn("unstick=sent_enter", text)

    def test_problems_only_auto_unsticks_registry_pane_sibling_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"active.md","tmux_target":"vl:2.1","started_at_s":1}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:2"), encoding="utf-8")
            report = Report("stuck_input", ["› stale pane prompt"], "stale pane prompt", True)

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:2.1":
                    return report
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("vl:2.1", report)
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=vl:2.1 role=registry_unmanaged task_status=running", text)
            self.assertIn("unstick=sent_enter", text)

    def test_problems_only_auto_unsticks_todo_unmanaged_done_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md vl 53\n", encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter("done", runat="vl:53"), encoding="utf-8")
            report = Report("stuck_input", ["› pasted prompt"], "pasted prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("vl:53", report)
            text = out.getvalue()
            self.assertIn("stuck_input: task=stale.md evidence=target=vl:53 role=todo_unmanaged task_status=done", text)
            self.assertIn("unstick=sent_enter", text)

    def test_problems_only_reports_todo_unmanaged_capacity_error_without_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md vl 9\n", encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter("done", runat="vl:9"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["Selected model is at capacity. Please try a different model."])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=stale.md evidence=target=vl:9 role=todo_unmanaged task_status=done", text)

    def test_problems_only_reports_ready_stale_done_registry_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"old.md","tmux_target":"vl:23.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nold.md vl 23\n", encoding="utf-8")
            _ = (root / "old.md").write_text(task_frontmatter("done", runat="vl:23"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1 done-registry-stale=1", text)
            self.assertIn("ready: task=old.md evidence=target=vl:23.0 role=registry_unmanaged task_status=done", text)
            self.assertIn("done-stale: task=old.md", text)

    def test_problems_only_reports_ready_stale_running_registry_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"vl:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:2"), encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                if getattr(args, "target") == "vl:1.0":
                    return Report("ready", ["idle"])
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=active.md evidence=target=vl:1.0 role=registry_unmanaged task_status=running", text)

    def test_problems_only_does_not_attribute_reused_target_to_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "human pending:\nactive.md wl 3\n\nprevious:\ncompleted.md wl 3\n"
            _ = todo.write_text(todo_text, encoding="utf-8")
            active = root / "active.md"
            _ = active.write_text(
                task_frontmatter("blocked", runat="wl:3", blocked_on="human decision on the required guarantee"),
                encoding="utf-8",
            )
            completed = root / "completed.md"
            completed_text = task_frontmatter("done", runat="wl:3")
            _ = completed.write_text(completed_text, encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["later task output"])) as inspect, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertEqual("", out.getvalue())
            self.assertEqual(["wl:3"], [call.args[0].target for call in inspect.call_args_list])
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(completed_text, completed.read_text(encoding="utf-8"))

    def test_problems_only_ignores_historical_runat_for_targetless_human_pending_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="wl:31", blocked_on="waiting on the human's decision about the release"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])) as inspect, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertEqual("", out.getvalue())
            inspect.assert_not_called()

    def test_problems_only_does_not_quiet_legacy_target_bound_low_priority_custody(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"parked.md","tmux_target":"vl:31.0","started_at_s":1}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("low priority:\nparked.md\n", encoding="utf-8")
            _ = (root / "parked.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl:31",
                    blocked_on="paused by direct human decision",
                    pending_items=("resume this work only when authorized",),
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with (
                patch("omo_manager.omo_agent_status.target_resolution_state", return_value=False),
                patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False),
                patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])) as inspect,
                patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]),
                redirect_stdout(out),
            ):
                self.assertEqual(
                    3,
                    main(
                        [
                            "--root",
                            str(root),
                            "--registry",
                            str(registry),
                            "--problems-only",
                            "--no-auto-unstick",
                        ]
                    ),
                )
            self.assertIn("missing: task=parked.md", out.getvalue())
            inspect.assert_called()

    def test_targetless_low_priority_nonhuman_dependency_is_not_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            registry.write_text('{"sessions":[]}', encoding="utf-8")
            (root / "TODO.md").write_text("low priority:\nparked.md\n", encoding="utf-8")
            (root / "parked.md").write_text(
                task_frontmatter("blocked", runat="vl:31", blocked_on="non-human dependency", pending_items=("resume work",)),
                encoding="utf-8",
            )
            out = StringIO()
            with (
                patch("omo_manager.omo_agent_status.target_resolution_state", return_value=False),
                patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])),
                redirect_stdout(out),
            ):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
            self.assertIn("missing: task=parked.md", out.getvalue())

    def test_targetless_low_priority_custody_requires_all_boundaries(self) -> None:
        for case in (
            "linked",
            "live",
            "empty queue",
            "manager",
            "human",
            "duplicate",
            "suffix",
            "annotation",
            "extra task ref",
            "unknown heading",
            "duplicate header",
            "noncanonical path",
            "header whitespace",
            "row whitespace",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                runat = "hvl:31" if case == "human" else "vl:31"
                row = {
                    "linked": "parked.md vl:31",
                    "suffix": "parked.md note",
                    "annotation": "parked.md (paused?)",
                    "extra task ref": "parked.md other.md",
                    "noncanonical path": "./parked.md",
                }.get(case, "parked.md")
                todo = f"low priority:\n{row}\n"
                if case == "duplicate":
                    todo += "previous:\nparked.md vl:31\n"
                elif case == "unknown heading":
                    todo = "low priority:\nnotes:\nparked.md\n"
                elif case == "duplicate header":
                    todo = "low priority:\nlow priority:\nparked.md\n"
                elif case == "header whitespace":
                    todo = " low priority:\nparked.md\n"
                elif case == "row whitespace":
                    todo = "low priority:\n parked.md \n"
                _ = (root / "TODO.md").write_text(todo, encoding="utf-8")
                _ = (root / "parked.md").write_text(
                    task_frontmatter(
                        "blocked",
                        runat=runat,
                        is_manager=case == "manager",
                        blocked_on="paused",
                        pending_items=() if case == "empty queue" else ("open work",),
                    ),
                    encoding="utf-8",
                )
                out = StringIO()
                with (
                    patch("omo_manager.omo_agent_status.target_resolution_state", return_value=case == "live"),
                    patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=case == "live"),
                    patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])),
                    patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]),
                    redirect_stdout(out),
                ):
                    self.assertEqual(
                        3,
                        main(
                            [
                                "--root",
                                str(root),
                                "--registry",
                                str(registry),
                                "--problems-only",
                                "--no-auto-unstick",
                            ]
                        ),
                    )
                self.assertIn(
                    "missing: task=./parked.md" if case == "noncanonical path" else "missing: task=parked.md",
                    out.getvalue(),
                )

    def test_targetless_low_priority_custody_keeps_alerts_when_tmux_state_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nparked.md\n", encoding="utf-8")
            _ = (root / "parked.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="vl:31",
                    blocked_on="paused",
                    pending_items=("open work",),
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with (
                patch("omo_manager.omo_agent_status.target_resolution_state", return_value=None),
                patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False),
                patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])),
                patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]),
                redirect_stdout(out),
            ):
                self.assertEqual(
                    3,
                    main(
                        [
                            "--root",
                            str(root),
                            "--registry",
                            str(registry),
                            "--problems-only",
                            "--no-auto-unstick",
                        ]
                    ),
                )
            self.assertIn("missing: task=parked.md", out.getvalue())

    def write_reattested_custody_case(self, root: Path, state_dir: Path) -> tuple[TaskLine, TaskState, Path]:
        source = "Subject: halt\n\nstop this owner and preserve its queued work\n"
        old_locator = "manager_mail/halt.txt:3-3"
        locator = "202607/manager_mail/halt.txt:3-3"
        archive = root / "202607"
        (archive / "manager_mail").mkdir(parents=True)
        (archive / "manager_mail/halt.txt").write_text(source, encoding="utf-8")
        envelope_text = (
            f'<human_instruction authoritative="true" source="{locator}">\n'
            "stop this owner and preserve its queued work\n"
            "</human_instruction>\n"
            f"<agent_message>route under {locator}</agent_message>\n"
        )
        (archive / "halt_envelope.md").write_text(envelope_text, encoding="utf-8")
        session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
        old_task = task_frontmatter("blocked", runat="vl:31", blocked_on="paused by direct human decision", pending_items=("open work",))
        old_task += f"authority: {old_locator}\n(manager closed Codex agent 08-25 12:15 PDT; tmux target `vl:31`; session_id: `{session_id}`.)\n"
        task_text = old_task.replace(old_locator, locator)
        task_path = root / "parked.md"
        task_path.write_text(task_text, encoding="utf-8")
        todo_text = "low priority:\nparked.md\n"
        (root / "TODO.md").write_text(todo_text, encoding="utf-8")
        old_envelope = envelope_text.replace(locator, old_locator)
        prior = {
            "version": "v1.0.0", "operation": "park-unlinked", "task": "parked.md", "target": "vl:31",
            "pane_id": "", "task_sha256": hashlib.sha256(old_task.encode()).hexdigest(),
            "initial_todo_sha256": "1" * 64, "close_proof_commitment": "0" * 64,
            "prior_close_session_id": session_id, "authority_source": old_locator,
            "authority_sha256": hashlib.sha256(source.encode()).hexdigest(), "authority_envelope": "halt_envelope.md",
            "authority_envelope_sha256": hashlib.sha256(old_envelope.encode()).hexdigest(), "state": "complete",
        }
        prior_text = yaml.safe_dump(prior, sort_keys=True)
        reattestation = {
            "version": "v2.0.0", "operation": "park-unlinked-re-attestation", "state": "complete",
            "task": "parked.md", "target": "vl:31", "task_sha256": hashlib.sha256(task_text.encode()).hexdigest(),
            "todo_sha256": hashlib.sha256(todo_text.encode()).hexdigest(), "authority_source": locator,
            "authority_sha256": hashlib.sha256(source.encode()).hexdigest(), "authority_envelope": "202607/halt_envelope.md",
            "authority_envelope_sha256": hashlib.sha256(envelope_text.encode()).hexdigest(),
            "prior_complete_receipt_sha256": hashlib.sha256(prior_text.encode()).hexdigest(),
            "prior_complete_receipt": prior,
        }
        reattestation_text = yaml.safe_dump(reattestation, sort_keys=True)
        receipt = {
            "version": "v3.0.0", "operation": "park-unlinked-custody-re-attestation", "state": "complete",
            "task": "parked.md", "target": "vl:31", "task_sha256": hashlib.sha256(task_text.encode()).hexdigest(),
            "custody_sha256": hashlib.sha256(b"low priority:\nparked.md\n").hexdigest(), "authority_source": locator,
            "authority_sha256": hashlib.sha256(source.encode()).hexdigest(), "authority_envelope": "202607/halt_envelope.md",
            "authority_envelope_sha256": hashlib.sha256(envelope_text.encode()).hexdigest(),
            "prior_complete_receipt_sha256": hashlib.sha256(reattestation_text.encode()).hexdigest(),
            "prior_complete_receipt": reattestation,
        }
        receipt_dir = state_dir / "park-unlinked"
        receipt_dir.mkdir(parents=True, mode=0o700)
        state_dir.chmod(0o700)
        receipt_dir.chmod(0o700)
        receipt_path = receipt_dir / "parked.yaml"
        receipt_path.write_text(yaml.safe_dump(receipt, sort_keys=True), encoding="utf-8")
        receipt_path.chmod(0o600)
        task = TaskLine("parked.md", "todo:low priority", "parked.md", "", None)
        state = TaskState("blocked", "vl:31", None, reason="paused by direct human decision")
        return task, state, receipt_path

    def test_targetless_low_priority_custody_accepts_only_authenticated_reattestation(self) -> None:
        for case in (
            "valid",
            "unrelated TODO change",
            "receipt drift",
            "receipt todo digest drift",
            "receipt symlink",
            "authority drift",
            "task drift",
            "TODO custody target",
            "TODO custody duplicate",
            "TODO custody section",
            "duplicate owner",
            "unknown target",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work"
                root.mkdir()
                state_dir = Path(tmp) / "state"
                task, state, receipt = self.write_reattested_custody_case(root, state_dir)
                if case == "receipt drift":
                    receipt.write_text(receipt.read_text().replace("state: complete", "state: prepared", 1), encoding="utf-8")
                elif case == "receipt todo digest drift":
                    record = yaml.safe_load(receipt.read_text(encoding="utf-8"))
                    record["prior_complete_receipt"]["todo_sha256"] = "0" * 64
                    receipt.write_text(yaml.safe_dump(record, sort_keys=True), encoding="utf-8")
                elif case == "receipt symlink":
                    copy = state_dir / "receipt-copy.yaml"
                    copy.write_text(receipt.read_text(), encoding="utf-8")
                    copy.chmod(0o600)
                    receipt.unlink()
                    receipt.symlink_to(copy)
                elif case == "authority drift":
                    with (root / "202607/manager_mail/halt.txt").open("a", encoding="utf-8") as output:
                        output.write("drift\n")
                elif case == "task drift":
                    with (root / "parked.md").open("a", encoding="utf-8") as output:
                        output.write("drift\n")
                elif case == "unrelated TODO change":
                    with (root / "TODO.md").open("a", encoding="utf-8") as output:
                        output.write("other.md\n")
                elif case == "TODO custody target":
                    (root / "TODO.md").write_text("low priority:\nparked.md vl:31\n", encoding="utf-8")
                elif case == "TODO custody duplicate":
                    (root / "TODO.md").write_text("low priority:\nparked.md\nparked.md\n", encoding="utf-8")
                elif case == "TODO custody section":
                    (root / "TODO.md").write_text("current:\nparked.md\nlow priority:\n", encoding="utf-8")
                elif case == "duplicate owner":
                    (root / "duplicate.md").write_text(task_frontmatter("blocked", runat="vl:31", blocked_on="paused", pending_items=("work",)), encoding="utf-8")
                resolution = None if case == "unknown target" else False
                with (
                    patch.dict("omo_manager.omo_agent_status.LOCAL_ENV", {"OMO_MANAGER_STATE_DIR": str(state_dir)}),
                    patch("omo_manager.omo_agent_status.target_resolution_state", return_value=resolution),
                ):
                    self.assertEqual(case in {"valid", "unrelated TODO change"}, is_targetless_low_priority_custody(root, task, state))

    def test_target_resolution_state_rejects_failed_malformed_or_ambiguous_snapshots(self) -> None:
        cases = (
            subprocess.CompletedProcess(["tmux"], 1, "", "server unavailable"),
            subprocess.CompletedProcess(["tmux"], 0, "truncated snapshot\n", ""),
            subprocess.CompletedProcess(
                ["tmux"],
                0,
                "vl\t31\t0\t1\t%41\nvl\t31\t1\t1\t%42\n",
                "",
            ),
        )
        for result in cases:
            with self.subTest(result=result), patch("omo_manager.omo_agent_status.subprocess.run", return_value=result):
                self.assertIsNone(target_resolution_state("vl:31"))

    def test_problems_only_ignores_absent_low_priority_protected_human_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "low priority:\nvl_contrib_eval_human_resume_11889.md hvl:3\n"
            _ = todo.write_text(todo_text, encoding="utf-8")
            task = root / "vl_contrib_eval_human_resume_11889.md"
            task_text = task_frontmatter(
                "blocked",
                runat="hvl:3",
                managerat="vlprograms:0",
                blocked_on='"human decision: authorize a private port, polish, and revalidation of the independently approved candidate, or keep it archived"',
            )
            _ = task.write_text(task_text, encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])
            ) as inspect, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                self.assertEqual(set(), active_task_targets(root))

            self.assertEqual("", out.getvalue())
            inspect.assert_not_called()
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(task_text, task.read_text(encoding="utf-8"))

    def test_problems_only_ignores_absent_low_priority_exact_human_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "low priority:\nvl_target_select27.md vl_build_mgr:4\n"
            _ = todo.write_text(todo_text, encoding="utf-8")
            task = root / "vl_target_select27.md"
            task_text = task_frontmatter(
                "blocked",
                runat="vl_build_mgr:4",
                managerat="vl_build_mgr:3",
                blocked_on="human",
                pending_items=("Preserve the completed STOP packet until the human decides.",),
            )
            _ = task.write_text(task_text, encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])
            ) as inspect, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                self.assertEqual(set(), active_task_targets(root))

            self.assertEqual("", out.getvalue())
            inspect.assert_not_called()
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(task_text, task.read_text(encoding="utf-8"))

    def test_problems_only_ignores_absent_unsafe_bind_path_worker_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "human pending:\nanvl_a59_packet_route_work.md a52work:2\n"
            _ = todo.write_text(todo_text, encoding="utf-8")
            task = root / "anvl_a59_packet_route_work.md"
            task_text = task_frontmatter(
                "blocked",
                runat="a52work:2",
                managerat="vlcliimprove:0",
                blocked_on="authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts",
                pending_items=("Preserve the reviewed implementation route.", "Preserve terminal review constraints."),
            ) + "Task routing is repaired atomically. No second owner exists.\n"
            _ = task.write_text(task_text, encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.absent_bind_path_workdir_exists", return_value=False
            ), patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])) as inspect, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                self.assertEqual(set(), active_task_targets(root))

            self.assertEqual("", out.getvalue())
            inspect.assert_not_called()
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(task_text, task.read_text(encoding="utf-8"))

    def test_problems_only_retains_changed_unsafe_bind_path_worker_evidence(self) -> None:
        cases = (
            ("current", "authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts", False, False, "No second owner exists."),
            ("low priority", "different blocker", False, False, "No second owner exists."),
            ("low priority", "authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts", True, False, "No second owner exists."),
            ("low priority", "authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts", False, True, "No second owner exists."),
            ("low priority", "authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts", False, False, "Replacement ownership is unknown."),
        )
        for section, blocker, target_exists, workdir_exists, ownership in cases:
            with self.subTest(section=section, blocker=blocker, target_exists=target_exists, workdir_exists=workdir_exists, ownership=ownership), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text(f"{section}:\nanvl_a59_packet_route_work.md a52work:2\n", encoding="utf-8")
                _ = (root / "anvl_a59_packet_route_work.md").write_text(task_frontmatter("blocked", runat="a52work:2", blocked_on=blocker) + ownership + "\n", encoding="utf-8")
                out = StringIO()
                with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=target_exists), patch(
                    "omo_manager.omo_agent_status.absent_bind_path_workdir_exists", return_value=workdir_exists
                ), patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

                self.assertIn("missing: task=anvl_a59_packet_route_work.md", out.getvalue())

    def test_problems_only_retains_same_named_nested_bind_path_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            nested = root / "other"
            nested.mkdir()
            _ = (root / "TODO.md").write_text("low priority:\nother/anvl_a59_packet_route_work.md a52work:2\n", encoding="utf-8")
            _ = (nested / "anvl_a59_packet_route_work.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="a52work:2",
                    blocked_on="authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts",
                )
                + "No second owner exists.\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.absent_bind_path_workdir_exists", return_value=False
            ), patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertIn("missing: task=other/anvl_a59_packet_route_work.md", out.getvalue())

    def test_problems_only_retains_bind_path_record_with_ownership_only_in_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nanvl_a59_packet_route_work.md a52work:2\n", encoding="utf-8")
            _ = (root / "anvl_a59_packet_route_work.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="a52work:2",
                    blocked_on="authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts",
                    pending_items=("Record that No second owner exists.",),
                ),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.absent_bind_path_workdir_exists", return_value=False
            ), patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertIn("missing: task=anvl_a59_packet_route_work.md", out.getvalue())

    def test_reused_bind_path_target_running_pane_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nanvl_a59_packet_route_work.md a52work:2\n", encoding="utf-8")
            _ = (root / "anvl_a59_packet_route_work.md").write_text(
                task_frontmatter(
                    "blocked",
                    runat="a52work:2",
                    blocked_on="authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts",
                )
                + "No second owner exists.\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=True), patch(
                "omo_manager.omo_agent_status.absent_bind_path_workdir_exists", return_value=False
            ), patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["unrelated work"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertIn("blocked_idle: task=anvl_a59_packet_route_work.md", out.getvalue())

    def test_problems_only_ignores_ready_exact_external_delivery_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nvl_target_select27.md vl_build_mgr:4\n", encoding="utf-8")
            blocker = "final STOP report SHA-256 a187a1c44d639453a5bbd7e8fd9029dd4100dabbf8b7a79464710471748b4f6e has only replay f8f58a97780162ec525554b264ee8240f51ab1e6dfcd5f69177cd8b9e49116be commitment; accepted delivery and exact consumed-closure attestation are absent, so the task contract requires retaining all six items"
            _ = (root / "vl_target_select27.md").write_text(
                task_frontmatter("blocked", runat="vl_build_mgr:4", managerat="vl_build_mgr:3", blocked_on=blocker, pending_items=BLOCKED_DELIVERY_ITEMS),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["waiting"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            self.assertEqual("", out.getvalue())

    def test_problems_only_retains_changed_external_delivery_wait(self) -> None:
        cases = (
            ("other.md", "vl_build_mgr:4", 6, False, False, "low priority", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:5", 6, False, False, "low priority", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:4", BLOCKED_DELIVERY_ITEMS[:-1], False, False, "low priority", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:4", (*BLOCKED_DELIVERY_ITEMS[:-1], "Changed sixth item."), False, False, "low priority", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:4", 6, True, False, "low priority", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:4", 6, False, False, "current", Report("ready", ["waiting"])),
            ("vl_target_select27.md", "vl_build_mgr:4", 6, False, False, "low priority", Report("error", ["fatal"])),
        )
        blocker = "final STOP report SHA-256 a187a1c44d639453a5bbd7e8fd9029dd4100dabbf8b7a79464710471748b4f6e has only replay f8f58a97780162ec525554b264ee8240f51ab1e6dfcd5f69177cd8b9e49116be commitment; accepted delivery and exact consumed-closure attestation are absent, so the task contract requires retaining all six items"
        normalized_cases = tuple((name, target, BLOCKED_DELIVERY_ITEMS if items == 6 else items, is_manager, pending_delivery, section, report) for name, target, items, is_manager, pending_delivery, section, report in cases)
        for task_name, target, items, is_manager, pending_delivery, section, report in normalized_cases:
            with self.subTest(task_name=task_name, target=target, n_items=len(items), is_manager=is_manager, section=section, report=report.status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text(f"{section}:\n{task_name} {target}\n", encoding="utf-8")
                task_text = task_frontmatter("blocked", runat=target, blocked_on=blocker, is_manager=is_manager, pending_items=items)
                if pending_delivery:
                    task_text += "(pending)\n"
                _ = (root / task_name).write_text(task_text, encoding="utf-8")
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

                self.assertIn(f"task={task_name}", out.getvalue())

    def test_problems_only_retains_actionable_protected_human_target_states(self) -> None:
        cases = (
            ("running", "", (), "hvl:3", False, "low priority"),
            ("long_running", "persistent role", (), "hvl:3", False, "low priority"),
            ("blocked", "human decision: authorize the private port", ("Apply the decision.",), "hvl:3", False, "low priority"),
            ("blocked", "dependency unavailable", (), "hvl:3", False, "low priority"),
            ("blocked", "human decision pending", (), "hvl:3", False, "low priority"),
            ("blocked", "human decision:", (), "hvl:3", False, "low priority"),
            ("blocked", "human decision: authorize the private port", (), "cfg:3", False, "low priority"),
            ("blocked", "human decision: authorize the private port", (), "hvl:3", True, "low priority"),
            ("blocked", "human decision: authorize the private port", (), "hvl:3", False, "current"),
        )
        for status, blocker, pending_items, target, target_exists, section in cases:
            with self.subTest(status=status, blocker=blocker, pending_items=pending_items, target=target, target_exists=target_exists, section=section), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text(f"{section}:\nreview.md {target}\n", encoding="utf-8")
                _ = (root / "review.md").write_text(
                    task_frontmatter(status, runat=target, blocked_on=blocker, pending_items=pending_items),
                    encoding="utf-8",
                )
                out = StringIO()
                with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=target_exists), patch(
                    "omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])
                ), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                    self.assertEqual({target}, active_task_targets(root))

                self.assertIn("missing: task=review.md", out.getvalue())

    def test_historical_protected_target_does_not_hide_unrelated_agent_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nreview.md hvl:3\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="hvl:3", blocked_on="human decision: authorize the private port"),
                encoding="utf-8",
            )
            out = StringIO()

            def inspect_target(args: object) -> Report:
                return Report("running", ["unrelated work"]) if getattr(args, "target") == "wl:31" else Report("missing", [])

            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.tmux_list_panes", return_value=["wl:31"]
            ), patch("omo_manager.omo_agent_status.inspect", side_effect=inspect_target), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            text = out.getvalue()
            self.assertIn("untracked_agent: task=tmux:wl:31", text)
            self.assertNotIn("task=review.md", text)

    def test_reused_historical_exact_human_target_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nreview.md vl_build_mgr:4\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="vl_build_mgr:4", blocked_on="human", pending_items=("Preserve the decision queue.",)),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=True), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["unrelated agent"])
            ), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                self.assertEqual({"vl_build_mgr:4"}, active_task_targets(root))

            self.assertIn("blocked_idle: task=review.md", out.getvalue())

    def test_problems_only_reports_conflicting_index_for_absent_protected_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nreview.md hvl:3\n\nlow priority:\n./review.md hvl:3\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="hvl:3", blocked_on="human decision: authorize the private port"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.target_resolves_exactly", return_value=False), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])
            ), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))
                self.assertEqual({"hvl:3"}, active_task_targets(root))

            self.assertIn("missing: task=review.md", out.getvalue())

    def test_problems_only_reports_untracked_pane_reusing_historical_human_pending_runat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nreview.md\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="wl:31", blocked_on="waiting on the human's decision about the release"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["wl:31.0"]), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["unrelated agent"])
            ), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            text = out.getvalue()
            self.assertIn("untracked_agent: task=tmux:wl:31", text)
            self.assertNotIn("task=review.md", text)

    def test_problems_only_ignores_aliased_registry_row_for_reused_historical_runat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"review.md","tmux_target":"wl:31.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\n./review.md\n", encoding="utf-8")
            _ = (root / "review.md").write_text(
                task_frontmatter("blocked", runat="wl:31", blocked_on="waiting on the human's decision about the release"),
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["wl:31.0"]), patch(
                "omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["unrelated agent"])
            ), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            text = out.getvalue()
            self.assertIn("untracked_agent: task=tmux:wl:31", text)
            self.assertNotIn("role=registry_unmanaged", text)
            self.assertNotIn("task=./review.md", text)

    def test_problems_only_keeps_missing_live_targetless_todo_task_visible(self) -> None:
        for status in ("running", "long_running"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nworker.md\n", encoding="utf-8")
                _ = (root / "worker.md").write_text(task_frontmatter(status, runat="wl:32"), encoding="utf-8")
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("missing", [])), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

                self.assertIn("missing: task=worker.md evidence=target=wl:32", out.getvalue())

    def test_problems_only_pending_active_target_suppresses_completed_todo_attribution(self) -> None:
        for active_target in ("wl:3", "wl:3.0"):
            with self.subTest(active_target=active_target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nactive.md wl 3\n\nprevious:\ncompleted.md wl 3\n", encoding="utf-8")
                _ = (root / "active.md").write_text(f"{task_frontmatter('running', runat=active_target)}(pending)\nrecover delivery\n", encoding="utf-8")
                _ = (root / "completed.md").write_text(task_frontmatter("done", runat="wl:3"), encoding="utf-8")
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["active pending delivery"])) as inspect, redirect_stdout(out):
                    self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

                self.assertEqual("", out.getvalue())
                self.assertEqual([active_target], [call.args[0].target for call in inspect.call_args_list])

    def test_problems_only_pending_active_target_keeps_completed_registry_bookkeeping_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"completed.md","tmux_target":"wl:3.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md wl 3\n\nprevious:\ncompleted.md wl 3\n", encoding="utf-8")
            _ = (root / "active.md").write_text(f"{task_frontmatter('running', runat='wl:3')}(pending)\nrecover delivery\n", encoding="utf-8")
            _ = (root / "completed.md").write_text(task_frontmatter("done", runat="wl:3"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["active pending delivery"])) as inspect, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            text = out.getvalue()
            self.assertIn("done-stale: task=completed.md", text)
            self.assertNotIn("ready: task=completed.md", text)
            self.assertEqual(["wl:3"], [call.args[0].target for call in inspect.call_args_list])

    def test_problems_only_reused_registry_target_keeps_only_stale_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"completed.md","tmux_target":"wl:3.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nactive.md wl 3\n\nprevious:\ncompleted.md wl 3\n", encoding="utf-8")
            _ = (root / "active.md").write_text(
                task_frontmatter("blocked", runat="wl:3", blocked_on="human decision on the required guarantee"),
                encoding="utf-8",
            )
            _ = (root / "completed.md").write_text(task_frontmatter("done", runat="wl:3"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["later task output"])) as inspect, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

            text = out.getvalue()
            self.assertIn("done-stale: task=completed.md", text)
            self.assertNotIn("ready: task=completed.md", text)
            self.assertNotIn("later task output", text)
            self.assertEqual(["wl:3"], [call.args[0].target for call in inspect.call_args_list])

    def test_problems_only_keeps_active_reused_target_problems_visible(self) -> None:
        cases = (
            ("running", "", Report("ready", ["idle"]), "ready"),
            ("blocked", "dependency unavailable", Report("ready", ["idle"]), "blocked_idle"),
            ("running", "", Report("error", ["fatal"]), "error"),
            ("running", "", Report("missing", []), "missing"),
            ("running", "", Report("stuck_input", ["› retry report delivery"], "retry report delivery", True), "stuck_input"),
        )
        for status, blocked_on, report, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = root / "sessions.json"
                _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
                _ = (root / "TODO.md").write_text("current:\nactive.md wl 3\n\nprevious:\ncompleted.md wl 3\n", encoding="utf-8")
                _ = (root / "active.md").write_text(task_frontmatter(status, runat="wl:3", blocked_on=blocked_on), encoding="utf-8")
                _ = (root / "completed.md").write_text(task_frontmatter("done", runat="wl:3"), encoding="utf-8")
                out = StringIO()
                with patch("omo_manager.omo_agent_status.inspect", return_value=report), redirect_stdout(out):
                    self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--no-auto-unstick"]))

                text = out.getvalue()
                self.assertIn(f"{expected}: task=active.md evidence=target=wl:3", text)
                self.assertNotIn("task=completed.md", text)

    def test_problems_only_reports_later_ready_stale_running_duplicate_registry_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"pending.md","tmux_target":"vl:1.0","started_at_s":1},{"task_file":"active.md","tmux_target":"vl:1.0","started_at_s":2}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("current:\npending.md vl 1\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('running', runat='vl:1')}(pending)\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:2"), encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:1.0":
                    return Report("ready", ["idle"])
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=active.md evidence=target=vl:1.0 role=registry_unmanaged task_status=running", text)
            self.assertNotIn("ready: task=pending.md", text)

    def test_problems_only_reports_ready_stale_done_todo_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md vl 53\n", encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter("done", runat="vl:53"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=stale.md evidence=target=vl:53 role=todo_unmanaged task_status=done", text)

    def test_problems_only_reports_ready_non_vl_stale_done_todo_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md cfg 53\n", encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter("done", runat="cfg:53"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=stale.md evidence=target=cfg:53 role=todo_unmanaged task_status=done", text)

    def test_problems_only_reports_ready_unregistered_agent_pane_as_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nvl_old_review.md\n", encoding="utf-8")
            _ = (root / "vl_old_review.md").write_text("(done)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15", "h:2"]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: untracked_agent=1", text)
            self.assertIn("untracked_agent: task=tmux:vl:15 evidence=target=vl:15 role=tmux_unmanaged", text)
            self.assertNotIn("h:2", text)

    def test_problems_only_reports_running_unregistered_agent_pane_as_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15"]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: untracked_agent=1", text)
            self.assertIn("untracked_agent: task=tmux:vl:15 evidence=target=vl:15 role=tmux_unmanaged output=working", text)

    def test_root_manager_reports_untracked_agents_for_all_owner_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl:15\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text(task_frontmatter("running", runat="vl:15", managerat="wl:16", is_manager=True), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:30", "wl:22", "h:2"]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            text = out.getvalue()
            self.assertIn("untracked_agent: task=tmux:wl:22 evidence=target=wl:22 role=tmux_unmanaged", text)
            self.assertIn("untracked_agent: task=tmux:vl:30 evidence=target=vl:30 role=tmux_unmanaged", text)
            self.assertIn("owner_target=vl:15", text)
            self.assertNotIn("h:2", text)

    def test_manager_view_skips_valid_upper_routed_submanager_tmux_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops.md wl:16\nperipheral_projects.md wl:18\n", encoding="utf-8")
            _ = (root / "manager_ops.md").write_text(task_frontmatter("running", runat="wl:16", managerat="wl:1", is_manager=True), encoding="utf-8")
            _ = (root / "peripheral_projects.md").write_text(task_frontmatter("blocked", runat="wl:18", managerat="wl:1", is_manager=True, blocked_on="waiting on human"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["wl:16", "wl:18", "wl:22"]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))

            text = out.getvalue()
            self.assertIn("untracked_agent: task=tmux:wl:22 evidence=target=wl:22 role=tmux_unmanaged", text)
            self.assertNotIn("tmux:wl:16", text)
            self.assertNotIn("tmux:wl:18", text)

    def test_problems_only_uses_todo_target_for_unmanaged_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"vl:10.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 10\n\nprevious:\nstale.md vl 9\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="vl:10"), encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter("done", runat="vl:10"), encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:9":
                    return Report("error", ["Selected model is at capacity. Please try a different model."])
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("error: task=stale.md evidence=target=vl:9 role=todo_unmanaged task_status=done", text)
            self.assertNotIn("target=vl:10 role=todo_unmanaged", text)

    def test_problems_only_reports_live_vl_panes_without_todo_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nvl_supervisor_5410.md\nvl_old_review.md\n", encoding="utf-8")
            _ = (root / "vl_supervisor_5410.md").write_text(task_frontmatter("done", runat="vl:20"), encoding="utf-8")
            _ = (root / "vl_old_review.md").write_text("(done)\n", encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:20":
                    return Report("not_codex", ["fish prompt"])
                if target == "vl:15":
                    return Report("stuck_input", ["› pasted prompt"], "pasted prompt", True)
                if target == "vl:0":
                    return Report("not_codex", ["editor shell"])
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:0", "vl:15", "vl:20", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("not_codex: task=vl_supervisor_5410.md evidence=target=vl:20 role=todo_unmanaged", text)
            self.assertIn("untracked_agent: task=tmux:vl:15 evidence=target=vl:15 role=tmux_unmanaged", text)
            self.assertIn("untracked_agent: task=tmux:wl:2 evidence=target=wl:2 role=tmux_unmanaged", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertNotIn("tmux:vl:0", text)
            unstick.assert_called_once()

    def test_problems_only_reports_live_vl_panes_for_loose_vl_todo_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 15\n", encoding="utf-8")

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:15":
                    return Report("stuck_input", ["› pasted prompt"], "pasted prompt", True)
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("untracked_agent: task=active.md evidence=target=vl:15 role=todo_current_untracked task_status=unlinked", text)
            self.assertIn("untracked_agent: task=tmux:wl:2 evidence=target=wl:2 role=tmux_unmanaged", text)
            self.assertIn("unstick=sent_enter", text)
            unstick.assert_called_once()

    def test_problems_only_reports_running_loose_todo_target_as_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 15\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: untracked_agent=1", text)
            self.assertIn("untracked_agent: task=active.md evidence=target=vl:15 role=todo_current_untracked task_status=unlinked output=working", text)

    def test_problems_only_keeps_explicit_vl_manager_target_out_of_tmux_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 15\n", encoding="utf-8")
            report = Report("stuck_input", ["› manager prompt"], "manager prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15"]), patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "vl:15"]))
            unstick.assert_called_once_with("vl:15", report)
            text = out.getvalue()
            self.assertIn("stuck_input: task=manager evidence=target=vl:15 role=manager", text)
            self.assertNotIn("task=tmux:vl:15", text)

    def test_problems_only_reports_stale_done_registry_even_with_ready_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1},{"task_file":"done.md","tmux_target":"cfg:2.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n\nprevious:\ndone.md cfg 2\n", encoding="utf-8")
            _ = (root / "role.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="persistent VL supervisor role waiting for follow-up"), encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:2"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("done-stale: task=done.md", out.getvalue())
            self.assertNotIn("ready: task=role.md", out.getvalue())

    def test_problems_only_reports_stale_done_registry_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"done.md","tmux_target":"cfg:2.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\ndone.md cfg 2\n", encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:2", managerat="wl:17"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            text = out.getvalue()
            self.assertIn("done-stale: task=done.md", text)
            self.assertIn("owner_target=wl:17", text)

    def test_problems_only_ignores_pending_task_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"pending.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\npending.md cfg 1\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('running', runat='cfg:1')}(pending)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_submits_visible_input_for_pending_delivery_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"pending.md","tmux_target":"hwl:3.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\npending.md hwl 3\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('long_running', runat='hwl:3')}(pending)\n", encoding="utf-8")
            report = Report("stuck_input", ["› Immediately record every pending task"], "Immediately record every pending task", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch(
                "omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"
            ) as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once()
            self.assertIn("stuck_input: task=pending.md", out.getvalue())
            self.assertIn("unstick=sent_enter", out.getvalue())

    def test_problems_only_ignores_done_pending_task_marker_for_stale_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"pending.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\npending.md cfg 1\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('done', runat='cfg:1')}(pending)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_ignores_blocked_pending_task_marker_for_blocked_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\npending.md cfg 1\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('blocked', runat='cfg:1', blocked_on='waiting on reply')}(pending)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_does_not_rediscover_known_pending_vl_target_as_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\npending.md vl 15\n", encoding="utf-8")
            _ = (root / "pending.md").write_text(f"{task_frontmatter('running', runat='vl:15')}(pending)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15"]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_pending_task_item_after_done_in_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\ndone.md cfg 1\n", encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:1", pending_items=("preserve human request",)), encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: human_request=1", text)
            self.assertIn("human_request: task=done.md evidence=pending_item=preserve human request", text)

    def test_problems_only_ignores_pending_task_item_after_done_in_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\ndone.md cfg 1\n", encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:1", pending_items=("preserve human request",)), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_pending_task_item_after_done_does_not_make_default_status_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\ndone.md cfg 1\n", encoding="utf-8")
            _ = (root / "done.md").write_text(task_frontmatter("done", runat="cfg:1", pending_items=("preserve human request",)), encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))
            self.assertIn("human_request=0", out.getvalue())
            self.assertNotIn("human_request: task=done.md", out.getvalue())

    def test_problems_only_ignores_pending_task_items_while_running_pending_or_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrunning.md cfg 1\npending.md cfg 2\nhuman pending:\nblocked.md cfg 3\n", encoding="utf-8")
            for name, status in (("running.md", "running"), ("pending.md", "running"), ("blocked.md", "blocked")):
                text = task_frontmatter(status, runat="cfg:1", pending_items=("keep item",), blocked_on="waiting" if status == "blocked" else "")
                if name == "pending.md":
                    text += "(pending)\n"
                _ = (root / name).write_text(text, encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_ignores_pending_task_items_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nnew.md cfg 1\n", encoding="utf-8")
            _ = (root / "new.md").write_text(task_frontmatter("running", runat="cfg:1", pending_items=("keep item",)), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_exit_code_if_active_returns_distinct_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["waiting"])):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))

    def test_default_exit_code_stays_zero_when_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text(task_frontmatter("running", runat="cfg:1"), encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry)]))


if __name__ == "__main__":
    _ = unittest.main()
