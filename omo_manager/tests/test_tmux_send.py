import os
import stat
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.omo_codex_status import Report
from omo_manager.omo_tmux_send import Args
from omo_manager.omo_tmux_send import CodexSendOptions
from omo_manager.omo_tmux_send import async_job_from_query
from omo_manager.omo_tmux_send import clear_existing_input_before_send
from omo_manager.omo_tmux_send import launch_async
from omo_manager.omo_tmux_send import main
from omo_manager.omo_tmux_send import parse_args
from omo_manager.omo_tmux_send import query_async_result
from omo_manager.omo_tmux_send import read_message
from omo_manager.omo_tmux_send import require_no_existing_input
from omo_manager.omo_tmux_send import require_sendable_codex_target
from omo_manager.omo_tmux_send import run_async_worker
from omo_manager.omo_tmux_send import run_tmux
from omo_manager.omo_tmux_send import send_message_file_to_codex
from omo_manager.omo_tmux_send import send_to_codex
from omo_manager.omo_tmux_send import verify_submit
from omo_manager.omo_tmux_send import wait_paste_visible
from omo_manager.omo_tmux_send import worker_argv
from omo_manager.omo_tmux_send import write_private_temp


def options(**kwargs: object) -> CodexSendOptions:
    values = {
        "enter_count": 1,
        "enter_delay_s": 0.15,
        "dry_run": False,
        "submit_verify_timeout_s": 1.0,
        "allow_plan_prompt_enter": False,
    }
    values.update(kwargs)
    return CodexSendOptions(**values)


class TmuxSendTests(unittest.TestCase):
    def test_read_message_file_preserves_special_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "message.txt"
            text = "literal $HOME `cmd` C-c ; newline\nsecond line\n"
            _ = path.write_text(text, encoding="utf-8")
            self.assertEqual(text, read_message(Args("cfg:1.0", path, options())))

    def test_private_temp_file_is_0600_and_preserves_text(self) -> None:
        path = write_private_temp("secret text\n")
        try:
            self.assertEqual("secret text\n", path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        finally:
            path.unlink(missing_ok=True)

    def test_parse_submit_is_mandatory(self) -> None:
        args = parse_args(["--target", "cfg:1.0", "--message-file", "prompt.md", "--enter-count", "2"])
        self.assertEqual(2, args.options.enter_count)
        self.assertEqual(1.0, parse_args(["--target", "cfg:1.0", "--message-file", "prompt.md", "--submit-verify-timeout-s", "1"]).options.submit_verify_timeout_s)
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit):
                parse_args(["--target", "cfg:1.0", "--message-file", "prompt.md", "--enter-count", "0"])

    def test_parse_accepts_deprecated_dispatch_flags_without_paste_only_mode(self) -> None:
        args = parse_args(["--target", "cfg:1.0", "--message-file", "prompt.md", "--enter", "--ready-timeout-s", "300"])

        self.assertEqual(1, args.options.enter_count)

    def test_send_to_codex_is_importable_library_boundary(self) -> None:
        calls: list[tuple[str, str, CodexSendOptions]] = []

        def fake_run(target: str, message: str, selected: CodexSendOptions, **_kwargs: object) -> None:
            calls.append((target, message, selected))

        with patch("omo_manager.omo_tmux_send.run_tmux", side_effect=fake_run):
            send_to_codex("cfg:1.0", "hello\n", options(enter_count=2))

        self.assertEqual("cfg:1.0", calls[0][0])
        self.assertEqual("hello\n", calls[0][1])
        self.assertEqual(2, calls[0][2].enter_count)

    def test_send_message_file_to_codex_reads_file_without_caller_tempfile(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_send(target: str, message: str, selected: CodexSendOptions | None = None) -> None:
            calls.append((target, message))

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.send_to_codex", side_effect=fake_send):
            path = Path(tmp) / "prompt.md"
            path.write_text("prompt\n", encoding="utf-8")
            send_message_file_to_codex("cfg:1.0", path, options())

        self.assertEqual([("cfg:1.0", "prompt\n")], calls)

    def test_require_sendable_codex_target_allows_running_ready_and_stuck(self) -> None:
        cases = (
            ["• Working", "  gpt-5.5"],
            [
                "• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9",
                "• Working (21s • esc to interrupt)",
                "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)",
                "› Implement {feature}",
                "  gpt-5.5",
            ],
            ["› Use /skills to list available skills", "  gpt-5.5"],
            ["› Continue task", "  gpt-5.5"],
        )
        for lines in cases:
            with self.subTest(lines=lines), patch("omo_manager.omo_tmux_send.tail", return_value=lines):
                require_sendable_codex_target("cfg:1.0")

    def test_require_sendable_codex_target_rejects_not_codex_and_error(self) -> None:
        with patch("omo_manager.omo_tmux_send.tail", return_value=["fish prompt"]):
            with self.assertRaisesRegex(RuntimeError, "not a Codex pane"):
                require_sendable_codex_target("vl:20.0")
        lines = ["────", "■ Error: 429 Too Many Requests", "› Use /skills", "  gpt-5.5"]
        with patch("omo_manager.omo_tmux_send.tail", return_value=lines):
            with self.assertRaisesRegex(RuntimeError, "Codex error state"):
                require_sendable_codex_target("cfg:1.0")

    def test_run_tmux_uses_buffer_and_mandatory_enter(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.clear_existing_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.require_sendable_codex_target"), patch("omo_manager.omo_tmux_send.verify_placeholder_paste", return_value=False), patch("omo_manager.omo_tmux_send.wait_paste_visible"), patch("omo_manager.omo_tmux_send.verify_submit"), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux("cfg:1.0", "literal C-c $(bad)\n", options())

        self.assertIn(["tmux", "load-buffer", "-b", calls[0][3], calls[0][4]], calls)
        self.assertIn(["tmux", "paste-buffer", "-b", calls[0][3], "-t", "cfg:1.0"], calls)
        self.assertIn(["tmux", "send-keys", "-t", "cfg:1.0", "Enter"], calls)

    def test_run_tmux_rechecks_input_immediately_before_paste(self) -> None:
        events: list[str] = []

        def before_paste() -> None:
            events.append("before_paste")

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            events.append(command[1])
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.clear_existing_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.require_sendable_codex_target"), patch("omo_manager.omo_tmux_send.verify_placeholder_paste", return_value=True), patch("omo_manager.omo_tmux_send.verify_submit"), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            run_tmux("cfg:1.0", "prompt\n", options(), before_paste=before_paste)

        self.assertEqual(["load-buffer", "before_paste", "capture-pane", "paste-buffer"], events[:4])
        self.assertIn("send-keys", events)

    def test_run_tmux_does_not_wait_for_compaction(self) -> None:
        with patch("omo_manager.omo_tmux_send.clear_existing_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.require_sendable_codex_target"), patch("omo_manager.omo_tmux_send.verify_placeholder_paste", return_value=True), patch("omo_manager.omo_tmux_send.verify_submit"), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)):
            run_tmux("cfg:1.0", "prompt\n", options())

    def test_wait_paste_visible_rejects_partial_probe_match(self) -> None:
        cases = (
            (["› Summarize recent commits", "  gpt-5.5"], "Summarize\nwith details\n"),
            (["• Working", "", "› Explain this codebase", "  gpt-5.5"], "Explain this codebase\nwith constraints\n"),
        )
        for lines, message in cases:
            with self.subTest(message=message), patch("omo_manager.omo_tmux_send.tail", return_value=lines), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
                with self.assertRaisesRegex(RuntimeError, "Codex paste not verified"):
                    wait_paste_visible("cfg:1.0", message, options())

    def test_wait_paste_visible_accepts_collapsed_pasted_content(self) -> None:
        lines = ["› [Pasted Content 2048 chars]", "  gpt-5.5"]
        with patch("omo_manager.omo_tmux_send.tail", return_value=lines):
            wait_paste_visible("cfg:1.0", "line one\nline two\n", options())

    def test_clear_existing_input_before_send_submits_existing_real_input(self) -> None:
        before = Report("stuck_input", ["› Draft the manager reply"], "Draft the manager reply", True)
        after = Report("running", ["• Working"], "", False)
        with patch("omo_manager.omo_tmux_send.inspect", side_effect=[before, after]), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)) as run, patch("omo_manager.omo_tmux_send.time.sleep") as sleep:
            self.assertEqual("sent_enter", clear_existing_input_before_send("cfg:1.0", options()))
        run.assert_called_once()
        sleep.assert_called_once()

    def test_clear_existing_input_before_send_submits_even_when_report_says_unsafe(self) -> None:
        before = Report("stuck_input", ["› Continue task"], "Continue task", False, "compacting")
        after = Report("running", ["• Working"], "", False)
        with patch("omo_manager.omo_tmux_send.inspect", side_effect=[before, after]), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)) as run:
            self.assertEqual("sent_enter", clear_existing_input_before_send("cfg:1.0", options()))
        run.assert_called_once()

    def test_clear_existing_input_before_send_submits_running_queued_input(self) -> None:
        before = Report("running", ["• Working"], "queued worker message", False, "queued_running_input")
        after = Report("running", ["• Working"], "", False)
        with patch("omo_manager.omo_tmux_send.inspect", side_effect=[before, after]), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)) as run:
            self.assertEqual("sent_enter", clear_existing_input_before_send("cfg:1.0", options()))
        run.assert_called_once()

    def test_clear_existing_input_before_send_refuses_to_paste_if_input_remains(self) -> None:
        report = Report("stuck_input", ["› Continue task"], "Continue task", False, "compacting")
        with patch("omo_manager.omo_tmux_send.inspect", side_effect=[report, report]), patch("omo_manager.omo_tmux_send.subprocess.run", return_value=subprocess.CompletedProcess(["tmux"], 0)):
            self.assertEqual("still_input", clear_existing_input_before_send("cfg:1.0", options()))

    def test_clear_existing_input_before_send_fails_closed_when_inspect_fails(self) -> None:
        with patch("omo_manager.omo_tmux_send.inspect", side_effect=RuntimeError("tmux unavailable")):
            self.assertEqual("inspect_failed", clear_existing_input_before_send("cfg:1.0", options()))

    def test_require_no_existing_input_rejects_real_input_before_paste(self) -> None:
        report = Report("running", ["• Working"], "queued worker message", False)
        with patch("omo_manager.omo_tmux_send.inspect", return_value=report):
            with self.assertRaisesRegex(RuntimeError, "existing input appeared"):
                require_no_existing_input("cfg:1.0")

    def test_run_tmux_stops_before_paste_when_stuck_input_remains(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.clear_existing_input_before_send", return_value="still_input"), patch("omo_manager.omo_tmux_send.require_sendable_codex_target"), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "target existing input not cleared"):
                run_tmux("cfg:1.0", "prompt\n", options())

        self.assertFalse(any(command[:2] == ["tmux", "paste-buffer"] for command in calls))

    def test_run_tmux_stops_before_paste_when_callback_introduces_input(self) -> None:
        calls: list[list[str]] = []
        state = {"callback_ran": False}

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        def before_paste() -> None:
            state["callback_ran"] = True

        def fake_inspect(_args: object) -> Report:
            if state["callback_ran"]:
                return Report("running", ["• Working"], "late queued input", False)
            return Report("running", ["• Working"], "", False)

        with patch("omo_manager.omo_tmux_send.clear_existing_input_before_send", return_value=""), patch("omo_manager.omo_tmux_send.require_sendable_codex_target"), patch("omo_manager.omo_tmux_send.verify_placeholder_paste", return_value=True), patch("omo_manager.omo_tmux_send.verify_submit"), patch("omo_manager.omo_tmux_send.inspect", side_effect=fake_inspect), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "existing input appeared"):
                run_tmux("cfg:1.0", "prompt\n", options(), before_paste=before_paste)

        self.assertFalse(any(command[:2] == ["tmux", "paste-buffer"] for command in calls))

    def test_verify_submit_requires_running_and_prompt_gone(self) -> None:
        tails = iter(
            [
                ["› Read the dispatch prompt from /tmp/x and follow it exactly.", "  gpt-5.5"],
                ["› Use /skills to list available skills", "  gpt-5.5"],
                ["• Working", "  gpt-5.5"],
            ]
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=lambda *_: next(tails)), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.sleep"):
            verify_submit("cfg:1.0", "Read the dispatch prompt from /tmp/x and follow it exactly.\n", options(allow_plan_prompt_enter=True))

        self.assertEqual([["tmux", "send-keys", "-t", "cfg:1.0", "Enter"]], calls)

    def test_verify_submit_accepts_waiting_subagent_as_running_like_after_prompt_leaves(self) -> None:
        lines = [
            "• Waiting for 019f3875-05fe-7583-ac1a-48abda94c6f9",
            "• Working (21s • esc to interrupt)",
            "• Messages to be submitted after next tool call (press esc to interrupt and send immediately)",
            "› Implement {feature}",
            "  gpt-5.5",
        ]
        with patch("omo_manager.omo_tmux_send.tail", return_value=lines):
            verify_submit("cfg:1.0", "Read the dispatch prompt from /tmp/x and follow it exactly.\n", options())

    def test_verify_submit_retries_enter_for_collapsed_paste_until_prompt_clears(self) -> None:
        tails = iter(
            [
                [
                    "• Working (19m 47s • esc to interrupt)",
                    "",
                    "› [Pasted Content 2048 chars]",
                    "  tab to queue message                                                                                    28% context left",
                ],
                ["• Working", "", "› Implement {feature}", "  gpt-5.5"],
            ]
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with patch("omo_manager.omo_tmux_send.tail", side_effect=lambda *_: next(tails)), patch("omo_manager.omo_tmux_send.subprocess.run", side_effect=fake_run), patch("omo_manager.omo_tmux_send.time.sleep"):
            verify_submit("cfg:1.0", "Read the dispatch prompt from /tmp/x and follow it exactly.\n", options())

        self.assertEqual([["tmux", "send-keys", "-t", "cfg:1.0", "Enter"]], calls)

    def test_verify_submit_rejects_unrelated_visible_input_even_if_running(self) -> None:
        lines = [
            "• Working (19m 47s • esc to interrupt)",
            "",
            "› human typed something else",
            "  tab to queue message                                                                                    28% context left",
        ]
        with patch("omo_manager.omo_tmux_send.tail", return_value=lines):
            with self.assertRaisesRegex(RuntimeError, "different input remains visible"):
                verify_submit("cfg:1.0", "Read the dispatch prompt from /tmp/x and follow it exactly.\n", options())

    def test_verify_submit_fails_when_prompt_gone_but_not_running(self) -> None:
        with patch("omo_manager.omo_tmux_send.tail", return_value=["› Use /skills to list available skills", "  gpt-5.5"]), patch("omo_manager.omo_tmux_send.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "target did not become running"):
                verify_submit("cfg:1.0", "Read the dispatch prompt from /tmp/x and follow it exactly.\n", options())

    def test_launch_async_copies_payload_starts_worker_and_prints_result_dir(self) -> None:
        started: list[list[str]] = []

        class Proc:
            pid = 1234

        def fake_popen(command: list[str], **_: object) -> Proc:
            started.append(command)
            return Proc()

        stdout = StringIO()
        with patch("omo_manager.omo_tmux_send.subprocess.Popen", side_effect=fake_popen), patch("sys.stdout", stdout):
            launch_async(Args("cfg:1.0", None, options(enter_count=2), async_mode=True, async_notify_target="cfg:0.0"), "literal $HOME\n")

        command = started[0]
        payload_path = Path(command[command.index("--message-file") + 1])
        result_dir = Path(command[command.index("--async-result-dir") + 1])
        try:
            self.assertIn("--async-worker", command)
            self.assertNotIn("--async", command)
            self.assertNotIn("--compaction-wait-timeout-s", command)
            self.assertEqual("literal $HOME\n", payload_path.read_text(encoding="utf-8"))
            self.assertEqual("running\n", (result_dir / "status.txt").read_text(encoding="utf-8"))
            self.assertEqual(0o700, stat.S_IMODE(os.stat(result_dir).st_mode))
            self.assertIn("async_id:", stdout.getvalue())
            self.assertIn(f"result_dir: {result_dir}", stdout.getvalue())
        finally:
            for path in result_dir.iterdir():
                path.unlink(missing_ok=True)
            result_dir.rmdir()

    def test_main_async_cleanup_removes_launch_message_file_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "prompt.txt"
            payload.write_text("prompt\n", encoding="utf-8")
            launched: dict[str, str] = {}

            def fake_launch_async(_args: Args, message: str) -> None:
                launched["message"] = message

            with patch("omo_manager.omo_tmux_send.launch_async", side_effect=fake_launch_async):
                rc = main(["--target", "cfg:1.0", "--message-file", str(payload), "--async", "--async-cleanup-message-file"])

            self.assertEqual(0, rc)
            self.assertEqual("prompt\n", launched["message"])
            self.assertFalse(payload.exists())

    def test_main_async_cleanup_keeps_message_file_when_launch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "prompt.txt"
            payload.write_text("prompt\n", encoding="utf-8")

            with patch("omo_manager.omo_tmux_send.launch_async", side_effect=OSError("spawn failed")):
                rc = main(["--target", "cfg:1.0", "--message-file", str(payload), "--async", "--async-cleanup-message-file"])

            self.assertEqual(1, rc)
            self.assertTrue(payload.exists())
            self.assertEqual("prompt\n", payload.read_text(encoding="utf-8"))

    def test_async_result_lookup_accepts_id_or_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omo-tmux-send-async-test-") as tmp:
            result_dir = Path(tmp)
            job = async_job_from_query(str(result_dir))
            job.status_file.write_text("succeeded\n", encoding="utf-8")
            job.result_file.write_text("sent\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                self.assertEqual(0, query_async_result(str(result_dir)))
            self.assertIn(f"result_dir: {result_dir}", stdout.getvalue())

    def test_run_async_worker_cleans_payload_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_tmux_send.send_to_codex", side_effect=RuntimeError("target not ready")):
            result_dir = Path(tmp) / "result"
            result_dir.mkdir()
            payload = Path(tmp) / "payload.txt"
            payload.write_text("prompt\n", encoding="utf-8")
            rc = run_async_worker(Args("cfg:1.0", payload, options(), async_cleanup_message_file=True, async_result_dir=result_dir))

            self.assertEqual(1, rc)
            self.assertFalse(payload.exists())
            self.assertEqual("failed\n", (result_dir / "status.txt").read_text(encoding="utf-8"))
            self.assertEqual("target not ready\n", (result_dir / "result.txt").read_text(encoding="utf-8"))

    def test_worker_argv_preserves_result_lookup_worker_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = async_job_from_query(str(Path(tmp) / "omo-tmux-send-async-abc"))
            args = Args("cfg:1.0", Path("prompt.md"), options(enter_count=2), async_notify_target="cfg:0.0")
            command = worker_argv(args, job)

        self.assertIn("--async-worker", command)
        self.assertIn("--async-cleanup-message-file", command)
        self.assertIn("--async-result-dir", command)
        self.assertEqual("2", command[command.index("--enter-count") + 1])


if __name__ == "__main__":
    unittest.main()
