from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


OMO_DIR = Path(__file__).resolve().parents[1]


def helper_help(name: str, *args: str) -> str:
    path = OMO_DIR / name
    command = [str(path), *args, "--help"] if path.suffix == ".sh" else [sys.executable, str(path), *args, "--help"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return " ".join(result.stdout.split())


class ManagerHelperHelpTests(unittest.TestCase):
    def test_pending_ingress_help_owns_routing_details(self) -> None:
        record_help = helper_help("omo_record_pending.py")
        self.assertIn("atomically", record_help)
        self.assertIn("original subject", record_help)
        self.assertIn("initial assignment", record_help)
        self.assertIn("worker prompts", record_help)

        move_help = helper_help("omo_task_edit.py", "pending-move")
        self.assertIn("initial owner", move_help)
        self.assertIn("initial routing", move_help)

    def test_launch_and_lifecycle_help_owns_operating_details(self) -> None:
        launch_help = helper_help("omo_task.py")
        self.assertIn("Every new launch requires --model and --reasoning-effort", launch_help)
        self.assertIn("create or update task frontmatter", launch_help)
        self.assertIn("link the task in TODO.md unless --no-link", launch_help)
        self.assertIn("open a tmux window with its normal shell", launch_help)
        self.assertIn("--prompt-file becomes the worker's initial prompt argument", launch_help)
        self.assertIn("start Cursor Agent there unless --tool codex", launch_help)
        self.assertIn("WORKER_DEFAULTS.md", launch_help)
        self.assertIn("gpt-5.6-sol medium is the default", launch_help)
        self.assertIn("cursor-grok-4.6-xhigh", launch_help)
        self.assertIn("Keep --task-file as manager-side bookkeeping", launch_help)

        cursor_help = helper_help("amh_cursor_agent.py")
        self.assertIn("cursor-grok-4.6-xhigh", cursor_help)

        status_help = helper_help("omo_task_status.py")
        self.assertIn("live `(pending)` marker", status_help)
        self.assertIn("TODO movement and worker shutdown", status_help)

        agent_status_help = helper_help("omo_agent_status.py")
        self.assertIn("reads task links from TODO.md", agent_status_help)
        self.assertIn("frontmatter as authoritative status", agent_status_help)
        self.assertIn("--problems-only --no-auto-unstick for a read-only", agent_status_help)

    def test_watch_and_pane_help_owns_safe_usage_details(self) -> None:
        watch_help = helper_help("omo_pending_watch.py")
        self.assertIn("without notifying targets", watch_help)
        self.assertIn("combine with --once", watch_help)

        compact_help = helper_help("omo_codex_compact_when_idle.py")
        self.assertIn("Different tmux target", compact_help)

        stop_help = helper_help("omo_codex_stop.py")
        self.assertIn("lower-level stop helper", stop_help)
        self.assertIn("normal task closure", stop_help)

        delegate_help = helper_help("omo_task_edit.py", "delegate-message")
        self.assertIn("delivery by omo_pending_watch.py", delegate_help)

    def test_report_help_owns_file_and_routing_details(self) -> None:
        report_help = helper_help("omo_report.sh")
        self.assertIn("private task-specific draft", report_help)
        self.assertIn("infers routing from the producer pane", report_help)
        self.assertIn("do not pass task-file, root, manager-target", report_help)

    def test_ops_manager_cursor_replace_help_owns_pin_and_fail_closed_details(self) -> None:
        replace_help = helper_help("omo_ops_manager_cursor_replace.py")
        self.assertIn("ops_manager.md", replace_help)
        self.assertIn("wl:3", replace_help)
        self.assertIn("managerat: wl:18", replace_help)
        self.assertIn("h*", replace_help)
        self.assertIn("does not launch a replacement pane", replace_help)
        self.assertIn("pending queue", replace_help)
        self.assertIn("dirty unknown", replace_help)
        self.assertIn("17-17", replace_help)


if __name__ == "__main__":
    _ = unittest.main()
