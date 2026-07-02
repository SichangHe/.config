from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_push_to_manager import Args, push_tmux


class PushToManagerTests(unittest.TestCase):
    def test_tmux_submit_waits_ready_and_sends_repeated_enter(self) -> None:
        calls: list[list[str]] = []
        timeouts: list[object] = []
        envs: list[dict[str, str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            timeouts.append(kwargs.get("timeout"))
            env = kwargs.get("env")
            self.assertIsInstance(env, dict)
            envs.append(env)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_push_to_manager.subprocess.run", side_effect=fake_run):
            push_tmux(Args("pending: file=x.md line=1", "", "wl:1.0", Path(tmp), True, 5))

        self.assertEqual("omo_tmux_send.py", calls[0][0])
        self.assertIn("--enter", calls[0])
        self.assertIn("--enter-count", calls[0])
        self.assertIn("--ready-timeout-s", calls[0])
        self.assertIn("--submit-verify-timeout-s", calls[0])
        self.assertIn("--allow-plan-prompt-enter", calls[0])
        self.assertEqual("2", calls[0][calls[0].index("--enter-count") + 1])
        self.assertEqual("300.0", calls[0][calls[0].index("--ready-timeout-s") + 1])
        self.assertEqual("5.0", calls[0][calls[0].index("--submit-verify-timeout-s") + 1])
        self.assertEqual([625.0], timeouts)
        self.assertEqual("300.0", envs[0]["OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S"])

    def test_tmux_submit_preserves_existing_compaction_timeout_env(self) -> None:
        envs: list[dict[str, str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs.get("env")
            self.assertIsInstance(env, dict)
            envs.append(env)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S": "7"}), patch("omo_manager.omo_push_to_manager.subprocess.run", side_effect=fake_run):
            push_tmux(Args("pending: file=x.md line=1", "", "wl:1.0", Path(tmp), True, 5))

        self.assertEqual("7", envs[0]["OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S"])

    def test_tmux_pending_guard_is_forwarded_to_sender(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_push_to_manager.subprocess.run", side_effect=fake_run):
            push_tmux(Args("pending: file=x.md line=1", "", "wl:1.0", Path(tmp), True, 5, Path("x.md"), 1, "abc"))

        self.assertIn("--pending-root", calls[0])
        self.assertEqual(str(Path(tmp)), calls[0][calls[0].index("--pending-root") + 1])
        self.assertEqual("x.md", calls[0][calls[0].index("--pending-file") + 1])
        self.assertEqual("1", calls[0][calls[0].index("--pending-line") + 1])
        self.assertEqual("abc", calls[0][calls[0].index("--pending-digest") + 1])


if __name__ == "__main__":
    unittest.main()
