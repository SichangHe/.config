import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import Args, classify_task, format_problem_summary, load_local_env, load_task_state, main, parse_task_lines, persistent_blocked_task_lines, registry_prune, session_records
from omo_manager.omo_agent_status import SessionRecord, StatusRow, TaskLine
from omo_manager.omo_codex_status import Report


class AgentStatusTests(unittest.TestCase):
    def test_load_task_state_prefers_reopened_active_over_stale_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n\nprevious:\ndone.md cfg 2 (done)\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Complete, delivered to human\n- `active.md` complete.\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            _ = (root / "done.md").write_text("runat: cfg:2 codex\n(done)\n", encoding="utf-8")
            current, done, _human_pending = load_task_state(root)
            self.assertIn("active.md", current)
            self.assertNotIn("active.md", done)
            self.assertIn("done.md", done)

    def test_load_task_state_ignores_legacy_manager_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Active / waiting\n- `tracker.md` (`pb:4`, port `18941`): running\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            current, done, human_pending = load_task_state(root)
            self.assertEqual({"active.md"}, set(current))
            self.assertEqual(set(), done)
            self.assertEqual(set(), human_pending)

    def test_load_task_state_uses_latest_task_file_tag_not_todo_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("human pending:\nreview.md (done)\n\nprevious:\nold.md (blocked)\n", encoding="utf-8")
            _ = (root / "review.md").write_text("runat: pb:4 codex\n(done)\n\n(blocked)\n", encoding="utf-8")
            _ = (root / "old.md").write_text("runat: wl:2 codex\n(running)\n", encoding="utf-8")
            current, done, human_pending = load_task_state(root)
            self.assertEqual({"old.md"}, set(current))
            self.assertEqual(set(), done)
            self.assertEqual({"review.md"}, human_pending)
            self.assertEqual("wl:2", current["old.md"].target)

    def test_persistent_blocked_task_lines_marks_role_from_latest_blocked_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(running)\n\n(done)\n\n(blocked: persistent VL proof-analysis role waiting for follow-up)\n", encoding="utf-8")
            current, _done, _human_pending = load_task_state(root)
            standby = persistent_blocked_task_lines(root)
            self.assertNotIn("role.md", current)
            self.assertEqual("role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)
            self.assertEqual("blocked", standby[0].status)

    def test_persistent_blocked_task_lines_accepts_separate_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(running)\n\n(blocked) (persistent role waiting for followup)\n", encoding="utf-8")
            standby = persistent_blocked_task_lines(root)
            self.assertEqual("role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)

    def test_persistent_blocked_task_lines_accepts_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(running)\n\n(blocked)\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            standby = persistent_blocked_task_lines(root)
            self.assertEqual("role.md", standby[0].task_file)
            self.assertTrue(standby[0].persistent_role)

    def test_persistent_blocked_task_lines_rejects_nonadjacent_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked)\nplain prose between status and note\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            self.assertEqual([], persistent_blocked_task_lines(root))

    def test_persistent_blocked_task_lines_rejects_blank_line_split_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked)\n\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            self.assertEqual([], persistent_blocked_task_lines(root))

    def test_persistent_blocked_task_lines_deduplicates_todo_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            _ = (root / "done.md").write_text("runat: cfg:2 codex\n(done)\n", encoding="utf-8")
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

    def test_problem_summary_ignores_ready_persistent_roles_only(self) -> None:
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

    def test_problems_only_stays_quiet_when_all_active_agents_are_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_stuck_input_during_background_terminal_wait(self) -> None:
        pane = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once()
            self.assertIn("stuck_input: task=active.md", out.getvalue())
            self.assertIn("unstick=sent_enter", out.getvalue())
            self.assertIn("unstuck: target=cfg:1.0 task=active.md action=sent_enter", out.getvalue())

    def test_problems_only_auto_unsticks_safe_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            _ = (root / "other.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            out = StringIO()
            report = Report("stuck_input", ["› Continue task"], "Continue task", True)
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_called_once_with("cfg:1.0", report)
            text = out.getvalue()
            self.assertEqual(1, text.count("unstuck: target=cfg:1.0"))
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstick=already_sent", text)

    def test_problems_only_reports_manager_target_stuck_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Reply to human"], "Reply to human", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertIn("stuck_input: task=manager evidence=target=mgr:1.0 role=manager", text)
            self.assertIn("unstick=sent_enter", text)
            self.assertIn("unstuck: target=mgr:1.0 task=manager action=sent_enter", text)

    def test_problems_only_omits_ready_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "mgr:1.0"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_ready_running_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("ready: task=active.md", out.getvalue())

    def test_problems_only_reports_ready_running_persistent_role_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(running: persistent VL spec-analysis role resumed for follow-up work)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("ready: task=role.md", out.getvalue())

    def test_problems_only_stays_quiet_for_ready_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL spec-analysis role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_stays_quiet_for_ready_blocked_persistent_role_separate_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked) (persistent role waiting for followup)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_problems_only_reports_stuck_input_for_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL spec-analysis role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Run /review on my current changes"])), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("stuck_input: task=role.md", out.getvalue())

    def test_problems_only_reports_error_for_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=role.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked) (persistent role waiting for followup)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=role.md", out.getvalue())

    def test_problems_only_reports_not_codex_for_split_note_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked)\n(persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("not_codex", [])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("not_codex: task=role.md", out.getvalue())

    def test_problems_only_reports_stale_done_registry_even_with_ready_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1},{"task_file":"done.md","tmux_target":"cfg:2.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n\nprevious:\ndone.md cfg 2\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            _ = (root / "done.md").write_text("runat: cfg:2 codex\n(done)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("done-stale: task=done.md", out.getvalue())
            self.assertNotIn("ready: task=role.md", out.getvalue())

    def test_problems_only_ignores_pending_task_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"pending.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\npending.md cfg 1\n", encoding="utf-8")
            _ = (root / "pending.md").write_text("runat: cfg:1 codex\n(pending)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["ready"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_exit_code_if_active_returns_distinct_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["waiting"])):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))

    def test_default_exit_code_stays_zero_when_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry)]))


if __name__ == "__main__":
    _ = unittest.main()
