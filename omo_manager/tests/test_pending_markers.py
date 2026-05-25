from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.email_idle_watcher import append_pending, existing_source_pending_line
from omo_manager.omo_pending_watch import Args, find_markers


class PendingMarkerTests(unittest.TestCase):
    def test_email_pending_block_has_new_and_legacy_markers_and_is_not_generic_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            line = append_pending(root, mail)
            self.assertEqual(line, existing_source_pending_line(root, mail))
            text = (root / "work_manager.md").read_text(encoding="utf-8")
            self.assertIn("(from email manager_mail/4002.txt)", text)
            self.assertIn("[source: email manager_mail/4002.txt]", text)
            markers = find_markers(root, [root / "work_manager.md"])
            self.assertEqual(1, len(markers))

    def test_legacy_email_source_block_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            _ = path.write_text("(pending)\n[source: email manager_mail/3979.txt]\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))

    def test_omo_report_writes_agent_pending_source_without_direct_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            duplicate_msg = Path(tmp) / "duplicate-msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            _ = duplicate_msg.write_text("done\n", encoding="utf-8")
            for message_file in (msg, duplicate_msg):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-4002",
                        "--message-file",
                        str(message_file),
                    ],
                    cwd=tmp,
                    env={**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            task = root / "task.md"
            text = task.read_text(encoding="utf-8")
            self.assertIn("(pending)\n(from agent agent-4002 via omo_report.sh status=done)\n", text)
            self.assertEqual(1, text.count("(from agent agent-4002 via omo_report.sh status=done)"))
            self.assertIn("[message-sha256: ", text)
            self.assertIn("message:\n> done\n", text)
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))

    def test_omo_report_concurrent_distinct_reports_keep_all_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            messages = []
            for idx in range(12):
                msg = Path(tmp) / f"msg-{idx}.md"
                _ = msg.write_text(f"distinct report {idx}\n", encoding="utf-8")
                messages.append(msg)
            env = {**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
            procs = [
                subprocess.Popen(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-4002",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for msg in messages
            ]
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual("", stdout)
                self.assertEqual("", stderr)
                self.assertEqual(0, proc.returncode)
            task = root / "task.md"
            text = task.read_text(encoding="utf-8")
            self.assertEqual(12, text.count("(from agent agent-4002 via omo_report.sh status=done)"))
            for idx in range(12):
                self.assertIn(f"message:\n> distinct report {idx}\n", text)
            self.assertEqual(12, len(find_markers(root, [task])))

    def test_new_email_source_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            _ = path.write_text("(pending)\n(from email manager_mail/4002.txt)\n[source: email manager_mail/4002.txt]\n", encoding="utf-8")
            seen: dict[str, float] = {}
            args = Args(root=root, manager_url="", state=Path(tmp) / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, once=True, dry_run=True)
            from omo_manager.omo_pending_watch import scan_once

            with redirect_stdout(StringIO()):
                self.assertTrue(scan_once(args, seen, [path]))


if __name__ == "__main__":
    _ = unittest.main()
