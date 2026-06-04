import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_tmux_send import Args, parse_args, read_message, run_tmux, write_private_temp


class TmuxSendTests(unittest.TestCase):
    def test_read_message_file_preserves_special_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "message.txt"
            text = "literal $HOME `cmd` C-c ; newline\nsecond line\n"
            _ = path.write_text(text, encoding="utf-8")
            self.assertEqual(text, read_message(Args("cfg:1.0", path, 0, 0.15, False)))

    def test_private_temp_file_is_0600_and_preserves_text(self) -> None:
        path = write_private_temp("secret text\n")
        try:
            self.assertEqual("secret text\n", path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            path.unlink(missing_ok=True)

    def test_parse_enter_count_only_applies_with_enter(self) -> None:
        self.assertEqual(2, parse_args(["--target", "cfg:1.0", "--enter", "--enter-count", "2"]).enter_count)
        self.assertEqual(0, parse_args(["--target", "cfg:1.0", "--enter-count", "2"]).enter_count)

    def test_run_tmux_uses_buffer_and_enter_without_message_send_keys(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 1, 0.15, False), "literal C-c $(bad)\n")

        self.assertEqual("tmux", calls[0][0])
        self.assertEqual(["tmux", "load-buffer", "-b"], calls[0][:3])
        buffer_name = calls[0][3]
        self.assertEqual(["tmux", "paste-buffer", "-b", buffer_name, "-t", "cfg:1.0"], calls[1])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[2])
        self.assertEqual(["tmux", "delete-buffer", "-b", buffer_name], calls[3])
        self.assertNotIn("literal C-c $(bad)\n", [part for call in calls for part in call])

    def test_run_tmux_can_send_repeated_enter(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.time.sleep") as sleep, patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 2, 0.15, False), "prompt")

        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[2])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[3])
        self.assertEqual(["tmux", "delete-buffer", "-b", calls[0][3]], calls[4])
        sleep.assert_called_once_with(0.15)


if __name__ == "__main__":
    _ = unittest.main()
