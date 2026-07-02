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

    def test_load_task_state_prefers_runat_over_historical_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current.md vl 15\n", encoding="utf-8")
            _ = (root / "vl_submanager_current.md").write_text(
                "runat: vl:15 codex\n"
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

    def test_problems_only_stays_quiet_for_review_placeholder_during_background_terminal_wait(self) -> None:
        pane = ['• Waiting for background terminal · 1 background terminal running · /ps to view · /stop to close', '', '› Run /review on my current changes', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_codex_status.tail", return_value=pane), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("agent-problems: error=1", text)
            self.assertIn("error: task=active.md", text)
            self.assertNotIn("ready: task=active.md", text)

    def test_error_evidence_prefers_warning_line_above_input_box(self) -> None:
        pane = ['────', '⚠ Selected model is at capacity. Please try a different model.', 'note', 'detail', '› Explain this codebase', '  gpt-5.5']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
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

    def test_problems_only_unsticks_alias_target_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"cfg:1","started_at_s":1},{"task_file":"other.md","tmux_target":"cfg:1.0","started_at_s":2}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\nother.md cfg 1\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: cfg:1 codex\n(running)\n", encoding="utf-8")
            _ = (root / "other.md").write_text("runat: cfg:1.0 codex\n(running)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: wl:1 codex\n(running)\n", encoding="utf-8")
            report = Report("stuck_input", ["› manager status text"], "manager status text", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:1.0"]))
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
            _ = (root / "active.md").write_text("runat: mgr:1.0 codex\n(running)\n", encoding="utf-8")
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

    def test_problems_only_reports_ready_blocked_persistent_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL spec-analysis role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["› Use /skills to list available skills"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("blocked_idle: task=role.md", out.getvalue())

    def test_problems_only_reports_ready_blocked_persistent_role_separate_note_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"role.md","tmux_target":"cfg:1.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nrole.md cfg 1\n", encoding="utf-8")
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked) (persistent role waiting for followup)\n", encoding="utf-8")
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
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL spec-analysis role waiting for follow-up)\n", encoding="utf-8")
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
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
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
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
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
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("error", ["traceback"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertIn("error: task=role.md", out.getvalue())

    def test_problems_only_reports_not_codex_blocked_persistent_role(self) -> None:
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

    def test_problems_only_reports_split_note_not_codex_blocked_persistent_role(self) -> None:
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

    def test_problems_only_reports_blocked_idle_vl_supervisor_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text(
                "current:\nmanager_vl_watchdog.md wl 5\n\nprevious:\nvl_supervisor_current_7404.md vl 7\nvl_worker.md vl 9\n",
                encoding="utf-8",
            )
            _ = (root / "manager_vl_watchdog.md").write_text("runat: wl:5 codex\n(running)\n", encoding="utf-8")
            _ = (root / "vl_supervisor_current_7404.md").write_text(
                "runat: vl:7 codex\n(blocked: persistent supervisor waiting on `vl_worker.md`; image lacks codex)\n",
                encoding="utf-8",
            )
            _ = (root / "vl_worker.md").write_text(
                "runat: vl:9 codex\n(blocked: Docker image lacks codex; no worker output was produced)\n",
                encoding="utf-8",
            )
            def fake_inspect(args: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "wl:5" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: blocked_idle=2", text)
            self.assertIn("manager-action: blocked_idle>0 inspect blocked agents", text)
            self.assertIn("blocked_idle: task=vl_supervisor_current_7404.md evidence=target=vl:7 role=blocked_idle_vl", text)
            self.assertIn("blocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl_dependency", text)
            self.assertIn("image lacks codex", text)

    def test_problems_only_reports_blocked_idle_to_explicit_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nworker.md cfg 1\n", encoding="utf-8")
            _ = (root / "worker.md").write_text("runat: cfg:1 codex\nmanagerat: wl:16\n(blocked: waiting on human API key)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:17"]))
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out := StringIO()):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16"]))
            text = out.getvalue()
            self.assertIn("blocked_idle: task=worker.md", text)
            self.assertIn("reason=waiting on human API key", text)
            self.assertIn("owner_target=wl:16", text)

    def test_problems_only_does_not_count_running_blocked_vl_target_as_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_worker.md vl 9\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text("runat: vl:9 codex\n(blocked: image lacks codex)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("running", ["working"])), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            self.assertEqual("", out.getvalue())

    def test_main_manager_problems_only_excludes_vl_worker_owned_by_active_submanager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl 15\nvl_worker.md vl 1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text("runat: vl:15 codex\n(running)\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text("runat: vl:1 codex\n(blocked: waiting on proof owner)\n", encoding="utf-8")
            def fake_inspect(args: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:15" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16.0"]))
            self.assertEqual("", out.getvalue())

    def test_main_manager_problems_only_excludes_vl_worker_owned_by_bulleted_submanager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\n- `vl_submanager_current_8653.md` (`vl:15`)\n- `vl_worker.md` (`vl:1`)\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text("runat: vl:15 codex\n(running)\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text("runat: vl:1 codex\n(blocked: waiting on proof owner)\n", encoding="utf-8")
            def fake_inspect(args: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:15" else Report("ready", ["idle"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "wl:16.0"]))
            self.assertEqual("", out.getvalue())

    def test_vl_submanager_problems_only_includes_vl_worker_without_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_submanager_current_8653.md vl 15\nvl_worker.md vl 1\n", encoding="utf-8")
            _ = (root / "vl_submanager_current_8653.md").write_text("runat: vl:15 codex\n(running)\n", encoding="utf-8")
            _ = (root / "vl_worker.md").write_text("runat: vl:1 codex\n(blocked: waiting on proof owner)\n", encoding="utf-8")
            def fake_inspect(args: object) -> Report:
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
            _ = (root / "vl_untracked.md").write_text("runat: vl:8 codex\n", encoding="utf-8")
            _ = (root / "vl_supervisor_current_7404.md").write_text("runat: vl:7 codex\n(blocked: image lacks codex)\n", encoding="utf-8")
            def fake_inspect(args: object) -> Report:
                return Report("running", ["working"]) if getattr(args, "target") == "vl:8" else Report("ready", ["idle"])

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
            _ = (root / "vl_worker.md").write_text("runat: vl:9 codex\n(blocked: image lacks codex)\n", encoding="utf-8")
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
            _ = (root / "vl_submanager_current_8653.md").write_text(
                "runat: vl:15 codex\n(running)\n",
                encoding="utf-8",
            )
            _ = (root / "vl_dirty_audit.md").write_text(
                "runat: vl:20 codex\n(blocked: waiting on proof owner)\n",
                encoding="utf-8",
            )
            _ = (root / "vl_followup.md").write_text(
                "runat: vl:20 codex\n(done)\n",
                encoding="utf-8",
            )
            report = Report("stuck_input", ["Reported privately.", "› [Pasted Content 1024 chars][Pasted Content 1024 chars] #2", "  (pending)"], "[Pasted Content 1024 chars][Pasted Content 1024 chars] #2\n  (pending)", True)
            def fake_inspect(args: object) -> Report:
                return report if getattr(args, "target") == "vl:20" else Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter"), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only", "--manager-target", "vl:15"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertIn("stuck_input: task=vl_dirty_audit.md evidence=target=vl:20 role=blocked_idle_vl task_status=blocked", text)
            self.assertIn("idle_status=stuck_input", text)
            self.assertIn("unstuck: target=vl:20 task=vl_dirty_audit.md action=sent_enter", text)
            self.assertNotIn("blocked_idle: task=vl_dirty_audit.md", text)

    def test_blocked_vl_target_does_not_duplicate_current_target_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nvl_current.md vl:20\nvl_old.md vl:blocked\n", encoding="utf-8")
            _ = (root / "vl_current.md").write_text("runat: vl:20 codex\n(running)\n", encoding="utf-8")
            _ = (root / "vl_old.md").write_text("runat: vl:20 codex\n(blocked: old blocker)\n", encoding="utf-8")
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
            _ = (root / "vl_role.md").write_text(
                "runat: vl:20 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n",
                encoding="utf-8",
            )
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
            _ = (root / "vl_role.md").write_text(
                "runat: vl:20 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n",
                encoding="utf-8",
            )
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("stuck_input", ["› Continue task"], "Continue task", True)), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: stuck_input=1", text)
            self.assertEqual(1, text.count("stuck_input: task=vl_role.md"))
            unstick.assert_called_once()

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
            _ = (root / "vl_one.md").write_text("runat: vl:9 codex\n(blocked: first blocker)\n", encoding="utf-8")
            _ = (root / "vl_two.md").write_text("runat: vl:9.0 codex\n(blocked: second blocker)\n", encoding="utf-8")
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
            _ = (root / "vl_worker.md").write_text("runat: vl:9 codex\n(blocked: image lacks codex)\n", encoding="utf-8")
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])):
                self.assertEqual(0, main(["--root", str(root), "--registry", str(registry), "--exit-code-if-active"]))

    def test_problems_only_reports_registry_unmanaged_capacity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"blocked.md","tmux_target":"vl:7.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("human pending:\nblocked.md vl 7\n", encoding="utf-8")
            _ = (root / "blocked.md").write_text("runat: vl:7 codex\n(blocked)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: vl:2 codex\n(running)\n", encoding="utf-8")

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

    def test_problems_only_reports_registry_unmanaged_done_stuck_input_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"old.md","tmux_target":"vl:23.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nold.md vl 23\n", encoding="utf-8")
            _ = (root / "old.md").write_text("runat: vl:23 codex\n(done)\n", encoding="utf-8")
            report = Report("stuck_input", ["› pasted manager prompt"], "pasted manager prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("stuck_input: task=old.md evidence=target=vl:23.0 role=registry_unmanaged task_status=done", text)
            self.assertIn("unstick=disabled:registry_unmanaged_done", text)
            self.assertIn("done-stale: task=old.md", text)

    def test_problems_only_reports_registry_unmanaged_running_stale_stuck_input_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"active.md","tmux_target":"vl:1.0","started_at_s":1}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: vl:2 codex\n(running)\n", encoding="utf-8")
            report = Report("stuck_input", ["› stale prompt"], "stale prompt", True)

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:1.0":
                    return report
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=vl:1.0 role=registry_unmanaged task_status=running", text)
            self.assertIn("unstick=disabled:registry_unmanaged_running", text)

    def test_problems_only_treats_registry_pane_sibling_as_stale_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text(
                '{"sessions":[{"task_file":"active.md","tmux_target":"vl:2.1","started_at_s":1}]}',
                encoding="utf-8",
            )
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 2\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: vl:2 codex\n(running)\n", encoding="utf-8")
            report = Report("stuck_input", ["› stale pane prompt"], "stale pane prompt", True)

            def fake_inspect(args: object) -> Report:
                target = getattr(args, "target")
                if target == "vl:2.1":
                    return report
                return Report("running", ["working"])

            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=vl:2.1 role=registry_unmanaged task_status=running", text)
            self.assertIn("unstick=disabled:registry_unmanaged_running", text)

    def test_problems_only_reports_todo_unmanaged_done_stuck_input_without_unstick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md vl 53\n", encoding="utf-8")
            _ = (root / "stale.md").write_text("runat: vl:53 codex\n(done)\n", encoding="utf-8")
            report = Report("stuck_input", ["› pasted prompt"], "pasted prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            unstick.assert_not_called()
            text = out.getvalue()
            self.assertIn("stuck_input: task=stale.md evidence=target=vl:53 role=todo_unmanaged task_status=done", text)
            self.assertIn("unstick=disabled:todo_unmanaged_done", text)

    def test_problems_only_reports_todo_unmanaged_capacity_error_without_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nstale.md vl 9\n", encoding="utf-8")
            _ = (root / "stale.md").write_text("runat: vl:9 codex\n(done)\n", encoding="utf-8")
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
            _ = (root / "old.md").write_text("runat: vl:23 codex\n(done)\n", encoding="utf-8")
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
            _ = (root / "active.md").write_text("runat: vl:2 codex\n(running)\n", encoding="utf-8")

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
            _ = (root / "pending.md").write_text("runat: vl:1 codex\n(pending)\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: vl:2 codex\n(running)\n", encoding="utf-8")

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
            _ = (root / "stale.md").write_text("runat: vl:53 codex\n(done)\n", encoding="utf-8")
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
            _ = (root / "stale.md").write_text("runat: cfg:53 codex\n(done)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=[]), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=stale.md evidence=target=cfg:53 role=todo_unmanaged task_status=done", text)

    def test_problems_only_reports_ready_unregistered_vl_pane_as_leaked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("previous:\nvl_old_review.md\n", encoding="utf-8")
            _ = (root / "vl_old_review.md").write_text("(done)\n", encoding="utf-8")
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", return_value=Report("ready", ["idle"])), redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("agent-problems: ready=1", text)
            self.assertIn("ready: task=tmux:vl:15 evidence=target=vl:15 role=tmux_unmanaged", text)
            self.assertNotIn("wl:2", text)

    def test_problems_only_uses_todo_target_for_unmanaged_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[{"task_file":"active.md","tmux_target":"vl:10.0","started_at_s":1}]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 10\n\nprevious:\nstale.md vl 9\n", encoding="utf-8")
            _ = (root / "active.md").write_text("runat: vl:10 codex\n(running)\n", encoding="utf-8")
            _ = (root / "stale.md").write_text("runat: vl:10 codex\n(done)\n", encoding="utf-8")

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
            _ = (root / "vl_supervisor_5410.md").write_text("runat: vl:20 codex\n(done)\n", encoding="utf-8")
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
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:0", "vl:15", "vl:20", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("not_codex: task=vl_supervisor_5410.md evidence=target=vl:20 role=todo_unmanaged", text)
            self.assertIn("stuck_input: task=tmux:vl:15 evidence=target=vl:15 role=tmux_unmanaged", text)
            self.assertIn("unstick=disabled:unregistered_tmux", text)
            self.assertNotIn("tmux:vl:0", text)
            self.assertNotIn("wl:2", text)
            unstick.assert_not_called()

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
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", side_effect=fake_inspect), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present") as unstick, redirect_stdout(out):
                self.assertEqual(3, main(["--root", str(root), "--registry", str(registry), "--problems-only"]))
            text = out.getvalue()
            self.assertIn("stuck_input: task=active.md evidence=target=vl:15 role=todo_current_untracked task_status=unlinked", text)
            self.assertIn("unstick=disabled:todo_current_untracked_unlinked", text)
            self.assertNotIn("wl:2", text)
            unstick.assert_not_called()

    def test_problems_only_keeps_explicit_vl_manager_target_out_of_tmux_unmanaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions.json"
            _ = registry.write_text('{"sessions":[]}', encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nactive.md vl 15\n", encoding="utf-8")
            report = Report("stuck_input", ["› manager prompt"], "manager prompt", True)
            out = StringIO()
            with patch("omo_manager.omo_agent_status.tmux_list_panes", return_value=["vl:15", "wl:2"]), patch("omo_manager.omo_agent_status.inspect", return_value=report), patch("omo_manager.omo_agent_status.submit_stuck_input_if_present", return_value="sent_enter") as unstick, redirect_stdout(out):
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
            _ = (root / "role.md").write_text("runat: cfg:1 codex\n(blocked: persistent VL supervisor role waiting for follow-up)\n", encoding="utf-8")
            _ = (root / "done.md").write_text("runat: cfg:2 codex\n(done)\n", encoding="utf-8")
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
            _ = (root / "done.md").write_text("runat: cfg:2 codex\nmanagerat: wl:17\n(done)\n", encoding="utf-8")
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
            _ = (root / "pending.md").write_text("runat: cfg:1 codex\n(pending)\n", encoding="utf-8")
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
            _ = (root / "pending.md").write_text("runat: vl:15 codex\n(pending)\n", encoding="utf-8")
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
            _ = (root / "done.md").write_text("runat: cfg:1 codex\nship feature\n- preserve human request\n(above are pending task items)\n(done)\n", encoding="utf-8")
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
            _ = (root / "done.md").write_text("runat: cfg:1 codex\nship feature\n- preserve human request\n(above are pending task items)\n(done)\n", encoding="utf-8")
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
            _ = (root / "done.md").write_text("runat: cfg:1 codex\nship feature\n- preserve human request\n(above are pending task items)\n(done)\n", encoding="utf-8")
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
            for name, status in (("running.md", "running"), ("pending.md", "pending"), ("blocked.md", "blocked")):
                _ = (root / name).write_text(f"runat: cfg:1 codex\nwork\n- keep item\n(above are pending task items)\n({status})\n", encoding="utf-8")
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
            _ = (root / "new.md").write_text("runat: cfg:1 codex\nwork\n- keep item\n(above are pending task items)\n", encoding="utf-8")
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
