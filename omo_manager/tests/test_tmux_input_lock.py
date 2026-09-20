from __future__ import annotations

import tempfile
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_tmux_input_lock import TmuxRuntimeBinding, capture_tmux_runtime_binding, guarded_tmux_runtime_command, require_same_tmux_runtime, tmux_input_lock, tmux_input_lock_path
from omo_manager.omo_tmux_send import CodexSendOptions, send_message_file_to_codex


class TmuxInputLockTests(unittest.TestCase):
    def test_canonical_pane_zero_uses_one_lock(self) -> None:
        self.assertEqual(tmux_input_lock_path("pb-newswatcher-agent:0"), tmux_input_lock_path("pb-newswatcher-agent:0.0"))
        self.assertNotEqual(tmux_input_lock_path("pb-newswatcher-agent:0"), tmux_input_lock_path("pb-newswatcher-agent:1"))

    def test_complete_runtime_capture_binds_cwd_and_process_start_ticks(self) -> None:
        result = subprocess.CompletedProcess(
            ["tmux"],
            0,
            "cfg:1.0\t%7\t@1\t4242\tbunx\t/workspace\n",
            "",
        )
        with patch("omo_manager.omo_tmux_input_lock.subprocess.run", return_value=result), patch(
            "omo_manager.omo_tmux_input_lock.process_start_ticks", return_value=7001
        ):
            self.assertEqual(
                TmuxRuntimeBinding("cfg:1.0", "%7", "@1", 4242, "bunx", Path("/workspace"), 7001),
                capture_tmux_runtime_binding("cfg:1.0"),
            )

    def test_runtime_guard_includes_every_tmux_field_and_kernel_start_tick(self) -> None:
        runtime = TmuxRuntimeBinding("cfg:1.0", "%7", "@1", 4242, "bunx", Path("/workspace"), 7001)
        command = guarded_tmux_runtime_command(runtime, "send-keys -t %7 Enter")
        self.assertEqual(["tmux", "if-shell", "-F", "-t", "%7"], command[:5])
        for fragment in ("#{pane_id},%7", "#{window_id},@1", "cfg:1.0", "#{pane_pid},4242", "#{pane_current_command},bunx", "#{pane_current_path},/workspace"):
            self.assertIn(fragment, command[5])
        self.assertIn("/proc/4242/stat", command[6])
        self.assertIn('test "${20:-}" = 7001', command[6])
        self.assertIn("send-keys -t %7 Enter", command[6])

    def test_runtime_revalidation_rejects_same_pid_with_new_start_ticks(self) -> None:
        original = TmuxRuntimeBinding("cfg:1.0", "%7", "@1", 4242, "bunx", Path("/workspace"), 7001)
        reused = TmuxRuntimeBinding("cfg:1.0", "%7", "@1", 4242, "bunx", Path("/workspace"), 7002)
        with patch("omo_manager.omo_tmux_input_lock.capture_tmux_runtime_binding", return_value=reused), self.assertRaisesRegex(
            RuntimeError, "identity changed"
        ):
            require_same_tmux_runtime(original)

    def test_same_thread_nested_alias_uses_one_underlying_lock(self) -> None:
        acquisitions: list[Path] = []

        @contextmanager
        def lock(path: Path):
            acquisitions.append(path)
            yield

        with patch("omo_manager.omo_tmux_input_lock.task_file_lock_at_path", side_effect=lock):
            with tmux_input_lock("cfg:1"):
                with tmux_input_lock("cfg:1.0"):
                    pass

        self.assertEqual([tmux_input_lock_path("cfg:1")], acquisitions)

    def test_normal_sender_holds_target_input_lock_while_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            message = Path(tmp) / "message.txt"
            message.write_text("hello\n", encoding="utf-8")
            target = "pb-newswatcher-agent:0.0"
            held = False

            @contextmanager
            def lock(actual_target: str):
                nonlocal held
                self.assertEqual(target, actual_target)
                held = True
                try:
                    yield
                finally:
                    held = False

            def send(actual_target: str, _payload: str, _options: CodexSendOptions, **_kwargs: object) -> None:
                self.assertTrue(held)
                self.assertEqual(target, actual_target)

            with (
                patch("omo_manager.omo_tmux_send.tmux_input_lock", side_effect=lock),
                patch("omo_manager.omo_tmux_send._run_tmux_payload", side_effect=send) as sender,
                patch("omo_manager.omo_tmux_send.secrets.randbelow", return_value=1),
            ):
                send_message_file_to_codex(target, message, CodexSendOptions(1, 0.15, False))
            sender.assert_called_once()
            self.assertFalse(held)


if __name__ == "__main__":
    unittest.main()
