from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import amh_cursor_agent as cursor


class CursorAgentPilotTests(unittest.TestCase):
    def test_command_uses_explicit_model_effort_and_unattended_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Review this change.\n", encoding="utf-8")
            args = cursor.parse_args(
                [
                    "--workspace",
                    str(root),
                    "--prompt-file",
                    str(prompt),
                    "--model",
                    "gpt-5.6-terra",
                    "--reasoning-effort",
                    "low",
                    "--timeout-s",
                    "60",
                ]
            )

            command = cursor.command(args, "/bin/agent")

            self.assertEqual("/bin/agent", command[0])
            self.assertIn("--print", command)
            self.assertIn("--force", command)
            self.assertIn("disabled", command)
            self.assertEqual("gpt-5.6-terra-low", command[command.index("--model") + 1])
            self.assertEqual("Review this change.\n", command[-1])

    def test_resume_uses_returned_cursor_session_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Continue.\n", encoding="utf-8")
            session_id = "920c5fd9-01ab-4bcf-aa9e-eead25bb0247"
            args = cursor.parse_args(
                [
                    "--workspace",
                    str(root),
                    "--prompt-file",
                    str(prompt),
                    "--model",
                    "gpt-5.6-terra",
                    "--reasoning-effort",
                    "low",
                    "--timeout-s",
                    "60",
                    "--resume",
                    session_id,
                ]
            )

            command = cursor.command(args, "/bin/agent")

            self.assertEqual(session_id, command[command.index("--resume") + 1])

    def test_run_returns_compact_success_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Reply.\n", encoding="utf-8")
            args = cursor.Args(root, prompt, "Reply.\n", "gpt-5.6-terra", "low", 60)
            payload = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "session_id": "920c5fd9-01ab-4bcf-aa9e-eead25bb0247",
            }

            with patch.object(cursor.shutil, "which", return_value="/bin/agent"), patch.object(
                cursor,
                "execute",
                return_value=cursor.subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
            ), patch("builtins.print") as output:
                self.assertEqual(0, cursor.run(args))

            self.assertIn('"schema":"amh-cursor-agent/v1","ok":true', output.call_args.args[0])
            self.assertIn('"session_id":"920c5fd9-01ab-4bcf-aa9e-eead25bb0247"', output.call_args.args[0])

    def test_run_returns_structured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Reply.\n", encoding="utf-8")
            args = cursor.Args(root, prompt, "Reply.\n", "gpt-5.6-terra", "low", 1)

            with patch.object(cursor.shutil, "which", return_value="/bin/agent"), patch.object(
                cursor,
                "execute",
                side_effect=cursor.subprocess.TimeoutExpired([], 1),
            ), patch("builtins.print") as output:
                self.assertEqual(124, cursor.run(args))

            self.assertIn('"ok":false', output.call_args.args[0])
            self.assertIn("timeout", output.call_args.args[0])

    def test_run_rejects_success_without_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Reply.\n", encoding="utf-8")
            args = cursor.Args(root, prompt, "Reply.\n", "gpt-5.6-terra", "low", 60)
            payload = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}

            with patch.object(cursor.shutil, "which", return_value="/bin/agent"), patch.object(
                cursor,
                "execute",
                return_value=cursor.subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
            ), patch("builtins.print") as output:
                self.assertEqual(1, cursor.run(args))

            self.assertIn('"ok":false', output.call_args.args[0])

    def test_timeout_stops_descendant_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child_pid_file = Path(tmp) / "child.pid"
            source = (
                "import pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
            )

            with self.assertRaises(cursor.subprocess.TimeoutExpired):
                cursor.execute([sys.executable, "-c", source, str(child_pid_file)], 0.2)

            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            for _ in range(20):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.05)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_timeout_stops_descendant_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child_pid_file = Path(tmp) / "child.pid"
            source = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
            )
            started_s = time.monotonic()

            with self.assertRaises(cursor.subprocess.TimeoutExpired):
                cursor.execute([sys.executable, "-c", source, str(child_pid_file)], 0.2)

            self.assertLess(time.monotonic() - started_s, 3)
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            for _ in range(20):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.05)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_timeout_must_be_finite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.txt"
            prompt.write_text("Reply.\n", encoding="utf-8")
            with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
                cursor.parse_args(
                    [
                        "--workspace",
                        str(root),
                        "--prompt-file",
                        str(prompt),
                        "--model",
                        "gpt-5.6-terra",
                        "--reasoning-effort",
                        "low",
                        "--timeout-s",
                        "inf",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
