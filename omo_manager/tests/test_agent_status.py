import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import Args, classify_task, load_local_env, load_task_state, parse_task_lines, registry_prune, session_records
from omo_manager.omo_agent_status import SessionRecord, TaskLine
from omo_manager.omo_codex_status import Report


class AgentStatusTests(unittest.TestCase):
    def test_load_task_state_prefers_reopened_active_over_stale_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n\nprevious:\ndone.md cfg 2 (done)\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Complete, delivered to human\n- `active.md` complete.\n", encoding="utf-8")
            current, done, _human_pending = load_task_state(root)
            self.assertIn("active.md", current)
            self.assertNotIn("active.md", done)
            self.assertIn("done.md", done)

    def test_load_task_state_ignores_legacy_manager_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nactive.md cfg 1\n", encoding="utf-8")
            _ = (root / "MANAGER_TRACKER.md").write_text("## Active / waiting\n- `tracker.md` (`pb:4`, port `18941`): running\n", encoding="utf-8")
            current, done, human_pending = load_task_state(root)
            self.assertEqual({"active.md"}, set(current))
            self.assertEqual(set(), done)
            self.assertEqual(set(), human_pending)

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
            current, done, _human_pending = load_task_state(root)
            self.assertIn("active.md", current)
            self.assertEqual({"done.md"}, done)
            removed = registry_prune(Args(root, registry, True), done)
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


if __name__ == "__main__":
    _ = unittest.main()
