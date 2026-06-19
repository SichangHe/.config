import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_status import Report
from omo_manager.omo_tmux_send import (
    Args,
    clear_stuck_input_before_send,
    current_input_text,
    input_has_probe,
    launch_async,
    message_probe,
    message_probes,
    parse_args,
    pending_marker_present,
    read_message,
    run_async_worker,
    run_tmux,
    verify_submit,
    wait_paste_visible,
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

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.wait_ready", side_effect=fake_wait), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
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

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 1, 0.15, 0, False), "literal C-c $(bad)\n")

        self.assertEqual("tmux", calls[0][0])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "C-u"], calls[0])
        self.assertEqual(["tmux", "load-buffer", "-b"], calls[1][:3])
        buffer_name = calls[1][3]
        self.assertEqual(["tmux", "paste-buffer", "-b", buffer_name, "-t", "cfg:1.0"], calls[2])
        self.assertEqual(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls[3])
        self.assertEqual(["tmux", "delete-buffer", "-b", buffer_name], calls[4])
        self.assertNotIn("literal C-c $(bad)\n", [part for call in calls for part in call])

    def test_run_tmux_waits_for_compaction_before_paste_and_enter(self) -> None:
        events: list[str] = []

        def fake_wait(_: Args, __: int = 80) -> None:
            events.append("wait")

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            events.append(command[1])
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send", side_effect=fake_wait), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 1, 0.15, 0, False), "prompt")

        load_idx = events.index("load-buffer")
        paste_idx = events.index("paste-buffer")
        enter_idx = events.index("send-keys", paste_idx)
        self.assertEqual(["wait", "wait"], events[:2])
        self.assertEqual("wait", events[enter_idx - 1])
        self.assertLess(load_idx, paste_idx)

    def test_run_tmux_can_send_repeated_enter(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send") as wait_compaction, patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.time.sleep") as sleep, patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 2, 0.15, 0, False), "prompt")

        self.assertEqual(4, wait_compaction.call_count)
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
        self.assertEqual(["first", "last"], message_probes("first\nmiddle\nlast\n"))
        self.assertTrue(input_has_probe(["› Read the dispatch prompt", "  gpt-5.5"], "Read the dispatch prompt"))
        self.assertFalse(input_has_probe(["› Older submitted prompt", "• Working", "  gpt-5.5"], "Older submitted prompt"))
        self.assertFalse(input_has_probe(["Read the dispatch prompt", "  gpt-5.5"], "Read the dispatch prompt"))
        self.assertEqual("Use /skills to list available skills", current_input_text(["› Use /skills to list available skills", "  gpt-5.5"]))
        self.assertEqual("Summarize recent commits", current_input_text(["› Summarize recent commits", "  gpt-5.5"]))
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

    def test_verify_submit_retries_enter_until_prompt_clears(self) -> None:
        calls: list[list[str]] = []
        tails = iter(
            [
                ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["• Working", "  gpt-5.5"],
            ]
        )

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0.0, 0.0, 0.3]), patch("omo_manager.omo_tmux_send.time.sleep"):
            verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        self.assertEqual(
            [
                ["tmux", "send-keys", "-t", "cfg:1.0", "Enter"],
                ["tmux", "send-keys", "-t", "cfg:1.0", "Enter"],
            ],
            calls,
        )

    def test_wait_paste_visible_requires_prompt_before_enter(self) -> None:
        tails = iter([["›", "  gpt-5.5"], ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"]])

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0.0, 0.1]), patch("omo_manager.omo_tmux_send.time.sleep") as sleep:
            wait_paste_visible(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        sleep.assert_called_once()

    def test_wait_paste_visible_waits_through_compacting_prompt(self) -> None:
        tails = iter(
            [
                ["• Compacting conversation", "", "› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
            ]
        )

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send") as wait_compaction:
            wait_paste_visible(Args("cfg:1.0", None, 1, 0.15, 10, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        wait_compaction.assert_called_once()

    def test_wait_paste_visible_does_not_accept_codex_suggestion_for_different_prompt(self) -> None:
        def fake_tail(_: str, __: int) -> list[str]:
            return ["• Working (1m 59s • esc to interrupt)", "", "› Summarize recent commits", "", "  gpt-5.5"]

        prompts = ("Summarize\nwith details\n", "Summarize recent commits\n")
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
                    with self.assertRaisesRegex(RuntimeError, "prompt not visible"):
                        wait_paste_visible(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), prompt)

    def test_wait_paste_visible_checks_trailing_probe_for_long_message(self) -> None:
        long_message = "\n".join(["first line", *[f"middle {idx}" for idx in range(100)], "last line"]) + "\n"
        tails = iter([["› last line", "  gpt-5.5"]])
        seen_n_lines: list[int] = []

        def fake_tail(_: str, n_lines: int) -> list[str]:
            seen_n_lines.append(n_lines)
            return next(tails)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail):
            wait_paste_visible(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), long_message)

        self.assertEqual([122], seen_n_lines)

    def test_verify_submit_fails_when_prompt_stays_in_input(self) -> None:
        def fake_tail(_: str, __: int) -> list[str]:
            return ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"]

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "Codex submit not verified"):
                verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

    def test_verify_submit_waits_for_compaction_before_fallback_enter(self) -> None:
        calls: list[tuple[list[str], int]] = []
        n_captures = 0
        tails = iter(
            [
                ["• Compacting conversation", "", "› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["• Working", "  gpt-5.5"],
            ]
        )

        def fake_tail(_: str, __: int) -> list[str]:
            nonlocal n_captures
            n_captures += 1
            return next(tails)

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, n_captures))
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send") as wait_compaction, patch("omo_manager.omo_tmux_send.time.sleep"):
            verify_submit(Args("cfg:1.0", None, 1, 0.15, 10, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        wait_compaction.assert_called_once()
        self.assertEqual([(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], 2)], calls)

    def test_verify_submit_waits_for_compaction_before_placeholder_return(self) -> None:
        tails = iter(
            [
                ["• Compacting conversation", "", "› Summarize recent commits", "  gpt-5.5"],
                ["• Working", "  gpt-5.5"],
            ]
        )

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send") as wait_compaction:
            verify_submit(Args("cfg:1.0", None, 1, 0.15, 10, False, submit_verify_timeout_s=1), "Summarize recent commits\n")

        wait_compaction.assert_called_once()

    def test_verify_submit_fails_when_running_input_box_still_has_other_text(self) -> None:
        calls: list[list[str]] = []

        def fake_tail(_: str, __: int) -> list[str]:
            return ["• Working (1m 59s • esc to interrupt)", "", "› Draft the manager reply", "", "  gpt-5.5"]

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "input box still has text"):
                verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Read the dispatch prompt from /tmp/x and follow it exactly.\n")

        self.assertEqual([], calls)

    def test_clear_stuck_input_before_send_submits_existing_real_input(self) -> None:
        report = Report("stuck_input", ["› Draft the manager reply"], "Draft the manager reply", True)
        with patch("omo_manager.omo_tmux_send.inspect", return_value=report), patch("omo_manager.omo_tmux_send.submit_stuck_input_if_present", return_value="sent_enter") as submit, patch("omo_manager.omo_tmux_send.time.sleep") as sleep:
            self.assertEqual("sent_enter", clear_stuck_input_before_send(Args("cfg:1.0", None, 1, 0.15, 0, False)))
        submit.assert_called_once()
        self.assertEqual(("cfg:1.0", report), submit.call_args.args)
        sleep.assert_called_once_with(0.15)

    def test_clear_stuck_input_before_send_ignores_placeholder(self) -> None:
        placeholder = Report("ready", ["› Summarize recent commits"], "Summarize recent commits", False)
        with patch("omo_manager.omo_tmux_send.inspect", return_value=placeholder), patch("omo_manager.omo_tmux_send.submit_stuck_input_if_present") as submit:
            self.assertEqual("", clear_stuck_input_before_send(Args("cfg:1.0", None, 1, 0.15, 0, False)))
        submit.assert_not_called()

    def test_clear_stuck_input_before_send_runs_for_paste_only_send(self) -> None:
        report = Report("stuck_input", ["› Draft the manager reply"], "Draft the manager reply", True)
        with patch("omo_manager.omo_tmux_send.inspect", return_value=report), patch("omo_manager.omo_tmux_send.submit_stuck_input_if_present", return_value="sent_enter") as submit:
            self.assertEqual("sent_enter", clear_stuck_input_before_send(Args("cfg:1.0", None, 0, 0.15, 0, False)))
        submit.assert_called_once()
        self.assertEqual(("cfg:1.0", report), submit.call_args.args)

    def test_run_tmux_clears_stuck_input_after_ready_wait(self) -> None:
        calls: list[str] = []

        def fake_clear(_: Args) -> str:
            calls.append("clear")
            return "sent_enter" if len(calls) == 2 else ""

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", side_effect=fake_clear), patch("omo_manager.omo_tmux_send.wait_ready") as wait, patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)):
            run_tmux(Args("cfg:1.0", None, 0, 0.15, 0, False), "prompt")

        wait.assert_called_once()
        self.assertEqual(["clear", "clear"], calls)

    def test_run_tmux_stops_if_clear_stuck_input_times_out_on_compaction(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", side_effect=["", "not_safe:compacting"]), patch("omo_manager.omo_tmux_send.wait_ready"), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "not_safe:compacting"):
                run_tmux(Args("cfg:1.0", None, 1, 0.15, 0, False), "prompt")

        self.assertEqual([], [call for call in calls if call[:2] != ["tmux", "delete-buffer"]])

    def test_run_tmux_stops_if_clear_stuck_input_fails_before_paste_only_send(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.clear_stuck_input_before_send", return_value="failed"), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                run_tmux(Args("cfg:1.0", None, 0, 0.15, 0, False), "prompt")

        self.assertEqual([], [call for call in calls if call[:2] != ["tmux", "delete-buffer"]])

    def test_verify_submit_ignores_codex_suggestion_while_running(self) -> None:
        prompts = (
            "Read the dispatch prompt from /tmp/x and follow it exactly.\n",
            "Summarize\n",
            "Summarize recent commits\nwith details\n",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                calls: list[list[str]] = []

                def fake_tail(_: str, __: int) -> list[str]:
                    return ["• Working (1m 59s • esc to interrupt)", "", "› Summarize recent commits", "", "  gpt-5.5"]

                def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0)

                with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
                    verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), prompt)

                self.assertEqual([], calls)

    def test_run_tmux_verifies_exact_codex_suggestion_prompt_with_probe(self) -> None:
        calls: list[list[str]] = []
        sentinel = "__omo_paste_probe_abcdef12__"
        tails = iter(
            [
                [f"› Summarize recent commits{sentinel}", "  gpt-5.5"],
                ["› Summarize recent commits", "  gpt-5.5"],
                ["• Working", "  gpt-5.5"],
            ]
        )

        class Uuid:
            hex = "abcdef1234567890"

        def fake_tail(_: str, __: int) -> list[str]:
            return next(tails)

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.wait_compaction_over_before_send"), patch("omo_manager.omo_tmux_send.uuid.uuid4", return_value=Uuid()), patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Summarize recent commits\n")

        self.assertIn(["tmux", "send-keys", "-l", "-t", "cfg:1.0", sentinel], calls)
        self.assertIn(["tmux", "send-keys", "-N", str(len(sentinel)), "-t", "cfg:1.0", "BSpace"], calls)
        self.assertLess(calls.index(["tmux", "send-keys", "-N", str(len(sentinel)), "-t", "cfg:1.0", "BSpace"]), calls.index(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"]))

    def test_verify_submit_accepts_matching_codex_suggestion_prompt_when_running(self) -> None:
        calls: list[list[str]] = []

        def fake_tail(_: str, __: int) -> list[str]:
            return ["• Working (1m 59s • esc to interrupt)", "", "› Summarize recent commits", "", "  gpt-5.5"]

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Summarize recent commits\n")

        self.assertEqual([], calls)

    def test_verify_submit_does_not_retry_ready_matching_codex_suggestion_prompt(self) -> None:
        calls: list[list[str]] = []

        def fake_tail(_: str, __: int) -> list[str]:
            return ["› Summarize recent commits", "  gpt-5.5"]

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=fake_tail), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(RuntimeError, "prompt still in input"):
                verify_submit(Args("cfg:1.0", None, 1, 0.15, 0, False, submit_verify_timeout_s=1), "Summarize recent commits\n")

        self.assertEqual([], calls)


if __name__ == "__main__":
    _ = unittest.main()
