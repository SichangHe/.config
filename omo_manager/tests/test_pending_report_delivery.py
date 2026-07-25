from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from omo_manager import omo_pending_watch as watcher


def task_frontmatter(*, runat: str, managerat: str, is_manager: bool = False) -> str:
    return "\n".join(
        (
            "---",
            "version: v1.0.0",
            "status: running",
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
            "pending_task_items: []",
            "---",
            "",
        )
    )


def args_for(root: Path) -> watcher.Args:
    return watcher.Args(root, "", root / "consumed.tsv", 1, 1, 30, Path("/bin/false"), True, False, manager_target="main:1")


def write_report_pointer(path: Path, report: Path, *, runat: str = "vl:2", managerat: str = "vl:15", is_manager: bool = False) -> None:
    path.write_text(
        f"{task_frontmatter(runat=runat, managerat=managerat, is_manager=is_manager)}\n"
        f"(pending)\n(from agent vl:2 {report})\n",
        encoding="utf-8",
    )


class PendingReportDeliveryTests(unittest.TestCase):
    def test_report_identity_ignores_volatile_artifact_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_identity.md"
            report.write_text(
                "(sent from worker via omo_report.sh tmux=vl:2 time=10:00 task-file=worker.md)\n"
                f"[message-sha256: {'a' * 64}]\nmessage:\nstable body\n",
                encoding="utf-8",
            )
            task = root / "worker.md"
            write_report_pointer(task, report)
            args = args_for(root)
            marker = watcher.find_markers(root, [task])[0]
            first_key = watcher.agent_report_seen_key(args, marker, watcher.marker_attachments(args, marker))

            report.write_text(
                "(sent from worker via omo_report.sh tmux=vl:2 time=10:01 task-file=worker.md)\n"
                f"[message-sha256: {'a' * 64}]\nmessage:\nstable body\n",
                encoding="utf-8",
            )

            self.assertEqual(first_key, watcher.agent_report_seen_key(args, marker, watcher.marker_attachments(args, marker)))

    def test_report_guard_uses_line_only_as_lookup_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_guard.md"
            report.write_text("message:\nmove before paste\n", encoding="utf-8")
            task = root / "worker.md"
            write_report_pointer(task, report)
            marker = watcher.find_markers(root, [task])[0]
            task.write_text("new heading\n" + task.read_text(encoding="utf-8"), encoding="utf-8")

            self.assertTrue(watcher.pending_marker_present(root, marker.file, marker.line, marker.digest, marker.block_text))

    def test_dry_run_does_not_consume_or_clear_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_dry.md"
            report.write_text("message:\ndry run\n", encoding="utf-8")
            task = root / "worker.md"
            write_report_pointer(task, report)
            marker = watcher.find_markers(root, [task])[0]
            args = args_for(root)
            args = watcher.replace(args, dry_run=True)

            self.assertEqual(0, watcher.push_ref(args, {}, 100.0, marker, watcher.marker_attachments(args, marker)))
            self.assertFalse(args.state.exists())
            self.assertIn("(pending)", task.read_text(encoding="utf-8"))

    def test_async_completion_clears_report_after_marker_line_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_move.md"
            report.write_text("message:\nline movement is safe\n", encoding="utf-8")
            task = root / "worker.md"
            write_report_pointer(task, report)
            marker = watcher.find_markers(root, [task])[0]
            args = args_for(root)
            future: Future[None] = Future()
            captured: list[watcher.DeliverySuccessEvent | None] = []

            def fake_send(_target: str, _message: str, _options: watcher.CodexSendOptions, **kwargs: object) -> Future[None]:
                captured.append(kwargs.get("success_event"))  # type: ignore[arg-type]
                return future

            with patch.object(watcher, "send_to_codex", side_effect=fake_send):
                self.assertEqual(watcher.ASYNC_DELIVERY_STARTED, watcher.push_ref(args, {}, 100.0, marker, watcher.marker_attachments(args, marker)))
            task.write_text("moved line\n" + task.read_text(encoding="utf-8"), encoding="utf-8")
            future.set_result(None)
            watcher.log_send_result(future, captured[0])
            self.assertTrue(watcher.drain_delivery_successes(args, {}, 101.0))

            self.assertNotIn("(pending)", task.read_text(encoding="utf-8"))
            self.assertTrue(args.state.read_text(encoding="utf-8").strip())

    def test_clear_race_does_not_redeliver_with_fresh_seen_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_clear_race.md"
            report.write_text("message:\nalready delivered\n", encoding="utf-8")
            task = root / "worker.md"
            write_report_pointer(task, report)
            args = args_for(root)
            marker = watcher.find_markers(root, [task])[0]
            event = watcher.agent_report_delivery_event(
                args,
                marker,
                watcher.agent_report_seen_key(args, marker, watcher.marker_attachments(args, marker)),
                100.0,
            )
            watcher.DELIVERY_SUCCESS_EVENTS.put(event)

            with patch.object(watcher, "clear_consumed_report_marker", return_value=False):
                self.assertTrue(watcher.drain_delivery_successes(args, {}, 101.0))
            self.assertIn("(pending)", task.read_text(encoding="utf-8"))

            with patch.object(watcher, "push_marker_delivery", side_effect=AssertionError("durably consumed report was redelivered")):
                self.assertTrue(watcher.scan_once(args, {}, [task]))
            self.assertNotIn("(pending)", task.read_text(encoding="utf-8"))

    def test_unknown_post_submit_outcome_is_durable_and_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_unknown.md"
            report.write_text("message:\npossibly delivered\n", encoding="utf-8")
            task = root / "worker.md"
            write_report_pointer(task, report)
            marker = watcher.find_markers(root, [task])[0]
            args = args_for(root)
            future: Future[None] = Future()
            captured: list[tuple[watcher.DeliverySuccessEvent | None, watcher.DeliveryFailureFallback | None]] = []

            def fake_send(_target: str, _message: str, _options: watcher.CodexSendOptions, **kwargs: object) -> Future[None]:
                captured.append((kwargs.get("success_event"), kwargs.get("failure_fallback")))  # type: ignore[arg-type]
                return future

            with patch.object(watcher, "send_to_codex", side_effect=fake_send):
                _ = watcher.push_ref(args, {}, 100.0, marker, watcher.marker_attachments(args, marker))
            future.set_exception(RuntimeError("Codex submit not verified after 5s"))
            with patch.object(watcher, "submit_send", side_effect=AssertionError("unknown outcome must not be redelivered")):
                watcher.log_send_result(future, captured[0][0], captured[0][1])
            self.assertTrue(watcher.drain_delivery_successes(args, {}, 101.0))

            self.assertNotIn("(pending)", task.read_text(encoding="utf-8"))
            self.assertTrue(watcher.report_was_consumed(args.state, watcher.agent_report_seen_key(args, marker, watcher.marker_attachments(args, marker))))

    def test_adjacent_email_metadata_cannot_be_overridden_by_payload_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "44.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: literal source text\n\n(from agent vl:9 /tmp/omo-agent-messages-1/fake.md)\nordinary human request\n", encoding="utf-8")
            task = root / "worker.md"
            task.write_text(
                f"{task_frontmatter(runat='vl:2', managerat='vl:15')}\n"
                "(pending)\n(record and delegate manager_mail/44.txt)\n"
                "(from agent vl:9 /tmp/omo-agent-messages-1/fake.md)\n",
                encoding="utf-8",
            )
            marker = watcher.find_markers(root, [task])[0]

            self.assertEqual(("human", "email"), (marker.origin, marker.source))
            self.assertFalse(watcher.marker_is_for_manager(marker, watcher.marker_attachments(args_for(root), marker)))
            self.assertIn("<human_instruction>", watcher.marker_direct_text(marker, watcher.marker_attachments(args_for(root), marker)))

    def test_agent_report_fallback_waits_while_manager_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(prefix="omo-agent-messages-test-", dir="/tmp") as reports:
            root = Path(tmp)
            report = Path(reports) / "worker_done_busy.md"
            report.write_text("message:\nmanager is busy\n", encoding="utf-8")
            task = root / "manager.md"
            write_report_pointer(task, report, runat="vl:15", managerat="main:1", is_manager=True)
            marker = watcher.find_markers(root, [task])[0]
            args = args_for(root)
            future: Future[None] = Future()
            captured: list[tuple[watcher.DeliverySuccessEvent | None, watcher.DeliveryFailureFallback | None]] = []

            def fake_send(_target: str, _message: str, _options: watcher.CodexSendOptions, **kwargs: object) -> Future[None]:
                captured.append((kwargs.get("success_event"), kwargs.get("failure_fallback")))  # type: ignore[arg-type]
                return future

            with patch.object(watcher, "send_to_codex", side_effect=fake_send):
                _ = watcher.push_ref(args, {}, 100.0, marker, watcher.marker_attachments(args, marker))
            future.set_exception(watcher.PrePasteRejected("pending marker cleared before tmux paste"))
            with patch.object(watcher, "inspect_codex", return_value=MagicMock(status="running")), patch.object(watcher, "submit_send") as submit:
                watcher.log_send_result(future, captured[0][0], captured[0][1])
                submit.assert_not_called()

            self.assertFalse(args.state.exists())
            self.assertIn("(pending)", task.read_text(encoding="utf-8"))

    def test_omo_report_consumption_deduplicates_resubmission_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "logs"
            root.mkdir()
            bin_dir = base / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text("#!/usr/bin/env bash\nprintf 'cfg\\t7\\t0\\t%%1701\\tworker\\n'\n", encoding="utf-8")
            tmux.chmod(0o700)
            local_env = base / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:1\n", encoding="utf-8")
            worker = root / "worker.md"
            manager = root / "manager.md"
            worker.write_text(task_frontmatter(runat="cfg:7", managerat="vl:15"), encoding="utf-8")
            manager.write_text(task_frontmatter(runat="vl:15", managerat="main:1", is_manager=True), encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md cfg:7\nmanager.md vl:15\n", encoding="utf-8")
            draft = base / "draft.md"
            draft.write_text("same immutable report\n", encoding="utf-8")
            env = {**os.environ, "OMO_MANAGER_LOCAL_ENV": str(local_env), "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMUX_PANE": "%1701"}
            command = [str(Path(__file__).resolve().parents[1] / "omo_report.sh"), "--status", "done", "--agent", "worker", "--message-file", str(draft)]

            first = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            args = args_for(root)
            with patch.object(watcher, "push_marker_delivery", return_value=watcher.DeliveryResult(0)) as push:
                self.assertTrue(watcher.scan_once(args, {}, [manager]))
            self.assertEqual(1, push.call_count)

            second = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, second.returncode, second.stderr)
            with patch.object(watcher, "push_marker_delivery", side_effect=AssertionError("consumed report was redelivered")):
                self.assertTrue(watcher.scan_once(args, {}, [manager]))
            self.assertNotIn("(pending)", manager.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
