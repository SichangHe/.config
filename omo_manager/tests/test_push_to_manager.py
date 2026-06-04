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

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_push_to_manager.subprocess.run", side_effect=fake_run):
            push_tmux(Args("pending: file=x.md line=1", "", "wl:1.0", Path(tmp), True, 5))

        self.assertEqual("omo_tmux_send.py", calls[0][0])
        self.assertIn("--enter", calls[0])
        self.assertIn("--enter-count", calls[0])
        self.assertIn("--ready-timeout-s", calls[0])
        self.assertEqual("2", calls[0][calls[0].index("--enter-count") + 1])
        self.assertEqual("300.0", calls[0][calls[0].index("--ready-timeout-s") + 1])


if __name__ == "__main__":
    unittest.main()
