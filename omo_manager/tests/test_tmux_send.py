import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_tmux_send import (
    Args,
    current_input_text,
    input_has_probe,
    launch_async,
    message_probe,
    parse_args,
    pending_marker_present,
    read_message,
    run_async_worker,
    run_tmux,
    verify_submit,
    wait_ready,
    write_private_temp,
)


class TmuxSendTests(unittest.TestCase):
    def test_read_message_file_preserves_special_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "message.txt"
            text = "literal $HOME `cmd` C-c ; newline\nsecond line\n"
            _ = path.write_text(text, encoding="utf-8")
            self.assertEqual(text, read_message(Args("cfg:1.0", path, 0, 0.15, 0, False)))

    def test_read_message_requires_file(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--message-file is required"):
            read_message(Args("cfg:1.0", None, 0, 0.15, 0, False))

    def test_private_temp_file_is_0600_and_preserves_text(self) -> None:
        path = write_private_temp("secret text\n")
        try:
            self.assertEqual("secret text\n", path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            path.unlink(missing_ok=True)

    def test_parse_enter_count_only_applies_with_enter(self) -> None:
        base = ["--target", "cfg:1.0", "--message-file", "prompt.md"]
        self.assertEqual(2, parse_args([*base, "--enter", "--enter-count", "2"]).enter_count)
        self.assertEqual(0, parse_args([*base, "--enter-count", "2"]).enter_count)
        self.assertEqual(3, parse_args([*base, "--enter", "--ready-timeout-s", "3"]).ready_timeout_s)
        self.assertEqual(0, parse_args([*base, "--ready-timeout-s", "3"]).ready_timeout_s)
        self.assertEqual(5, parse_args([*base, "--enter"]).submit_verify_timeout_s)
        self.assertEqual(0, parse_args([*base, "--submit-verify-timeout-s", "3"]).submit_verify_timeout_s)
        self.assertEqual(3, parse_args([*base, "--enter", "--submit-verify-timeout-s", "3"]).submit_verify_timeout_s)

    def test_parse_async_requires_notify_target(self) -> None:
        with patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit):
            parse_args(["--target", "cfg:1.0", "--message-file", "prompt.md", "--async"])
        args = parse_args(
            [
                "--target",
                "cfg:1.0",
                "--message-file",
                "prompt.md",
                "--async",
                "--async-notify-target",
                "cfg:0.0",
                "--async-notify-enter-count",
                "0",
            ]
        )
        self.assertTrue(args.async_mode)
        self.assertEqual("cfg:0.0", args.async_notify_target)
        self.assertEqual(0, args.async_notify_enter_count)

    def test_pending_guard_rechecks_after_ready_wait_before_paste(self) -> None:
        calls: list[list[str]] = []

        def fake_wait(args: Args) -> None:
            assert args.pending_root is not None and args.pending_file is not None
            (args.pending_root / args.pending_file).write_text("(manager handled: done.)\nsource\n", encoding="utf-8")

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.wait_ready", side_effect=fake_wait), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nsource\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "pending marker cleared"):
                run_tmux(Args("cfg:1.0", None, 1, 0.15, 10, False, root, Path("task.md"), 1, ""), "pending: file=task.md line=1")

        self.assertEqual([], [call for call in calls if call[:2] != ["tmux", "delete-buffer"]])

    def test_pending_guard_matches_digest_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nsource\n", encoding="utf-8")
            digest = hashlib.sha256("task.md:1:source".encode("utf-8")).hexdigest()[:16]
            self.assertTrue(pending_marker_present(Args("cfg:1.0", None, 0, 0.15, 0, False, root, Path("task.md"), 1, digest)))
            self.assertFalse(pending_marker_present(Args("cfg:1.0", None, 0, 0.15, 0, False, root, Path("task.md"), 1, "bad")))

    def test_run_tmux_uses_buffer_and_enter_without_message_send_keys(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 1, 0.15, 0, False), "literal C-c $(bad)\n")

        self.assertEqual("tmux", calls[0][0])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "C-u"], calls[0])
        self.assertEqual(["tmux", "load-buffer", "-b"], calls[1][:3])
        buffer_name = calls[1][3]
        self.assertEqual(["tmux", "paste-buffer", "-b", buffer_name, "-t", "cfg:1.0"], calls[2])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[3])
        self.assertEqual(["tmux", "delete-buffer", "-b", buffer_name], calls[4])
        self.assertNotIn("literal C-c $(bad)\n", [part for call in calls for part in call])

    def test_run_tmux_can_send_repeated_enter(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.time.sleep") as sleep, patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 2, 0.15, 0, False), "prompt")

        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "C-u"], calls[0])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[3])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[4])
        self.assertEqual(["tmux", "delete-buffer", "-b", calls[1][3]], calls[5])
        sleep.assert_called_once_with(0.15)

    def test_launch_async_copies_payload_and_starts_worker(self) -> None:
        started: list[list[str]] = []

        class Proc:
            pid = 1234

        def fake_popen(command: list[str], **_: object) -> Proc:
            started.append(command)
            return Proc()

        with patch("omo_manager.omo_tmux_send.subprocess.Popen", side_effect=fake_popen), patch("sys.stdout", new_callable=StringIO):
            launch_async(
                Args(
                    "cfg:1.0",
                    None,
                    2,
                    0.2,
                    30,
                    False,
                    async_mode=True,
                    async_notify_target="cfg:0.0",
                    async_notify_enter_count=1,
                ),
                "literal $HOME\n",
            )

        self.assertEqual(1, len(started))
        command = started[0]
        self.assertIn("--async-worker", command)
        self.assertNotIn("--async", command)
        payload_path = Path(command[command.index("--message-file") + 1])
        try:
            self.assertEqual("literal $HOME\n", payload_path.read_text(encoding="utf-8"))
            self.assertIn("--enter", command)
            self.assertEqual("cfg:0.0", command[command.index("--async-notify-target") + 1])
        finally:
            payload_path.unlink(missing_ok=True)

    def test_async_worker_notifies_success_and_cleans_payload(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_tmux(args: Args, message: str) -> None:
            calls.append((args.target, message))

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.run_tmux", side_effect=fake_run_tmux):
            payload = Path(tmp) / "payload.txt"
            payload.write_text("/compact\n", encoding="utf-8")
            rc = run_async_worker(
                Args(
                    "cfg:1.0",
                    payload,
                    1,
                    0.15,
                    0,
                    False,
                    async_notify_target="cfg:0.0",
                    async_cleanup_message_file=True,
                )
            )

        self.assertEqual(0, rc)
        self.assertEqual(("cfg:1.0", "/compact\n"), calls[0])
        self.assertEqual("cfg:0.0", calls[1][0])
        self.assertIn("succeeded", calls[1][1])
        self.assertIn("Result: sent", calls[1][1])
        self.assertFalse(payload.exists())

    def test_async_worker_notifies_failure_details(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_tmux(args: Args, message: str) -> None:
            if args.target == "cfg:1.0":
                raise RuntimeError("target not ready")
            calls.append((args.target, message))

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.run_tmux", side_effect=fake_run_tmux):
            payload = Path(tmp) / "payload.txt"
            payload.write_text("/compact\n", encoding="utf-8")
            rc = run_async_worker(
                Args(
                    "cfg:1.0",
                    payload,
                    1,
                    0.15,
                    0,
                    False,
                    async_notify_target="cfg:0.0",
                    async_cleanup_message_file=True,
                )
            )

        self.assertEqual(1, rc)
        self.assertEqual(1, len(calls))
        self.assertEqual("cfg:0.0", calls[0][0])
        self.assertIn("failed", calls[0][1])
        self.assertIn("target not ready", calls[0][1])

    def test_wait_ready_waits_through_running_codex(self) -> None:
        seen: list[int] = []
        tails = iter([["• Working", "  gpt-5.5"], ["› Use /skills to list available skills", "  gpt-5.5"]])

        def fake_tail(_: str, __: int) -> list[str]:
            seen.append(1)
            return next(tails)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.time.sleep"):
            wait_ready(Args("cfg:1.0", None, 1, 0.15, 1, False))

        self.assertEqual(2, len(seen))

    def test_input_probe_matches_codex_input_only(self) -> None:
        self.assertEqual("Read the dispatch prompt", message_probe("\nRead the dispatch prompt\nmore"))
        self.assertTrue(input_has_probe(["› Read the dispatch prompt", "  gpt-5.5"], "Read the dispatch prompt"))
        self.assertFalse(input_has_probe(["› Older submitted prompt", "• Working", "  gpt-5.5"], "Older submitted prompt"))
        self.assertFalse(input_has_probe(["Read the dispatch prompt", "  gpt-5.5"], "Read the dispatch prompt"))
        self.assertEqual("", current_input_text(["› Use /skills to list available skills", "  gpt-5.5"]))
        self.assertEqual("Write tests for @filename", current_input_text(["• Working (1m 59s • esc to interrupt)", "", "› Write tests for @filename", "", "  gpt-5.5"]))
        multi_line_input = [
            "• Working (1m 59s • esc to interrupt)",
            "",
            "› Run `~/.config/getagentsmd` first.",
            "  Continue `x.md`.",
            "  - Report back.",
            "",
            "  gpt-5.5",
        ]
        self.assertEqual("Run `~/.config/getagentsmd` first.\n  Continue `x.md`.\n  - Report back.", current_input_text(multi_line_input))

    def test_verify_submit_sends_fallback_enter_when_prompt_remains_in_input(self) -> None:
        calls: list[list[str]] = []
        tails = iter([["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"], ["• Working", "  gpt-5.5"]])

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.sleep"):
            verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        self.assertEqual([["tmux", "send-keys", "-t", "cfg:1.0", "Enter"]], calls)

    def test_verify_submit_fails_when_prompt_stays_in_input(self) -> None:
        def fake_tail(_: str, __: int) -> list[str]:
            return ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"]

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "Codex submit not verified"):
                verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

    def test_verify_submit_fails_when_running_input_box_still_has_other_text(self) -> None:
        calls: list[list[str]] = []

        def fake_tail(_: str, __: int) -> list[str]:
            return ["• Working (1m 59s • esc to interrupt)", "", "› Write tests for @filename", "", "  gpt-5.5"]

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "input box still has text"):
                verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        self.assertEqual([["tmux", "send-keys", "-t", "cfg:1.0", "Enter"]], calls)


if __name__ == "__main__":
    _ = unittest.main()
