import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager import email_idle_watcher
from omo_manager import omo_pending_watch


class DirectTmuxDeliveryCallerTests(unittest.TestCase):
    def test_pending_watcher_sender_submits_threaded_tmux_send(self) -> None:
        captured: dict[str, object] = {}

        def fake_require(target: str, n_lines: int = 80) -> None:
            captured["validated_target"] = target
            captured["validated_lines"] = n_lines

        def fake_submit(target: str, message: str, options: object, _pending_guard: object = None, _success_event: object = None, _failure_fallback: object = None) -> Future[None]:
            captured["target"] = target
            captured["message"] = message
            captured["options"] = options
            future: Future[None] = Future()
            future.set_result(None)
            return future

        with patch("omo_manager.omo_pending_watch.require_sendable_codex_target", side_effect=fake_require), patch("omo_manager.omo_pending_watch.submit_send", side_effect=fake_submit):
            omo_pending_watch.send_to_codex("cfg:1.0", "hello\n")

        self.assertEqual("cfg:1.0", captured["validated_target"])
        self.assertGreaterEqual(captured["validated_lines"], 80)
        self.assertEqual("cfg:1.0", captured["target"])
        self.assertFalse(captured["options"].dry_run)
        self.assertEqual("hello\n", captured["message"])

    def test_pending_delivery_calls_sender_directly_after_marker_check(self) -> None:
        calls = []

        def fake_send(target: str, text: str, options: object, **_kwargs: object) -> None:
            calls.append((target, text, options))

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_pending_watch.send_to_codex", side_effect=fake_send):
            root = Path(tmp)
            task = root / "task.md"
            task.write_text("(pending)\nbody\n", encoding="utf-8")
            status = omo_pending_watch.send_delivery_text(
                "pending delivery",
                "pending: file=task.md line=1",
                "cfg:1.0",
                root=root,
                pending_file=Path("task.md"),
                pending_line=1,
            )

        self.assertEqual(0, status)
        self.assertEqual("cfg:1.0", calls[0][0])
        self.assertEqual("pending: file=task.md line=1", calls[0][1])

    def test_pending_delivery_skips_when_marker_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_pending_watch.send_to_codex") as send:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text("(manager handled: done)\nbody\n", encoding="utf-8")
            status = omo_pending_watch.send_delivery_text(
                "pending delivery",
                "pending: file=task.md line=1",
                "cfg:1.0",
                root=root,
                pending_file=Path("task.md"),
                pending_line=1,
            )

        self.assertEqual(1, status)
        send.assert_not_called()

    def test_email_push_calls_sender_directly_after_marker_check(self) -> None:
        calls = []

        def fake_send(target: str, text: str, options: object) -> None:
            calls.append((target, text, options))

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.email_idle_watcher.send_to_codex", side_effect=fake_send):
            root = Path(tmp)
            manager = root / "work_manager_today.md"
            manager.write_text("(pending)\n(record and delegate manager_mail/1.txt)\n", encoding="utf-8")
            push = email_idle_watcher.EmailPush(
                1,
                "cfg:1.0",
                "pending: file=work_manager_today.md line=1 origin=human source=email action=ack-human",
                root,
                Path("work_manager_today.md"),
            )
            self.assertTrue(email_idle_watcher.run_email_push(push))

        self.assertEqual("cfg:1.0", calls[0][0])
        self.assertIn("origin=human", calls[0][1])

    def test_email_push_skips_when_marker_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.email_idle_watcher.send_to_codex") as send:
            root = Path(tmp)
            manager = root / "work_manager_today.md"
            manager.write_text("(manager handled: done)\n", encoding="utf-8")
            push = email_idle_watcher.EmailPush(1, "cfg:1.0", "pending: file=work_manager_today.md line=1", root, Path("work_manager_today.md"))
            self.assertFalse(email_idle_watcher.run_email_push(push))

        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
