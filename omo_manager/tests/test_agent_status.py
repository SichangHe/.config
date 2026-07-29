import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import Args, TaskFrontmatterError, classify_task, format_problem_summary, format_summary, load_local_env, load_task_state, main, parse_task_lines, parse_task_metadata, persistent_blocked_task_lines, registry_prune, session_records
from omo_manager.omo_agent_status import SessionRecord, StatusRow, TaskLine
from omo_manager.omo_codex_status import Args as CodexStatusArgs, Report


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

    def test_problems_only_reports_long_running_manager_without_required_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nbroken_manager.md cfg 7\n", encoding="utf-8")
            malformed = task_frontmatter("running", runat="cfg:7", is_manager=True).replace("status: running", "status: long_running")
            _ = (root / "broken_manager.md").write_text(malformed, encoding="utf-8")
            out = StringIO()

            report = Report("stuck_input", ["queued input"], "queued input", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as submit, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            submit.assert_not_called()

            text = out.getvalue()
            self.assertIn("agent-problems: malformed_task=1", text)
            self.assertIn("malformed_task: task=broken_manager.md", text)
            self.assertIn("`blocked_on` is required", text)
            self.assertEqual(1, text.count("malformed_task: task=broken_manager.md"))
            self.assertIn("untracked_agent: task=broken_manager.md", text)
            self.assertIn("unstick=disabled:malformed_task_present", text)

    def test_problems_only_reports_malformed_active_low_priority_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("low priority:\nbroken_manager.md cfg 7\n", encoding="utf-8")
            malformed = task_frontmatter("running", runat="cfg:7", is_manager=True).replace("status: running", "status: long_running")
            _ = (root / "broken_manager.md").write_text(malformed, encoding="utf-8")
            out = StringIO()

            with redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            text = out.getvalue()
            self.assertIn("agent-problems: malformed_task=1", text)
            self.assertIn("malformed_task: task=broken_manager.md", text)
            self.assertIn("`blocked_on` is required", text)

    def test_malformed_active_aliases_report_once_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nbroken.md cfg 8\n./broken.md cfg 8\n", encoding="utf-8")
            malformed = task_frontmatter("running", runat="cfg:8").replace("status: running", "status: long_running")
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
            malformed = task_frontmatter("running", runat="cfg:8", is_manager=True).replace("status: running", "status: long_running")
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
            malformed = task_frontmatter("running", runat="cfg:9").replace("status: running", "status: long_running")
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

    def test_frontmatter_rejects_long_running_without_blocked_on(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "required"):
            parse_task_metadata(task_frontmatter("running").replace("status: running", "status: long_running"))

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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            self.assertEqual("", out.getvalue())

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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present"), redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
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

            self.assertIn("not_codex: task=manager evidence=target=wl:1.0 role=manager", out.getvalue())

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

    def test_problems_only_reports_blocked_worker_even_with_running_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg:1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", blocked_on="dependency.md"), encoding="utf-8")
            _ = (root / "dependency.md").write_text(task_frontmatter("running", runat="cfg:2", managerat="cfg:1"), encoding="utf-8")
            out = StringIO()

            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))

            self.assertIn("not_codex: task=worker.md", out.getvalue())

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
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick:
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

    def test_problems_only_skips_non_hvl_human_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_frontmatter("blocked", runat="cfg:1", managerat="wl:16", blocked_on="waiting on human response"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_manager_ops_future_request_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nmanager_ops_submanager_8653.md wl 16\n", encoding="utf-8")
            _ = (root / "manager_ops_submanager_8653.md").write_text(task_frontmatter("blocked", runat="wl:16", managerat="wl:1", is_manager=True, blocked_on="waiting for future human or watcher manager-ops request"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

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

    def test_problems_only_skips_human_helper_direction_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nhelper_audit_agent_9580.md hcfg 1\n", encoding="utf-8")
            _ = (root / "helper_audit_agent_9580.md").write_text(task_frontmatter("blocked", runat="hcfg:1", managerat="wl:16", blocked_on="future human/helper audit direction"), encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            self.assertEqual("", out.getvalue())

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

    def test_problems_only_skips_ready_hvl_concrete_human_review_wait(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_ready_human_review_wait_with_stage_target(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_ready_non_hvl_human_review_wait(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

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

    def test_problems_only_skips_manager_human_facing_review_waits(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_hvl_direct_human_discussion_wait(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_vl_human_pending_wait(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_hvl_human_approval_wait(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_explicit_human_integration_decision_or_authorization(self) -> None:
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
                    self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
                self.assertEqual("", out.getvalue())

    def test_problems_only_skips_hvl_review_waiting_variant(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_skips_hvl_review_wait_case_variant(self) -> None:
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
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

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
