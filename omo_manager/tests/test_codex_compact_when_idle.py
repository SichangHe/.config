import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_compact_when_idle import (
    Args,
    COMPACT_MESSAGE,
    launch_background,
    parse_args,
    run_worker,
    send_compact,
    wait_until_ready,
)
from omo_manager.omo_codex_status import Report


class CodexCompactWhenIdleTests(unittest.TestCase):
    def test_wait_until_ready_polls_until_ready(self) -> None:
        reports = iter([Report("running", ["working"]), Report("ready", ["done"])])
        seen: list[str] = []

        def fake_inspect(_: object) -> Report:
            seen.append("inspect")
            return next(reports)

        with patch("omo_manager.omo_codex_compact_when_idle.inspect", side_effect=fake_inspect), patch("omo_manager.omo_codex_compact_when_idle.time.sleep") as sleep:
            report = wait_until_ready(Args("cfg:1.0", 10, 2, 80, False, False, "", 1, None, 5))

        self.assertEqual("ready", report.status)
        self.assertEqual(["inspect", "inspect"], seen)
        sleep.assert_called_once_with(2)

    def test_wait_until_ready_reports_last_status_on_timeout(self) -> None:
        with patch("omo_manager.omo_codex_compact_when_idle.inspect", return_value=Report("running", [])), patch("omo_manager.omo_codex_compact_when_idle.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, "last status: running"):
                wait_until_ready(Args("cfg:1.0", 1, 2, 80, False, False, "", 1, None, 5))

    def test_send_compact_uses_safe_tmux_send_after_ready(self) -> None:
        calls: list[tuple[str, str, int]] = []

        def fake_run_tmux(args: object, message: str) -> None:
            calls.append((args.target, message, args.enter_count))

        with patch("omo_manager.omo_codex_compact_when_idle.wait_until_ready", return_value=Report("ready", [])), patch("omo_manager.omo_codex_compact_when_idle.run_tmux", side_effect=fake_run_tmux):
            send_compact(Args("cfg:1.0", 10, 2, 80, False, False, "", 1, None, 7))

        self.assertEqual([("cfg:1.0", COMPACT_MESSAGE, 1)], calls)

    def test_run_worker_notifies_failure(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_run_tmux(args: object, message: str) -> None:
            calls.append((args.target, message))

        with patch("omo_manager.omo_codex_compact_when_idle.send_compact", side_effect=RuntimeError("not ready")), patch("omo_manager.omo_codex_compact_when_idle.run_tmux", side_effect=fake_run_tmux), patch("sys.stdout", new_callable=StringIO):
            rc = run_worker(Args("cfg:1.0", 10, 2, 80, False, False, "cfg:0.0", 0, None, 5))

        self.assertEqual(1, rc)
        self.assertEqual("cfg:0.0", calls[0][0])
        self.assertIn("failed", calls[0][1])
        self.assertIn("not ready", calls[0][1])

    def test_launch_background_starts_worker_and_logs(self) -> None:
        started: list[list[str]] = []

        class Proc:
            pid = 4321

        def fake_popen(command: list[str], **_: object) -> Proc:
            started.append(command)
            return Proc()

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_codex_compact_when_idle.subprocess.Popen", side_effect=fake_popen), patch("sys.stdout", new_callable=StringIO) as stdout:
            log_file = Path(tmp) / "compact.log"
            launch_background(Args("cfg:1.0", 10, 2, 80, True, False, "cfg:0.0", 0, log_file, 5))

        command = started[0]
        self.assertIn("--worker", command)
        self.assertNotIn("--background", command)
        self.assertEqual("cfg:1.0", command[command.index("--target") + 1])
        self.assertIn("worker pid=4321", stdout.getvalue())

    def test_parse_args_validates_background_worker_split(self) -> None:
        with patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit):
            parse_args(["--target", "cfg:1.0", "--background", "--worker"])
        self.assertEqual(30, parse_args(["--target", "cfg:1.0", "--timeout-s", "30"]).timeout_s)


if __name__ == "__main__":
    _ = unittest.main()
