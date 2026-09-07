from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager.omo_tmux_input_lock import tmux_input_lock_path
from omo_manager.omo_tmux_send import main


class TmuxInputLockTests(unittest.TestCase):
    def test_canonical_pane_zero_uses_one_lock(self) -> None:
        self.assertEqual(tmux_input_lock_path("pb-newswatcher-agent:0"), tmux_input_lock_path("pb-newswatcher-agent:0.0"))
        self.assertNotEqual(tmux_input_lock_path("pb-newswatcher-agent:0"), tmux_input_lock_path("pb-newswatcher-agent:1"))

    def test_normal_sender_holds_target_input_lock_while_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            message = Path(tmp) / "message.txt"
            message.write_text("hello\n", encoding="utf-8")
            args = SimpleNamespace(
                async_result="",
                submit_existing_file=None,
                submit_existing_sha256="",
                cancel_existing_file=None,
                cancel_existing_sha256="",
                async_mode=False,
                async_worker=False,
                message_file=message,
                target="pb-newswatcher-agent:0.0",
                options=object(),
            )
            held = False

            @contextmanager
            def lock(target: str):
                nonlocal held
                self.assertEqual(args.target, target)
                held = True
                try:
                    yield
                finally:
                    held = False

            def send(target: str, path: Path, _options: object) -> None:
                self.assertTrue(held)
                self.assertEqual(args.target, target)
                self.assertEqual(message, path)

            with (
                patch("omo_manager.omo_tmux_send.parse_args", return_value=args),
                patch("omo_manager.omo_tmux_send.tmux_input_lock", side_effect=lock),
                patch("omo_manager.omo_tmux_send.send_message_file_to_codex", side_effect=send) as sender,
            ):
                self.assertEqual(0, main([]))
            sender.assert_called_once()
            self.assertFalse(held)


if __name__ == "__main__":
    unittest.main()
