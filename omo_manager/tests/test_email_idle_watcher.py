from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch

from omo_manager import email_idle_watcher as watcher
from omo_manager import omo_codex_start
from omo_manager import omo_manager_rotate as rotation
from omo_manager import omo_pending_watch as pending_watcher
from omo_manager import omo_tmux_input_lock as input_locks
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_email_subject import RecentHeader, SubjectInputError, SubjectLookupTimeout


class EmailRetentionScheduleTests(unittest.TestCase):
    def test_quiet_idle_runs_five_second_fallback_scan(self) -> None:
        args = watcher.Args(
            Path("/tmp/logs"),
            "",
            Path("/tmp/mail"),
            Path("/tmp/state"),
            None,
            False,
            "human@example.test",
            0,
            Path("/bin/false"),
            idle_wait_s=60,
            pull_interval_s=5,
            idle_exit_after_s=0,
        )
        client = Mock()
        with (
            patch.object(watcher.time, "monotonic", side_effect=[0.0, 0.0, 5.0]),
            patch.object(watcher, "idle_once", return_value=False) as idle,
            patch.object(watcher, "handle_unseen", side_effect=[False, RuntimeError("stop")]) as scan,
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            watcher.watch_inbox(client, args)
        idle.assert_called_once_with(client, 5.0)
        client.select.assert_called_once_with("INBOX")
        self.assertEqual([call(client, args, "startup"), call(client, args, "poll")], scan.call_args_list)

    def test_fallback_scan_default_is_five_seconds(self) -> None:
        self.assertEqual(5, watcher.DEFAULT_PULL_INTERVAL_S)
        self.assertEqual(5, watcher.parse_args([]).pull_interval_s)

    def test_same_mailbox_retention_runs_only_every_five_minutes(self) -> None:
        now_s = 0.0
        schedule = watcher.ForegroundThresholdCheck(clock=lambda: now_s)
        check = Mock(return_value=True)
        self.assertTrue(schedule(check))
        now_s = 5
        self.assertFalse(schedule(check))
        now_s = 299
        self.assertFalse(schedule(check))
        now_s = 300
        self.assertTrue(schedule(check))
        self.assertEqual(2, check.call_count)

    def test_human_inbox_retention_default_is_five_minutes(self) -> None:
        self.assertEqual(300, watcher.DEFAULT_MANAGER_MAIL_THRESHOLD_INTERVAL_S)

    def test_slow_human_inbox_retention_does_not_block_idle_intake(self) -> None:
        started = threading.Event()
        release = threading.Event()
        args = watcher.Args(
            Path("/tmp/logs"),
            "",
            Path("/tmp/mail"),
            Path("/tmp/state"),
            None,
            False,
            "human@example.test",
            0,
            Path("/bin/false"),
        )

        def threshold_check() -> watcher.ManagerMailThresholdSnapshot:
            started.set()
            _ = release.wait(timeout=2)
            return watcher.ManagerMailThresholdSnapshot(args, watcher.ManagerMailCounts(0, 0, 86400, 0, True))

        check = watcher.BackgroundThresholdCheck(threshold_check)
        try:
            started_s = time.monotonic()
            self.assertFalse(check())
            self.assertLess(time.monotonic() - started_s, 0.5)
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(check())
        finally:
            release.set()

    def test_human_inbox_retention_runs_only_after_five_minutes(self) -> None:
        args = watcher.Args(
            Path("/tmp/logs"),
            "",
            Path("/tmp/mail"),
            Path("/tmp/state"),
            None,
            False,
            "human@example.test",
            0,
            Path("/bin/false"),
        )
        now_s = 0.0
        completed = threading.Event()
        calls = 0

        def collect() -> watcher.ManagerMailThresholdSnapshot:
            nonlocal calls
            calls += 1
            completed.set()
            return watcher.ManagerMailThresholdSnapshot(args, watcher.ManagerMailCounts(0, 0, 86400, 0, True))

        check = watcher.BackgroundThresholdCheck(collect, clock=lambda: now_s)
        with patch.object(watcher, "apply_manager_mail_thresholds", return_value=False) as apply:
            self.assertFalse(check())
            self.assertTrue(completed.wait(timeout=1))
            now_s = 60
            deadline_s = time.monotonic() + 1
            while apply.call_count == 0 and time.monotonic() < deadline_s:
                self.assertFalse(check())
                time.sleep(0.001)
            self.assertEqual(1, apply.call_count)
            self.assertEqual(1, calls)
            now_s = 299
            self.assertFalse(check())
            self.assertEqual(1, calls)
            completed.clear()
            now_s = 300
            self.assertFalse(check())
            self.assertTrue(completed.wait(timeout=1))
            self.assertEqual(2, calls)

    def test_overlong_retention_collection_does_not_overlap(self) -> None:
        args = watcher.Args(Path("/tmp/logs"), "", Path("/tmp/mail"), Path("/tmp/state"), None, False, "human@example.test", 0, Path("/bin/false"))
        now_s = 0.0
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def collect() -> watcher.ManagerMailThresholdSnapshot:
            nonlocal calls
            calls += 1
            started.set()
            _ = release.wait(timeout=2)
            return watcher.ManagerMailThresholdSnapshot(args, watcher.ManagerMailCounts(0, 0, 86400, 0, True))

        check = watcher.BackgroundThresholdCheck(collect, clock=lambda: now_s)
        try:
            self.assertFalse(check())
            self.assertTrue(started.wait(timeout=1))
            now_s = 600
            self.assertFalse(check())
            self.assertFalse(check())
            self.assertEqual(1, calls)
        finally:
            release.set()

    def test_retention_apply_failure_does_not_escape_intake(self) -> None:
        args = watcher.Args(Path("/tmp/logs"), "", Path("/tmp/mail"), Path("/tmp/state"), None, False, "human@example.test", 0, Path("/bin/false"))
        completed = threading.Event()

        def collect() -> watcher.ManagerMailThresholdSnapshot:
            completed.set()
            return watcher.ManagerMailThresholdSnapshot(args, watcher.ManagerMailCounts(0, 0, 86400, 0, True))

        check = watcher.BackgroundThresholdCheck(collect)
        with patch.object(watcher, "apply_manager_mail_thresholds", side_effect=OSError("state unavailable")), self.assertLogs(level="WARNING") as logs:
            self.assertFalse(check())
            self.assertTrue(completed.wait(timeout=1))
            deadline_s = time.monotonic() + 1
            while "threshold apply failed" not in "\n".join(logs.output) and time.monotonic() < deadline_s:
                self.assertFalse(check())
                time.sleep(0.001)
        self.assertIn("threshold apply failed", "\n".join(logs.output))

    def test_retention_task_changes_run_on_intake_thread(self) -> None:
        args = watcher.Args(
            Path("/tmp/logs"),
            "",
            Path("/tmp/mail"),
            Path("/tmp/state"),
            None,
            False,
            "human@example.test",
            0,
            Path("/bin/false"),
        )
        collected = threading.Event()
        collector_thread = 0
        apply_thread = 0

        def collect() -> watcher.ManagerMailThresholdSnapshot:
            nonlocal collector_thread
            collector_thread = threading.get_ident()
            collected.set()
            return watcher.ManagerMailThresholdSnapshot(args, watcher.ManagerMailCounts(0, 0, 86400, 0, True))

        def apply(_args: watcher.Args, _counts: watcher.ManagerMailCounts) -> bool:
            nonlocal apply_thread
            apply_thread = threading.get_ident()
            return False

        check = watcher.BackgroundThresholdCheck(collect)
        with patch.object(watcher, "apply_manager_mail_thresholds", side_effect=apply):
            self.assertFalse(check())
            self.assertTrue(collected.wait(timeout=1))
            deadline_s = time.monotonic() + 1
            while apply_thread == 0 and time.monotonic() < deadline_s:
                self.assertFalse(check())
                time.sleep(0.001)
        self.assertEqual(threading.get_ident(), apply_thread)
        self.assertNotEqual(collector_thread, apply_thread)

    def test_pending_delivery_log_correlates_email_source_and_target(self) -> None:
        guard = pending_watcher.PendingGuard(
            Path("/tmp/logs"),
            Path("task.md"),
            9,
            "digest",
            "(pending)\n(record and delegate manager_mail/85c-source.txt)",
        )
        stderr = StringIO()
        with patch.object(pending_watcher, "verified_send_to_codex"), redirect_stderr(stderr):
            pending_watcher.run_verified_send(
                "config:24",
                "request",
                pending_watcher.CodexSendOptions(1, 0, False),
                pending_guard=guard,
            )
        output = stderr.getvalue()
        self.assertIn("source=manager_mail/85c-source.txt", output)
        self.assertIn("target=config:24", output)
        self.assertIn("submit_started_at=", output)
        self.assertIn("submitted_at=", output)

    def test_intake_timing_adds_no_metadata_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            manager = root / "manager.md"
            manager.write_text("manager log\n", encoding="utf-8")
            args = watcher.Args(root, "", mail_dir, root / "state", manager, True, "human@example.test", 0, Path("/bin/false"), manager_target="main:0", mail_thresholds=False)
            raw_message = (
                b"From: Human <human@example.test>\r\n"
                b"Date: Sat, 12 Sep 2026 16:47:17 -0700\r\n"
                b"Subject: Direct work\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                b"Do the work.\r\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, tuple[object, ...]]] = []

                def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                    self.calls.append((command, arguments))
                    if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_message)]
                    raise AssertionError((command, arguments))

            client = Client()
            with (
                patch.object(watcher, "search_sender_uids", return_value={b"7"}),
                patch.object(watcher, "exact_human_sender", return_value=True),
                patch.object(watcher, "try_route_amh_message", return_value=watcher.AmhRouteDisposition.FALLBACK),
                patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
                patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
                self.assertLogs(level="INFO") as logs,
            ):
                self.assertTrue(watcher.handle_unseen(client, args, "idle"))  # type: ignore[arg-type]

            self.assertEqual([("fetch", ("7", "(BODY.PEEK[])"))], client.calls)
            timing = "\n".join(logs.output)
            self.assertIn("email intake timing:", timing)
            self.assertIn("trigger=idle", timing)
            self.assertIn("message_date_to_copy_s=", timing)

    def test_retention_append_cannot_overwrite_human_mail_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            manager.write_text("task\n", encoding="utf-8")
            mail = root / "manager_mail" / "source.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: request\n\nbody\n", encoding="utf-8")
            args = watcher.Args(root, "", mail.parent, root / "state", manager, False, "human@example.test", 0, Path("/bin/false"))
            retention_read = threading.Event()
            release_retention = threading.Event()
            original_read_text = Path.read_text

            def read_text(path: Path, *read_args: object, **read_kwargs: object) -> str:
                text = original_read_text(path, *read_args, **read_kwargs)  # type: ignore[arg-type]
                if path == manager and threading.current_thread().name == "retention-test" and not retention_read.is_set():
                    retention_read.set()
                    _ = release_retention.wait(timeout=2)
                return text

            retention = threading.Thread(
                target=watcher.append_manager_mail_threshold_pending,
                args=(args, "total-cleanup", watcher.ManagerMailCounts(30, 0, 86400, 0, True)),
                name="retention-test",
            )
            human = threading.Thread(target=watcher.append_pending, args=(root, mail, manager), name="human-test")
            with patch.object(Path, "read_text", read_text):
                retention.start()
                self.assertTrue(retention_read.wait(timeout=1))
                human.start()
                human.join(timeout=0.05)
                self.assertTrue(human.is_alive())
                release_retention.set()
                retention.join(timeout=2)
                human.join(timeout=2)
            self.assertFalse(retention.is_alive())
            self.assertFalse(human.is_alive())
            text = manager.read_text(encoding="utf-8")
            self.assertIn("manager mail 30 exceeds", text)
            self.assertIn("(record and delegate manager_mail/source.txt)", text)


def task_text(
    *,
    runat: str,
    managerat: str,
    is_manager: bool = False,
    pending_items: tuple[str, ...] = (),
    status: str = "running",
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
        f"runat: {runat}",
        "tool: codex",
        f"managerat: {managerat}",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.extend(("---", "task body", ""))
    return "\n".join(lines)


def args_for(root: Path, manager_file: Path) -> watcher.Args:
    return watcher.Args(
        root=root,
        manager_url="",
        mail_dir=root / "manager_mail",
        state_dir=root / "state",
        manager_file=manager_file,
        once=True,
        self_email="human@example.test",
        recovery_debounce_s=0,
        restart_script=Path("/bin/false"),
        manager_target="main:0",
        mail_thresholds=False,
    )


class AgentLifecycleCommandParserTests(unittest.TestCase):
    def test_accepts_exact_case_insensitive_command_prefix_without_please_or_period(self) -> None:
        cases = {
            "REPLACE this Agent": watcher.AgentLifecycleAction.REPLACE,
            "terminate this agent": watcher.AgentLifecycleAction.TERMINATE,
            "Replace this agent.": watcher.AgentLifecycleAction.REPLACE,
            "Terminate this agent -- because it is stuck\nSteven": watcher.AgentLifecycleAction.TERMINATE,
            "Replace this agent\nExplanation starts on the next line without a blank line.": watcher.AgentLifecycleAction.REPLACE,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                command = watcher.agent_lifecycle_command(body)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertIs(expected, command.action)

    def test_replacement_preserves_everything_after_the_matched_instruction_as_context(self) -> None:
        command = watcher.agent_lifecycle_command("Replace this agent. Previous agent ignored the queue.\n-- Human")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(". Previous agent ignored the queue.\n-- Human", command.replacement_context)

        termination = watcher.agent_lifecycle_command("Terminate this agent because the work is obsolete")
        self.assertIsNotNone(termination)
        assert termination is not None
        self.assertEqual("", termination.replacement_context)

        crlf = watcher.agent_lifecycle_command("Replace this agent.\r\nExact Windows-style reason\r\n-- Human")
        self.assertIsNotNone(crlf)
        assert crlf is not None
        self.assertEqual(".\r\nExact Windows-style reason\r\n-- Human", crlf.replacement_context)

    def test_lifecycle_guidance_keeps_context_and_ordered_queue_in_sources(self) -> None:
        binding = watcher.AgentLifecycleBinding(
            Path("worker.md"),
            "worker:2",
            "mgr:1",
            Path("manager.md"),
            "running",
            False,
            "0" * 64,
            ("first task", "second task"),
        )
        replacement = watcher.lifecycle_action_guidance(
            watcher.AgentLifecycleCommand(
                watcher.AgentLifecycleAction.REPLACE,
                replacement_context="\nThe previous agent ignored the requested tests.",
            ),
            binding,
            main_manager=False,
        )
        termination = watcher.lifecycle_action_guidance(
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
            binding,
            main_manager=False,
        )

        self.assertIn(
            "decision: stop this agent; give its successor the complete original Human request",
            replacement,
        )
        self.assertNotIn("The previous agent ignored the requested tests.", "\n".join(replacement))
        self.assertIn(
            "decision: stop this agent; retain its task queue",
            termination,
        )

    def test_lifecycle_delivery_does_not_inline_custody_payload(self) -> None:
        context = "reason-" + ("x" * pending_watcher.LIFECYCLE_DELIVERY_CHAR_LIMIT)
        items = tuple(f"task-{index}-" + ("y" * 500) for index in range(12))
        binding = watcher.AgentLifecycleBinding(
            Path("worker.md"),
            "worker:2",
            "mgr:1",
            Path("manager.md"),
            "running",
            False,
            "0" * 64,
            items,
        )
        termination_marker = pending_watcher.Marker(
            file=Path("worker.md"),
            line=1,
            digest="0" * 64,
            origin="human",
            source="manager_mail/42.txt",
            delegate_source="manager_mail/42.txt",
            pending_tail="",
            block_text="\n".join(
                (
                    pending_watcher.LIFECYCLE_EXPLANATION,
                    "requested_action: terminate",
                    *watcher.lifecycle_action_guidance(
                        watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
                        binding,
                        main_manager=False,
                    ),
                )
            ),
            file_lines=1,
            blocked_reason="",
        )
        replacement_marker = pending_watcher.Marker(
            file=Path("worker.md"),
            line=1,
            digest="0" * 64,
            origin="human",
            source="manager_mail/42.txt",
            delegate_source="manager_mail/42.txt",
            pending_tail="",
            block_text="\n".join(
                (
                    pending_watcher.LIFECYCLE_EXPLANATION,
                    "requested_action: replace",
                    *watcher.lifecycle_action_guidance(
                        watcher.AgentLifecycleCommand(
                            watcher.AgentLifecycleAction.REPLACE,
                            replacement_context=context,
                        ),
                        binding,
                        main_manager=False,
                    ),
                )
            ),
            file_lines=1,
            blocked_reason="",
        )

        termination_delivery = pending_watcher.lifecycle_marker_delivery_text(termination_marker, ())
        replacement_delivery = pending_watcher.lifecycle_marker_delivery_text(replacement_marker, ())
        self.assertNotIn(items[0], termination_marker.block_text)
        self.assertNotIn(context, replacement_marker.block_text)
        self.assertLess(len(termination_marker.block_text), 500)
        self.assertLess(len(replacement_marker.block_text), 500)
        self.assertNotIn(json.dumps(list(items), ensure_ascii=False, separators=(",", ":")), termination_delivery)
        self.assertNotIn(json.dumps(context, ensure_ascii=False), replacement_delivery)
        self.assertLess(len(termination_delivery), 300)
        self.assertLess(len(replacement_delivery), 300)

    def test_rejects_please_synonyms_nonleading_and_incomplete_phrases(self) -> None:
        for body in (
            "Please replace this agent.",
            "Please terminate this agent.",
            "Could you replace this agent?",
            "swap out this agent",
            "close this agent",
            "Do not terminate this agent",
            "Thanks.\n\nTerminate this agent",
            "> terminate this agent",
            "replace this agency",
            "Replace this agent's instructions",
            "Terminate this agent’s session",
            "Stop saying DeepWiki. It’s DeGenTWeb, and you should simply say dw",
        ):
            with self.subTest(body=body):
                self.assertIsNone(watcher.agent_lifecycle_command(body))


class TaskStemMailRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="omo-task-stem-mail.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        (self.root / "manager_mail").mkdir()
        self.manager = self.root / "work_manager_today.md"
        self.manager.write_text("manager log\n", encoding="utf-8")
        self.args = args_for(self.root, self.manager)

    def add_task(self, relative: str, target: str, *, status: str = "running") -> Path:
        task = self.root / relative
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(task_text(runat=target, managerat="main:0", status=status), encoding="utf-8")
        return task

    def test_current_manager_owner_survives_midnight_and_receives_mail(self) -> None:
        target = "omnigent://a29a5bd5525b47ceb563a1b28d7dbc3a"
        manager = self.root / "main_manager_agy.md"
        manager.write_text(task_text(runat=target, managerat="main:0", is_manager=True), encoding="utf-8")
        (self.root / "TODO.md").write_text(f"current:\nmain_manager_agy.md {target}\n", encoding="utf-8")
        args = watcher.replace(self.args, manager_file=None, manager_target=target)
        for date in ("2026-10-02", "2026-10-03"):
            with self.subTest(date=date), patch.object(watcher, "dated_manager_file", return_value=self.root / f"work_manager_{date}.md"):
                self.assertEqual(manager, watcher.current_manager_file(args))
        raw = b"From: Human <human@example.test>\r\nMessage-ID: <midnight@example.test>\r\nSubject: Email watcher seems broken\r\n\r\nPlease fix it.\r\n"
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
        ):
            watcher.handle_unseen(inbox, args)
        self.assertIn("(record and delegate manager_mail/7.txt)", manager.read_text(encoding="utf-8"))
        self.assertEqual({"7"}, watcher.load_processed_uids(watcher.processed_uids_path(args)))
        self.assertFalse((self.root / "work_manager_2026-10-03.md").exists())

    def test_current_manager_explicit_file_and_legacy_fallback(self) -> None:
        args = watcher.replace(self.args, manager_file=None)
        dated = self.root / "work_manager_2026-10-03.md"
        with patch.object(watcher, "dated_manager_file", return_value=dated):
            self.assertEqual(self.manager, watcher.current_manager_file(self.args))
            self.assertEqual(dated, watcher.current_manager_file(args))
            inactive = self.add_task("old_manager.md", "main:0", status="done")
            (self.root / "TODO.md").write_text(f"done:\n{inactive.name} main:0\n", encoding="utf-8")
            self.assertEqual(dated, watcher.current_manager_file(args))

    def test_current_manager_rejects_ambiguous_inactive_or_mismatched_owner(self) -> None:
        manager = self.root / "main_manager.md"
        other = self.root / "other_manager.md"
        args = watcher.replace(self.args, manager_file=None)
        for status, rows in (
            ("running", "main_manager.md main:0\nother_manager.md main:0\n"),
            ("running", "main_manager.md main:0\nother_manager.md wrong:0\n"),
            ("done", "main_manager.md main:0\n"),
            ("running", "main_manager.md wrong:0\n"),
        ):
            with self.subTest(status=status, rows=rows):
                manager.write_text(task_text(runat="main:0", managerat="main:1", is_manager=True, status=status), encoding="utf-8")
                other.write_text(task_text(runat="main:0", managerat="main:1", is_manager=True), encoding="utf-8")
                (self.root / "TODO.md").write_text(f"current:\n{rows}", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "manager target has no"):
                    watcher.current_manager_file(args)

    def test_missing_route_file_does_not_block_later_addressed_mail(self) -> None:
        worker = self.add_task("worker.md", "worker:2")
        (self.root / "TODO.md").write_text("current:\nworker.md worker:2\n", encoding="utf-8")
        args = watcher.replace(self.args, manager_file=self.root / "missing" / "manager.md")
        messages = {
            b"7": b"From: Human <human@example.test>\r\nMessage-ID: <missing@example.test>\r\nSubject: Untagged manager request\r\n\r\nPlease fix it.\r\n",
            b"8": b"From: Human <human@example.test>\r\nMessage-ID: <worker@example.test>\r\nSubject: [worker] New worker request\r\n\r\nPlease do this.\r\n",
        }
        inbox = Mock()
        inbox.uid.side_effect = lambda _command, uid, _fields: ("OK", [(b"RFC822", messages[uid.encode()])])
        with (
            patch.object(watcher, "search_sender_uids", return_value=set(messages)),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True) as mark_seen,
        ):
            watcher.handle_unseen(inbox, args)
        self.assertIn("(record and delegate manager_mail/8.txt)", worker.read_text(encoding="utf-8"))
        self.assertEqual({"8"}, watcher.load_processed_uids(watcher.processed_uids_path(args)))
        self.assertEqual({"7"}, watcher.load_processed_uids(watcher.unaccepted_pending_uids_path(args)))
        self.assertEqual(["8"], [entry.args[1] for entry in mark_seen.call_args_list])
        self.assertFalse(args.manager_file.exists())

    def test_inactive_manager_does_not_block_addressed_mail(self) -> None:
        manager = self.root / "inactive_manager.md"
        manager.write_text(task_text(runat="main:0", managerat="main:1", is_manager=True, status="done"), encoding="utf-8")
        worker = self.add_task("worker.md", "worker:2")
        (self.root / "TODO.md").write_text("current:\ninactive_manager.md main:0\nworker.md worker:2\n", encoding="utf-8")
        args = watcher.replace(self.args, manager_file=None)
        raw = b"From: Human <human@example.test>\r\nMessage-ID: <worker@example.test>\r\nSubject: [worker] Worker request\r\n\r\nPlease do this.\r\n"
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"8"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
        ):
            watcher.handle_unseen(inbox, args)
        self.assertIn("(record and delegate manager_mail/8.txt)", worker.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", manager.read_text(encoding="utf-8"))
        self.assertEqual({"8"}, watcher.load_processed_uids(watcher.processed_uids_path(args)))

    def test_source_or_amh_staging_write_failure_does_not_block_later_mail(self) -> None:
        for stage in ("source", "amh"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix="omo-email-staging.") as temporary:
                root = Path(temporary)
                manager = root / "manager.md"
                manager.write_text("manager log\n", encoding="utf-8")
                args = args_for(root, manager)
                args.state_dir.mkdir()
                if stage == "amh":
                    (args.state_dir / "amh-email-staging").write_text("not a directory", encoding="utf-8")
                subject = "[main] Status" if stage == "amh" else "First request"
                messages = {
                    "7": f"From: Human <human@example.test>\r\nMessage-ID: <failed@example.test>\r\nSubject: {subject}\r\n\r\nFirst request.\r\n".encode(),
                    "8": b"From: Human <human@example.test>\r\nMessage-ID: <later@example.test>\r\nSubject: Later request\r\n\r\nPlease do this.\r\n",
                }
                inbox = Mock()
                inbox.uid.side_effect = lambda _command, uid, _fields: ("OK", [(b"RFC822", messages[uid])])
                open_file = watcher.os.open

                def fail_first_source(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                    if stage == "source" and path == args.mail_dir / "7.txt":
                        raise OSError("first source write failed")
                    return open_file(path, flags, mode, dir_fd=dir_fd)

                bridge = Mock()
                bridge.route_from_watcher_subject.return_value.routes_to_amh = True
                with (
                    patch.object(watcher.os, "open", side_effect=fail_first_source),
                    patch.object(watcher, "search_sender_uids", return_value={b"7", b"8"}),
                    patch.object(watcher, "exact_human_sender", return_value=True),
                    patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
                    patch.object(watcher, "mark_seen_after_human_intake", return_value=True) as mark_seen,
                    patch.object(watcher, "fetch_gmail_metadata", return_value=("message7", "thread7", 0)),
                    patch.object(watcher, "amh_committed_operation", return_value=None),
                    patch.object(watcher, "load_amh_bridge_modules", return_value=(bridge, Mock())),
                    patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                ):
                    watcher.handle_unseen(inbox, args)
                self.assertEqual({"8"}, watcher.load_processed_uids(watcher.processed_uids_path(args)))
                self.assertEqual({"7"}, watcher.load_processed_uids(watcher.unaccepted_pending_uids_path(args)))
                self.assertEqual(["8"], [entry.args[1] for entry in mark_seen.call_args_list])
                self.assertFalse((args.mail_dir / "7.txt").exists())
                self.assertTrue((args.mail_dir / "8.txt").exists())
                text = manager.read_text(encoding="utf-8")
                self.assertIn("(record and delegate manager_mail/8.txt)", text)
                self.assertNotIn("manager_mail/7.txt", text)
                bridge.bridge_watcher_message.assert_not_called()

    def test_human_reply_routes_to_exact_current_worker_or_manager(self) -> None:
        worker = self.add_task("worker_0927.md", "dw:64")
        manager = self.add_task("submanager_0927.md", "dw:1")
        (self.root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n- submanager_0927.md dw:1\n", encoding="utf-8")
        for tag, task, target in (("worker_0927", worker, "dw:64"), ("submanager_0927", manager, "dw:1")):
            for task_tag in (tag, f"{tag}.md"):
                route = watcher.email_route(self.args, f"Re: [{task_tag}] Human original-thread subject")
                self.assertEqual((task, target), (route.manager_file, route.manager_target))
                self.assertEqual(target, watcher.lifecycle_subject_target(self.args, f"Re: [{task_tag}] Replace this agent"))
                self.assertTrue(watcher.indexed_task_stem_subject(self.root, f"Re: [{task_tag}] Human original-thread subject"))
                self.assertEqual("main:0", watcher.lifecycle_subject_target(self.args, f"Re: [{task_tag}] work", "main:0"))
        self.assertEqual("main:0", watcher.lifecycle_subject_target(self.args, "Re: [dw:64] work", "main:0"))
        self.add_task("main.md", "main:1")
        self.add_task("pb.md", "pb:1")
        (self.root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n- submanager_0927.md dw:1\n- main.md main:1\n- pb.md pb:1\n", encoding="utf-8")
        for tag in ("main", "pb"):
            self.assertFalse(watcher.indexed_task_stem_subject(self.root, f"Re: [{tag}] Human original-thread subject"))
            self.assertEqual(self.manager, watcher.email_route(self.args, f"Re: [{tag}] Human original-thread subject").manager_file)

    def test_untagged_reply_uses_authenticated_thread_owner_not_latest_sender(self) -> None:
        config = self.add_task("config_repair_0926.md", "config:1")
        (self.root / "TODO.md").write_text("current:\n- config_repair_0926.md config:1\n", encoding="utf-8")
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        message["In-Reply-To"] = "<manager-sent@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        subject = "Re: Config: use task names in launch prompts and mail"
        header = RecentHeader("human@example.test", subject, datetime.now().astimezone(), "<human-reply@example.test>", recipient="agent@example.test", thread_target="config:1")
        with patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")), patch.object(watcher, "find_recent_thread_matching", return_value=header) as lookup:
            target = watcher.verified_reply_owner_target(subject, message)
        self.assertEqual("config:1", target)
        lookup.assert_called_once()
        route = watcher.email_route(self.args, subject, verified_reply_target=target)
        self.assertEqual((config, "config:1"), (route.manager_file, route.manager_target))

    def test_wl1_tagged_human_reply_is_not_routed_to_config_thread_owner(self) -> None:
        main_manager = self.add_task("work_manager_0929.md", "wl:1")
        config = self.add_task("config_repair_0926.md", "config:1")
        (self.root / "TODO.md").write_text(
            "current:\n- work_manager_0929.md wl:1\n- config_repair_0926.md config:1\n",
            encoding="utf-8",
        )
        subject = "Re: [wl:1] Config repair: watcher restarted; delivery still being verified"
        raw = (
            b"From: Human <human@example.test>\r\nTo: agent@example.test\r\n"
            b"Message-ID: <human-reply@example.test>\r\nIn-Reply-To: <config-parent@example.test>\r\n"
            + f"Subject: {subject}\r\n\r\nDon't do work.\r\n".encode("utf-8")
        )
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        header = RecentHeader(
            "human@example.test", subject, datetime.now().astimezone(),
            "<human-reply@example.test>", recipient="agent@example.test", thread_target="config:1",
        )
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")),
            patch.object(watcher, "verified_direct_task_parent_target", return_value=""),
            patch.object(watcher, "find_recent_thread_matching", return_value=header),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            watcher.handle_unseen(inbox, self.args)
        self.assertIn("(pending)", main_manager.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", config.read_text(encoding="utf-8"))
        self.assertIn(f"Subject: {subject}", next((self.root / "manager_mail").glob("*.txt")).read_text(encoding="utf-8"))

    def test_explicit_task_tag_verifies_exact_sent_parent_without_full_thread_scan(self) -> None:
        task = self.add_task("tuesday_slides_0927.md", "wl:4")
        (self.root / "TODO.md").write_text("current:\ntuesday_slides_0927.md wl:4\n", encoding="utf-8")
        subject = "Re: [tuesday_slides_0927] DW presentation slides"
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        message["In-Reply-To"] = "<agent-parent@example.test>"
        message["References"] = "<older@example.test> <agent-parent@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        parent = RecentHeader(
            "agent@example.test", "Re: [tuesday_slides_0927] Earlier slides",
            datetime.now().astimezone(), "<agent-parent@example.test>", recipient="human@example.test",
        )
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.select.return_value = ("OK", [])
        client.uid.return_value = ("OK", [b"9530"])
        with (
            patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")),
            patch.object(watcher.imaplib, "IMAP4_SSL", return_value=client),
            patch.object(watcher, "fetch_recent_headers", return_value=[parent]),
            patch.object(watcher, "find_recent_thread_matching", side_effect=AssertionError("slow scan")),
        ):
            target = watcher.verified_reply_owner_target(subject, message)
        self.assertEqual("tuesday_slides_0927", target)
        self.assertEqual((task, "wl:4"), (watcher.email_route(self.args, subject, verified_reply_target=target).manager_file, "wl:4"))
        client.uid.assert_called_once_with("search", None, "HEADER", "Message-ID", '"<agent-parent@example.test>"')

    def test_explicit_task_tag_does_not_trust_wrong_sent_parent(self) -> None:
        header = RecentHeader(
            "human@example.test", "Re: [tuesday_slides_0927] DW presentation slides",
            None, "<human-reply@example.test>", references="<agent-parent@example.test>",
            recipient="agent@example.test", in_reply_to="<agent-parent@example.test>",
        )
        parent = RecentHeader(
            "agent@example.test", "Re: [different_task] Earlier mail",
            datetime.now().astimezone(), "<agent-parent@example.test>", recipient="human@example.test",
        )
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.select.return_value = ("OK", [])
        client.uid.return_value = ("OK", [b"9530"])
        with patch.object(watcher.imaplib, "IMAP4_SSL", return_value=client), patch.object(watcher, "fetch_recent_headers", return_value=[parent]):
            self.assertEqual("", watcher.verified_direct_task_parent_target(header.subject, header, Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")))

    def test_explicit_tmux_tag_verifies_exact_sent_parent_before_thread_scan(self) -> None:
        task = self.add_task("cc_dw26.md", "dw-manager-20260926:1")
        (self.root / "TODO.md").write_text("current:\ncc_dw26.md dw-manager-20260926:1\n", encoding="utf-8")
        subject = "Re: [dw-manager-20260926:1] Positives in CC 2014 dataset"
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        message["In-Reply-To"] = "<agent-parent@example.test>"
        message["References"] = "<agent-parent@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        parent = RecentHeader("agent@example.test", subject, datetime.now().astimezone(), "<agent-parent@example.test>", recipient="human@example.test")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.select.return_value = ("OK", [])
        client.uid.return_value = ("OK", [b"9530"])
        with (
            patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")),
            patch.object(watcher.imaplib, "IMAP4_SSL", return_value=client),
            patch.object(watcher, "fetch_recent_headers", return_value=[parent]),
            patch.object(watcher, "find_recent_thread_matching", side_effect=AssertionError("must not route by old thread owner")),
        ):
            target = watcher.verified_reply_owner_target(subject, message)
        self.assertEqual("dw-manager-20260926:1", target)
        self.assertEqual((task, target), (watcher.email_route(self.args, subject, verified_reply_target=target).manager_file, watcher.email_route(self.args, subject, verified_reply_target=target).manager_target))
        self.assertEqual("main:0", watcher.lifecycle_subject_target(self.args, subject, verified_reply_target="main:0"))

    def test_explicit_tmux_tag_rejects_different_sent_parent(self) -> None:
        subject = "Re: [dw-manager-20260926:1] Positives in CC 2014 dataset"
        header = RecentHeader("human@example.test", subject, None, "<human-reply@example.test>", references="<agent-parent@example.test>", recipient="agent@example.test", in_reply_to="<agent-parent@example.test>")
        parent = RecentHeader("agent@example.test", "Re: [dw:1] Earlier work", None, "<agent-parent@example.test>", recipient="human@example.test")
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.select.return_value = ("OK", [])
        client.uid.return_value = ("OK", [b"9530"])
        with patch.object(watcher.imaplib, "IMAP4_SSL", return_value=client), patch.object(watcher, "fetch_recent_headers", return_value=[parent]):
            self.assertEqual("", watcher.verified_direct_task_parent_target(header.subject, header, Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")))

    def test_exact_parent_intake_records_source_once_in_slides_task(self) -> None:
        task = self.add_task("tuesday_slides_0927.md", "wl:4")
        (self.root / "TODO.md").write_text("current:\ntuesday_slides_0927.md wl:4\n", encoding="utf-8")
        raw = (
            b"From: Human <human@example.test>\r\nTo: agent@example.test\r\n"
            b"Message-ID: <human-reply@example.test>\r\nIn-Reply-To: <agent-parent@example.test>\r\n"
            b"References: <older@example.test> <agent-parent@example.test>\r\n"
            b"Subject: Re: [tuesday_slides_0927] DW presentation slides\r\n\r\n"
            b"The figures and outline are missing.\r\n"
        )
        inbox = Mock()
        inbox.uid.return_value = ("OK", [(b"RFC822", raw)])
        parent = RecentHeader("agent@example.test", "Re: [tuesday_slides_0927] Earlier slides", datetime.now().astimezone(), "<agent-parent@example.test>", recipient="human@example.test")
        sent = Mock()
        sent.__enter__ = Mock(return_value=sent)
        sent.__exit__ = Mock(return_value=None)
        sent.select.return_value = ("OK", [])
        sent.uid.return_value = ("OK", [b"9530"])
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")),
            patch.object(watcher.imaplib, "IMAP4_SSL", return_value=sent),
            patch.object(watcher, "fetch_recent_headers", return_value=[parent]),
            patch.object(watcher, "find_recent_thread_matching", side_effect=AssertionError("slow scan")),
            patch.object(watcher, "push_email_ref", return_value=True),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            watcher.handle_unseen(inbox, self.args)
            watcher.handle_unseen(inbox, self.args)
        self.assertEqual(task.read_text().count("(pending)"), 1)
        self.assertEqual(task.read_text().count("manager_mail/7.txt"), 1)
        self.assertEqual((self.root / "manager_mail/7.txt").read_text().count("The figures and outline are missing."), 1)

    def test_explicit_manager_tag_routes_reply_to_manager_despite_old_thread_owner(self) -> None:
        self.add_task("config_repair_0926.md", "config:1")
        (self.root / "TODO.md").write_text("current:\n- config_repair_0926.md config:1\n", encoding="utf-8")
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        message["In-Reply-To"] = "<manager-sent@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        subject = "Re: [wl:1] Config: use task names in launch prompts and mail"
        header = RecentHeader("human@example.test", subject, datetime.now().astimezone(), "<human-reply@example.test>", recipient="agent@example.test", thread_target="config:1")
        with patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")), patch.object(watcher, "verified_direct_task_parent_target", return_value=""), patch.object(watcher, "find_recent_thread_matching", return_value=header):
            target = watcher.verified_reply_owner_target(subject, message)
        route = watcher.email_route(self.args, subject, verified_reply_target=target)
        self.assertEqual((self.manager, "main:0"), (route.manager_file, route.manager_target))

    def test_completed_unique_task_tag_routes_to_current_manager(self) -> None:
        worker = self.add_task("png_dw26.md", "worker:2")
        manager = self.add_task("dw_manager.md", "mgr:1")
        worker.write_text(task_text(runat="worker:2", managerat="mgr:1", status="done"), encoding="utf-8")
        manager.write_text(task_text(runat="mgr:1", managerat="main:0", is_manager=True), encoding="utf-8")
        (self.root / "TODO.md").write_text("current:\ndw_manager.md mgr:1\n", encoding="utf-8")
        route = watcher.email_route(self.args, "Re: [png_dw26] DW presentation slides", verified_reply_target="png_dw26")
        self.assertEqual((manager, "mgr:1"), (route.manager_file, route.manager_target))
        self.add_task("archive/png_dw26.md", "retired:1")
        with self.assertRaisesRegex(RuntimeError, "does not map to one current task"):
            watcher.email_route(self.args, "Re: [png_dw26] DW presentation slides", verified_reply_target="png_dw26")

    def test_untagged_reply_without_verified_ancestor_does_not_fall_back_to_manager(self) -> None:
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        message["In-Reply-To"] = "<unknown@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        subject = "Re: Config: use task names in launch prompts and mail"
        with patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")), patch.object(watcher, "find_recent_thread_matching", side_effect=SubjectInputError("ambiguous")), self.assertRaisesRegex(RuntimeError, "cannot be verified"):
            watcher.verified_reply_owner_target(subject, message)

    def test_missing_untagged_parent_stays_unrouted(self) -> None:
        message = EmailMessage()
        message["Message-ID"] = "<human-reply@example.test>"
        with self.assertRaisesRegex(RuntimeError, "no verifiable parent"):
            watcher.verified_reply_owner_target("Re: Config: use task names in launch prompts and mail", message)

    def test_unicode_subject_uses_ascii_prefix_and_exact_mime_ancestry(self) -> None:
        message = EmailMessage()
        message["Message-ID"] = "<unicode-reply@example.test>"
        message["In-Reply-To"] = "<agent-parent@example.test>"
        message["From"] = "human@example.test"
        message["To"] = "agent@example.test"
        subject = "Re: dw agent’s work"
        selected = RecentHeader("human@example.test", subject, datetime.now().astimezone(), "<unicode-reply@example.test>", recipient="agent@example.test", thread_target="worker_0927")
        with patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")), patch.object(watcher, "find_recent_thread_matching", return_value=selected) as subject_search:
            self.assertEqual("worker_0927", watcher.verified_reply_owner_target(subject, message))
        subject_search.assert_called_once()
        self.assertEqual("dw agent", subject_search.call_args.args[1])
        self.assertEqual("<unicode-reply@example.test>", subject_search.call_args.kwargs["selected_header"].message_id)
        self.assertEqual("agent@example.test", subject_search.call_args.kwargs["route_profile"].agent_address)

    def test_verified_config_reply_cannot_replace_manager_from_subject_tag(self) -> None:
        self.add_task("config_repair_0926.md", "config:1")
        (self.root / "TODO.md").write_text("current:\n- config_repair_0926.md config:1\n", encoding="utf-8")
        subject = "Re: [wl:1] Config: use task names in launch prompts and mail"
        replace = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE)
        self.assertFalse(watcher.is_main_manager_replacement(self.args, subject, replace, "config:1"))
        self.assertEqual("config:1", watcher.lifecycle_subject_target(self.args, subject, "config:1"))
        self.assertEqual(self.manager, watcher.email_route(self.args, subject, verified_reply_target="config:1").manager_file)

    def test_received_untagged_reply_routes_to_verified_original_owner(self) -> None:
        task = self.add_task("config_repair_0926.md", "config:1")
        (self.root / "TODO.md").write_text("current:\n- config_repair_0926.md config:1\n", encoding="utf-8")
        raw = (
            b"From: Human <human@example.test>\r\nTo: agent@example.test\r\n"
            b"Message-ID: <human-reply@example.test>\r\nIn-Reply-To: <manager-reply@example.test>\r\n"
            b"Subject: Re: Config: use task names in launch prompts and mail\r\n\r\n"
            b"Why did the manager answer?\r\n"
        )
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        header = RecentHeader("human@example.test", "Re: Config: use task names in launch prompts and mail", datetime.now().astimezone(), "<human-reply@example.test>", recipient="agent@example.test")
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")),
            patch.object(watcher, "find_recent_thread_matching", return_value=watcher.replace(header, thread_target="config:1")),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            watcher.handle_unseen(inbox, self.args)
        self.assertIn("(pending)", task.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", self.manager.read_text(encoding="utf-8"))

    def test_received_reply_without_parent_stays_unread_and_unrouted(self) -> None:
        raw = b"From: Human <human@example.test>\r\nMessage-ID: <no-parent@example.test>\r\nSubject: Re: Config status\r\n\r\nPlease fix it.\r\n"
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        with patch.object(watcher, "search_sender_uids", return_value={b"7"}), patch.object(watcher, "exact_human_sender", return_value=True), patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False), patch.object(watcher, "mark_seen_after_human_intake") as mark_seen:
            watcher.handle_unseen(inbox, self.args)
        mark_seen.assert_not_called()
        self.assertNotIn("(pending)", self.manager.read_text(encoding="utf-8"))

    def test_reply_lookup_timeout_leaves_source_unread_without_pending(self) -> None:
        raw = b"From: Human <human@example.test>\r\nTo: agent@example.test\r\nMessage-ID: <reply@example.test>\r\nIn-Reply-To: <parent@example.test>\r\nSubject: Re: Config status\r\n\r\nPlease fix it.\r\n"
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        with patch.object(watcher, "search_sender_uids", return_value={b"7"}), patch.object(watcher, "exact_human_sender", return_value=True), patch.object(watcher, "configured_agent_mail", return_value=Mock(agent_address="agent@example.test", human_address="human@example.test", app_password="secret")), patch.object(watcher.imaplib, "IMAP4_SSL", side_effect=SubjectLookupTimeout("deadline")), patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False), patch.object(watcher, "mark_seen_after_human_intake") as mark_seen:
            watcher.handle_unseen(inbox, self.args)
        mark_seen.assert_not_called()
        self.assertNotIn("(pending)", self.manager.read_text(encoding="utf-8"))

    def test_received_manager_retagged_lifecycle_uses_verified_worker_stem(self) -> None:
        manager = self.add_task("submanager.md", "mgr:1")
        worker = self.add_task("worker_0927.md", "worker:2")
        manager.write_text(task_text(runat="mgr:1", managerat="main:0", is_manager=True), encoding="utf-8")
        worker.write_text(task_text(runat="worker:2", managerat="mgr:1", pending_items=("keep this",)), encoding="utf-8")
        (self.root / "TODO.md").write_text("current:\n- submanager.md mgr:1\n- worker_0927.md worker:2\n", encoding="utf-8")
        raw = (
            b"From: Human <human@example.test>\r\nMessage-ID: <human-reply@example.test>\r\n"
            b"In-Reply-To: <manager-sent@example.test>\r\n"
            b"Subject: Re: [main:0] Worker update\r\n\r\nReplace this agent.\r\n"
        )
        inbox = Mock()
        inbox.uid.return_value = "OK", [(b"RFC822", raw)]
        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "verified_reply_owner_target", return_value="worker_0927"),
            patch.object(watcher, "require_sendable_codex_target", return_value=None),
            patch.object(watcher, "execute_main_manager_replacement") as rotate,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            watcher.handle_unseen(inbox, self.args)
        rotate.assert_not_called()
        self.assertIn("requested_action: replace", worker.read_text(encoding="utf-8"))
        self.assertIn("(pending)", worker.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", self.manager.read_text(encoding="utf-8"))

    def test_ambiguous_archived_missing_or_done_task_fails_closed(self) -> None:
        task = self.add_task("worker_0927.md", "dw:64")
        (self.root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n", encoding="utf-8")
        self.add_task("202608/worker_0927.md", "old:1")
        self.assertEqual(task, watcher.email_route(self.args, "[worker_0927] Update").manager_file)
        self.assertEqual(task, watcher.email_route(self.args, "[worker_0927.md] Update").manager_file)
        for tag in ("missing",):
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                watcher.email_route(self.args, f"[{tag}] Update")
        duplicate = self.add_task("other/worker_0927.md", "dw:65")
        (self.root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n- other/worker_0927.md dw:65\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "one current task"):
            watcher.email_route(self.args, "[worker_0927] Update")
        duplicate.unlink()
        task.write_text(task_text(runat="dw:64", managerat="main:0", status="done"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "active owner"):
            watcher.email_route(self.args, "[worker_0927] Update")

    def test_colliding_legacy_agent_tag_still_reaches_amh_ingress(self) -> None:
        for index, tag in enumerate(("main", "pb"), start=7):
            with self.subTest(tag=tag):
                self.add_task(f"{tag}.md", f"{tag}:1")
                (self.root / "TODO.md").write_text(f"current:\n- {tag}.md {tag}:1\n", encoding="utf-8")
                raw_message = (
                    f"From: Human <human@example.test>\r\nMessage-ID: <legacy-{index}@example.test>\r\n"
                    f"Subject: Re: [{tag}] Status\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nStatus update\r\n"
                ).encode()

                class Client:
                    def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                        if command == "fetch" and arguments == (str(index), "(BODY.PEEK[])"):
                            return "OK", [(b"RFC822", raw_message)]
                        raise AssertionError((command, arguments))

                with (
                    patch.object(watcher, "search_sender_uids", return_value={str(index).encode()}),
                    patch.object(watcher, "verified_reply_owner_target", return_value=tag),
                    patch.object(watcher, "search_processed_sender_uids", return_value={str(index).encode()}),
                    patch.object(watcher, "exact_human_sender", return_value=True),
                    patch.object(watcher, "try_route_amh_message", return_value=watcher.AmhRouteDisposition.ADVANCED) as amh_route,
                    patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
                ):
                    self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]
                    amh_route.assert_called_once()


class AgentLifecycleRoutingTests(unittest.TestCase):
    def test_lifecycle_chain_accepts_login_shell_only_on_codex_path(self) -> None:
        self.assertEqual(
            {2, 3, 4},
            watcher._lifecycle_ancestor_chain(4, 1, {2: 1, 3: 2, 4: 3}),
        )
        self.assertNotIn(5, watcher._lifecycle_ancestor_chain(4, 1, {2: 1, 3: 2, 4: 3, 5: 1}))
        with self.assertRaisesRegex(RuntimeError, "process tree changed"):
            watcher._lifecycle_ancestor_chain(4, 1, {4: 3, 3: 4})

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="omo-email-lifecycle.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "manager_mail").mkdir()
        (self.root / "state").mkdir()
        self.main_file = self.root / "work_manager_today.md"
        self.main_file.write_text("main manager log\n", encoding="utf-8")
        self.manager_task = self.root / "submanager.md"
        self.manager_task.write_text(task_text(runat="mgr:1", managerat="main:0", is_manager=True), encoding="utf-8")
        self.worker_task = self.root / "worker.md"
        self.worker_task.write_text(
            task_text(runat="worker:2", managerat="mgr:1", pending_items=("first task", "second task")),
            encoding="utf-8",
        )
        self.mail = self.root / "manager_mail" / "42.txt"
        self.mail.write_text("Subject: Re: work\n\nTerminate this agent and preserve the queue.\n", encoding="utf-8")
        self.message_id = "<lifecycle-42@example.test>"
        self.args = args_for(self.root, self.main_file)
        manager_custody = patch.object(watcher, "require_sendable_codex_target", return_value=None)
        manager_custody.start()
        self.addCleanup(manager_custody.stop)
        legacy_reply_route = patch.object(watcher, "verified_reply_owner_target", side_effect=lambda subject, _message: watcher.subject_manager_target(subject) or watcher.subject_task_stem(subject))
        legacy_reply_route.start()
        self.addCleanup(legacy_reply_route.stop)

    def test_termination_preserves_queue_and_routes_manager_only_transport(self) -> None:
        before = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert before is not None
        route, pending_line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
            self.message_id,
        )
        after = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert after is not None

        self.assertEqual(("first task", "second task"), after.pending_task_items)
        self.assertEqual(before.pending_task_items, after.pending_task_items)
        self.assertEqual(self.worker_task.resolve(), route.manager_file)
        self.assertEqual("mgr:1", route.manager_target)
        self.assertTrue(route.pending_watcher_delivery)
        self.assertGreater(pending_line, 1)

        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = [pending_watcher.source_attachment(self.root, marker.delegate_source)]
        delivery = pending_watcher.marker_delivery_text(marker, attachments, manager_only=True)
        self.assertEqual("mgr:1", pending_watcher.marker_for_manager_target(self._pending_args(), marker))
        self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
        self.assertIn(
            "decision: stop this agent; retain its task queue",
            marker.block_text,
        )
        self.assertNotIn('["first task","second task"]', marker.block_text)
        self.assertIn("The Human asked to terminate worker:2.", delivery)
        self.assertNotIn("managers handle lifecycle only", marker.block_text)
        self.assertNotIn("the responsible worker reports directly", marker.block_text)
        self.assertNotIn("addressed_task:", delivery)
        self.assertNotIn("omo_queue_transfer.py", delivery)
        self.assertNotIn("pending_task_items_ordered_json", delivery)
        self.assertIn("Terminate this agent and preserve the queue.", delivery)
        self.assertIn("custody record is stored", delivery)
        self.assertNotIn("<human_instruction>", delivery)
        for target in ("mgr:1", "worker:2"):
            guard, payload = watcher.worker_lifecycle_report_guard(self.args.state_dir, target, self.message_id)
            self.assertEqual(payload, guard.read_bytes())

        consumed_worker = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        responsible_manager = parse_task_metadata(self.manager_task.read_text(encoding="utf-8"), self.root)
        assert consumed_worker is not None and responsible_manager is not None
        self.assertEqual(("first task", "second task"), consumed_worker.pending_task_items)
        self.assertEqual((), responsible_manager.pending_task_items)
        self.assertIn("(pending)", self.worker_task.read_text(encoding="utf-8"))
        event = pending_watcher.lifecycle_delivery_event(self._pending_args(), marker, "delivery-key", 1.0)
        self.assertEqual(self.root, event.clear_root)
        self.assertEqual(marker, event.clear_marker)

    def test_tagged_human_reply_routes_both_lifecycle_triggers_to_sole_manager(self) -> None:
        for action in ("Terminate", "Replace"):
            with self.subTest(action=action), tempfile.TemporaryDirectory(prefix="omo-lifecycle-tagged.") as raw_root:
                root = Path(raw_root)
                (root / "manager_mail").mkdir()
                (root / "state").mkdir()
                main_file = root / "work_manager_today.md"
                main_file.write_text("main manager log\n", encoding="utf-8")
                manager = root / "submanager.md"
                manager.write_text(task_text(runat="mgr:1", managerat="main:0", is_manager=True), encoding="utf-8")
                worker = root / "worker_0927.md"
                worker.write_text(task_text(runat="worker:2", managerat="mgr:1", pending_items=("first task", "second task")), encoding="utf-8")
                (root / "TODO.md").write_text("current:\nworker_0927.md worker:2\nsubmanager.md mgr:1\n", encoding="utf-8")
                args = args_for(root, main_file)
                raw_message = (
                    b"From: Human <human@example.test>\r\n"
                    b"Message-ID: <tagged-lifecycle@example.test>\r\n"
                    b"Subject: Re: [worker_0927] worker update\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                    + f"{action} this agent. Preserve the queue.\r\n".encode()
                )

                class Client:
                    def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                        if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                            return "OK", [(b"RFC822", raw_message)]
                        raise AssertionError((command, arguments))

                events: list[str] = []

                def stop_before_seen(*_args: object) -> None:
                    events.append("stopped")

                def mark_seen(*_args: object) -> bool:
                    events.append("seen")
                    return True

                with (
                    patch.object(watcher, "search_sender_uids", return_value={b"7"}),
                    patch.object(watcher, "verified_reply_owner_target", return_value="worker_0927"),
                    patch.object(watcher, "exact_human_sender", return_value=True),
                    patch.object(watcher, "require_sendable_codex_target", return_value=None),
                    patch.object(watcher, "stop_terminated_lifecycle_agent", side_effect=stop_before_seen) as stop,
                    patch.object(watcher, "mark_seen_after_human_intake", side_effect=mark_seen),
                    patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
                    patch.object(watcher, "try_route_amh_message") as amh_route,
                ):
                    self.assertTrue(watcher.handle_unseen(Client(), args))  # type: ignore[arg-type]

                stop.assert_called_once()
                self.assertEqual(["stopped", "seen"], events)
                self.assertEqual("worker:2", stop.call_args.args[2])
                if action == "Terminate":
                    self.assertIn("requested_action: terminate", worker.read_text(encoding="utf-8"))
                else:
                    self.assertIn("requested_action: replace", worker.read_text(encoding="utf-8"))

                amh_route.assert_not_called()
                marker = pending_watcher.find_markers(root, [worker])[0]
                attachments = [pending_watcher.source_attachment(root, marker.delegate_source)]
                self.assertEqual("mgr:1", pending_watcher.marker_for_manager_target(
                    pending_watcher.Args(root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1.0, status_script=Path("/bin/false"), once=True, dry_run=True), marker
                ))
                self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
                self.assertIn(f"requested_action: {action.lower()}", marker.block_text)
                self.assertIn("manager_mail/7.txt", marker.block_text)
                if action == "Replace":
                    self.assertIn("give its successor the complete original Human request", marker.block_text)
                else:
                    self.assertIn("retain its task queue", marker.block_text)
                metadata = parse_task_metadata(worker.read_text(encoding="utf-8"), root)
                assert metadata is not None
                self.assertEqual(("first task", "second task"), metadata.pending_task_items)
                self.assertNotIn("(pending)", manager.read_text(encoding="utf-8"))

    def test_ambiguous_tagged_lifecycle_reply_never_selects_a_worker(self) -> None:
        (self.root / "TODO.md").write_text(
            "current:\nworker_0927.md worker:2\narchive/worker_0927.md worker:3\n", encoding="utf-8"
        )
        (self.root / "archive").mkdir()
        for relative, target in (("worker_0927.md", "worker:2"), ("archive/worker_0927.md", "worker:3")):
            (self.root / relative).write_text(task_text(runat=target, managerat="mgr:1"), encoding="utf-8")
        subject = "Re: [worker_0927] work"
        self.assertEqual("", watcher.lifecycle_subject_target(self.args, subject))
        self.mail.write_text(f"Subject: {subject}\n\nReplace this agent.\n", encoding="utf-8")
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, subject,
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE), self.message_id,
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        self.assertIn("requested_action: review", self.main_file.read_text(encoding="utf-8"))
        self.assertNotIn("pending_task_items_ordered_json:", self.main_file.read_text(encoding="utf-8"))
        self.assertFalse(any("(pending)" in (self.root / path).read_text(encoding="utf-8") for path in ("worker_0927.md", "archive/worker_0927.md")))

    def test_main_managed_worker_routes_action_to_main_log(self) -> None:
        self.worker_task.write_text(
            task_text(runat="worker:2", managerat="main:0", pending_items=("preserve me",)),
            encoding="utf-8",
        )
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
            self.message_id,
        )
        text = self.worker_task.read_text(encoding="utf-8")
        self.assertEqual(self.worker_task.resolve(), route.manager_file)
        self.assertEqual("main:0", route.manager_target)
        self.assertIn("requested_action: terminate", text)
        self.assertIn("responsible_manager_target: main:0", text)
        self.assertNotIn('pending_task_items_ordered_json:', text)
        consumed = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert consumed is not None
        self.assertEqual(("preserve me",), consumed.pending_task_items)
        self.assertLess(len(pending_watcher.find_markers(self.root, [self.worker_task])[0].block_text.splitlines()), 10)

    def test_replacement_routes_trailing_email_text_for_delivery_to_successor(self) -> None:
        command = watcher.agent_lifecycle_command("Replace this agent\nThe previous agent ignored the requested tests.")
        assert command is not None
        self.mail.write_text(
            "Subject: Re: work\n\nReplace this agent\nThe previous agent ignored the requested tests.\n",
            encoding="utf-8",
        )
        watcher.append_agent_lifecycle_pending(self.args, self.mail, "Re: [worker:2] work", command, self.message_id)
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        delivery = pending_watcher.marker_delivery_text(
            marker,
            [pending_watcher.source_attachment(self.root, marker.delegate_source)],
            manager_only=True,
        )

        self.assertIn("The Human asked to replace worker:2.", delivery)
        self.assertIn("The previous agent ignored the requested tests.", delivery)

    def test_manager_review_delivery_is_bounded_and_does_not_expand_referenced_records(self) -> None:
        manager_body = "manager history that must not be delivered\n" * 833
        self.main_file.write_text(manager_body, encoding="utf-8")
        source1717 = self.root / "manager_mail" / "1717.txt"
        source1717.write_text("unrelated B12 history\n" * 200, encoding="utf-8")
        self.worker_task.write_text(
            task_text(
                runat="dw:0",
                managerat="main:0",
                is_manager=True,
                status="long_running",
                pending_items=("prior item from manager_mail/1717.txt with unrelated B12 history",),
            )
            + ("addressed task history that must not be delivered\n" * 200),
            encoding="utf-8",
        )
        self.mail.write_text("Subject: Re: dw manager\n\nReview this lifecycle request.\n", encoding="utf-8")

        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [dw:0] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "the wording requires review"),
            self.message_id,
        )
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = pending_watcher.marker_attachments(self._pending_args(), marker)
        delivery = pending_watcher.marker_delivery_text(marker, attachments, manager_only=True)

        self.assertEqual("main:0", route.manager_target)
        self.assertEqual(["manager_mail/42.txt"], [attachment.source for attachment in attachments])
        self.assertLessEqual(len(delivery), pending_watcher.LIFECYCLE_DELIVERY_CHAR_LIMIT)
        self.assertIn("An exact agent lifecycle command for dw:0 needs manager handling", delivery)
        self.assertNotIn("addressed_task:", delivery)
        self.assertNotIn("responsible_manager_target:", delivery)
        self.assertNotIn("task_status:", delivery)
        self.assertIn("Review this lifecycle request.", delivery)
        self.assertNotIn("pending_task_items_ordered_json", delivery)
        self.assertNotIn("task_sha256_before_transport", delivery)
        self.assertNotIn("consume_command", delivery)
        self.assertNotIn("unrelated B12 history", delivery)
        self.assertNotIn("addressed task history", delivery)
        self.assertNotIn("manager history", delivery)

    def test_main_manager_termination_preserves_human_command(self) -> None:
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [main] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
            self.message_id,
        )
        text = self.main_file.read_text(encoding="utf-8")
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        self.assertIn("requested_action: terminate", text)
        self.assertNotIn("requested_action: review", text)
        self.assertNotIn("omo_task_status.py TASK.md done", text)

    def test_termination_stops_exact_worker_once_after_manager_transport(self) -> None:
        self.assertIsNotNone(watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE), self.message_id,
        ))
        pane = omo_codex_start.Pane("worker:2.0", "%901", "@901", "bunx", self.root, 901, 123)
        shell = omo_codex_start.Pane("worker:2.0", "%901", "@901", "sh", self.root, 902, 124)
        with (
            patch.object(omo_codex_start, "resolve_pane", return_value=pane),
            patch.object(omo_codex_start, "require_restartable_codex") as inspect,
            patch.object(omo_codex_start, "descendant_pids", return_value=set()),
            patch.object(omo_codex_start, "stop_unverified_replacement", return_value=shell) as stop,
        ):
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
        inspect.assert_called_once_with(pane)
        stop.assert_called_once_with(pane, 5.0)
        receipts = list((self.args.state_dir / "email-lifecycle-stops").glob("*.json"))
        self.assertEqual(1, len(receipts))
        result = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(("stopped", "worker:2", 901, 902), (
            result["outcome"], result["target"], result["pane_pid"], result["shell_pid"],
        ))
        self.assertEqual(("first task", "second task"), parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root).pending_task_items)

    def test_replacement_stops_exact_worker_once_and_preserves_its_queue(self) -> None:
        self.mail.write_text("Subject: Re: work\n\nReplace this agent. Preserve the queue.\n", encoding="utf-8")
        watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
            self.message_id,
        )
        pane = omo_codex_start.Pane("worker:2.0", "%901", "@901", "bunx", self.root, 901, 123)
        shell = omo_codex_start.Pane("worker:2.0", "%901", "@901", "sh", self.root, 902, 124)
        with (
            patch.object(omo_codex_start, "resolve_pane", return_value=pane),
            patch.object(omo_codex_start, "require_restartable_codex"),
            patch.object(omo_codex_start, "descendant_pids", return_value=set()),
            patch.object(omo_codex_start, "stop_unverified_replacement", return_value=shell) as stop,
        ):
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
        stop.assert_called_once_with(pane, 5.0)
        receipts = list((self.args.state_dir / "email-lifecycle-stops").glob("*.json"))
        self.assertEqual(1, len(receipts))
        self.assertEqual("replace", json.loads(receipts[0].read_text(encoding="utf-8"))["requested_action"])
        self.assertEqual(
            ("first task", "second task"),
            parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root).pending_task_items,
        )

    def test_lifecycle_stop_requires_matching_action_before_process_inspection(self) -> None:
        self.mail.write_text("Subject: Re: work\n\nReview this agent.\n", encoding="utf-8")
        watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW), self.message_id,
        )
        with patch.object(omo_codex_start, "resolve_pane") as pane:
            with self.assertRaisesRegex(RuntimeError, "exact Human action"):
                watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
            pane.assert_not_called()

    def test_termination_survives_manager_consuming_marker_before_stop(self) -> None:
        watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE), self.message_id,
        )
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        self.assertTrue(pending_watcher.clear_pending_marker_if_current(self.root, marker))
        pane = omo_codex_start.Pane("worker:2.0", "%901", "@901", "bunx", self.root, 901, 123)
        shell = omo_codex_start.Pane("worker:2.0", "%901", "@901", "sh", self.root, 902, 124)
        with (
            patch.object(omo_codex_start, "resolve_pane", return_value=pane),
            patch.object(omo_codex_start, "require_restartable_codex"),
            patch.object(omo_codex_start, "descendant_pids", return_value=set()),
            patch.object(omo_codex_start, "stop_unverified_replacement", return_value=shell) as stop,
        ):
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
        stop.assert_called_once_with(pane, 5.0)
        self.assertNotIn("(pending)", self.worker_task.read_text(encoding="utf-8"))

    def test_unknown_termination_receipt_does_not_retry_stop(self) -> None:
        watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE), self.message_id,
        )
        pane = omo_codex_start.Pane("worker:2.0", "%901", "@901", "bunx", self.root, 901, 123)
        with (
            patch.object(omo_codex_start, "resolve_pane", return_value=pane),
            patch.object(omo_codex_start, "require_restartable_codex"),
            patch.object(omo_codex_start, "descendant_pids", return_value=set()),
            patch.object(omo_codex_start, "stop_unverified_replacement", side_effect=RuntimeError("outcome unknown")) as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "outcome unknown"):
                watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
            with self.assertRaisesRegex(RuntimeError, "prior termination outcome is unknown"):
                watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
        stop.assert_called_once()

    def test_termination_preserves_unrelated_child_process(self) -> None:
        watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE), self.message_id,
        )
        pane = omo_codex_start.Pane("worker:2.0", "%901", "@901", "bunx", self.root, 901, 123)
        original_read_bytes = Path.read_bytes
        with (
            patch.object(omo_codex_start, "resolve_pane", return_value=pane),
            patch.object(omo_codex_start, "require_restartable_codex"),
            patch.object(omo_codex_start, "descendant_pids", return_value={902}),
            patch.object(Path, "read_bytes", autospec=True) as read_bytes,
            patch.object(omo_codex_start, "stop_unverified_replacement") as stop,
        ):
            read_bytes.side_effect = lambda path: b"python3\0crawler.py\0" if str(path) == "/proc/902/cmdline" else original_read_bytes(path)
            with self.assertRaisesRegex(RuntimeError, "unrelated child process"):
                watcher.stop_terminated_lifecycle_agent(self.args, self.mail, "worker:2")
            stop.assert_not_called()

    def test_termination_stops_only_one_disposable_tmux_pane(self) -> None:
        session = f"cfgstop{time.time_ns()}"
        executable = "node -e 'setInterval(()=>{},1000)'"
        created = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "agent", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, created.returncode, created.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        protected = subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", "protected", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, protected.returncode, protected.stderr)
        agent_target, protected_target = f"{session}:0", f"{session}:1"
        original = omo_codex_start.resolve_pane(agent_target)
        protected_pane = omo_codex_start.resolve_pane(protected_target)
        self.worker_task.write_text(
            task_text(runat=agent_target, managerat="mgr:1")
            + "\n(pending)\n(record and delegate manager_mail/42.txt)\n"
            + "(email watcher lifecycle-command explanation; generated by the watcher, not Human text)\n"
            + "requested_action: terminate\n(for manager)\n",
            encoding="utf-8",
        )
        (self.root / "TODO.md").write_text(f"current:\nworker.md {agent_target}\n", encoding="utf-8")
        with patch.object(omo_codex_start, "require_restartable_codex", side_effect=omo_codex_start.verify_same_process):
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, agent_target)
        stopped = omo_codex_start.resolve_pane(agent_target)
        current_protected = omo_codex_start.resolve_pane(protected_target)
        self.assertEqual(original.pane_id, stopped.pane_id)
        self.assertNotEqual(original.pane_pid, stopped.pane_pid)
        self.assertIn(stopped.command, omo_codex_start.SHELL_COMMANDS)
        self.assertEqual((protected_pane.pane_id, protected_pane.pane_pid, protected_pane.start_ticks),
                         (current_protected.pane_id, current_protected.pane_pid, current_protected.start_ticks))

    def test_replacement_stops_only_one_disposable_tmux_pane(self) -> None:
        self.mail.write_text("Subject: Re: work\n\nReplace this agent. Keep its current work.\n", encoding="utf-8")
        session = f"cfgreplace{time.time_ns()}"
        executable = "node -e 'setInterval(()=>{},1000)'"
        created = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "agent", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, created.returncode, created.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        protected = subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", "protected", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, protected.returncode, protected.stderr)
        agent_target, protected_target = f"{session}:0", f"{session}:1"
        original = omo_codex_start.resolve_pane(agent_target)
        protected_pane = omo_codex_start.resolve_pane(protected_target)
        self.worker_task.write_text(
            task_text(runat=agent_target, managerat="mgr:1", pending_items=("preserved work",))
            + "\n(pending)\n(record and delegate manager_mail/42.txt)\n"
            + "requested_action: replace\n(for manager)\n",
            encoding="utf-8",
        )
        (self.root / "TODO.md").write_text(f"current:\nworker.md {agent_target}\n", encoding="utf-8")
        with patch.object(omo_codex_start, "require_restartable_codex", side_effect=omo_codex_start.verify_same_process):
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, agent_target)
            watcher.stop_terminated_lifecycle_agent(self.args, self.mail, agent_target)
        stopped = omo_codex_start.resolve_pane(agent_target)
        current_protected = omo_codex_start.resolve_pane(protected_target)
        self.assertEqual(original.pane_id, stopped.pane_id)
        self.assertNotEqual(original.pane_pid, stopped.pane_pid)
        self.assertIn(stopped.command, omo_codex_start.SHELL_COMMANDS)
        self.assertEqual((protected_pane.pane_pid, protected_pane.start_ticks), (current_protected.pane_pid, current_protected.start_ticks))
        self.assertEqual(("preserved work",), parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root).pending_task_items)

    def test_main_termination_stops_only_its_disposable_pane(self) -> None:
        session = f"cfgmainstop{time.time_ns()}"
        executable = "node -e 'setInterval(()=>{},1000)'"
        started = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "manager", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, started.returncode, started.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        protected = subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", "protected", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, protected.returncode, protected.stderr)
        target = f"{session}:0"
        args = watcher.replace(self.args, manager_target=target)
        watcher.append_agent_lifecycle_pending(args, self.mail, f"Re: [{target}] work", watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE), self.message_id)
        previous = omo_codex_start.resolve_pane(target)
        protected_before = omo_codex_start.resolve_pane(f"{session}:1")
        with patch.object(omo_codex_start, "require_restartable_codex", side_effect=omo_codex_start.verify_same_process) as inspect:
            watcher.stop_terminated_lifecycle_agent(args, self.mail, target)
            watcher.stop_terminated_lifecycle_agent(args, self.mail, target)
        inspect.assert_called_once_with(previous)
        self.assertNotEqual(previous.pane_pid, omo_codex_start.resolve_pane(target).pane_pid)
        protected_after = omo_codex_start.resolve_pane(f"{session}:1")
        self.assertEqual((protected_before.pane_pid, protected_before.start_ticks), (protected_after.pane_pid, protected_after.start_ticks))

    def test_main_replacement_stops_real_pane_before_starting_successor(self) -> None:
        session = f"cfgmainreplace{time.time_ns()}"
        executable = "node -e 'setInterval(()=>{},1000)'"
        started = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "manager", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, started.returncode, started.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        protected = subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", "protected", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, protected.returncode, protected.stderr)
        target = f"{session}:0"
        args = watcher.replace(self.args, manager_target=target)
        self.mail.write_text(f"Subject: Re: [{target}] work\n\nReplace this agent. Keep original request.\n", encoding="utf-8")
        original = omo_codex_start.resolve_pane(target)
        protected_before = omo_codex_start.resolve_pane(f"{session}:1")
        audit = self.root / "main-replacement-audit.json"
        audit.write_text(json.dumps({"outcome": "succeeded", "target": target, "replacement_email_file": str(self.mail)}), encoding="utf-8")
        prepared = Mock(pane=rotation.resolve_exact_pane(target))

        def check_stopped(_prepared: object) -> Path:
            self.assertGreater(input_locks._thread_lock_depths().get(input_locks.tmux_input_lock_path(target), 0), 0)
            shell = omo_codex_start.resolve_pane(target)
            self.assertEqual(original.pane_id, shell.pane_id)
            self.assertNotEqual(original.pane_pid, shell.pane_pid)
            self.assertIn(shell.command, omo_codex_start.SHELL_COMMANDS)
            return audit

        with (
            patch.object(rotation, "preflight", return_value=prepared),
            patch.object(rotation, "execute_rotation", side_effect=check_stopped) as launch,
            patch.object(omo_codex_start, "require_restartable_codex", side_effect=omo_codex_start.verify_same_process),
        ):
            first = watcher.execute_main_manager_replacement(args, self.mail)
            second = watcher.execute_main_manager_replacement(args, self.mail)
        self.assertEqual(first, second)
        launch.assert_called_once()
        protected_after = omo_codex_start.resolve_pane(f"{session}:1")
        self.assertEqual((protected_before.pane_pid, protected_before.start_ticks), (protected_after.pane_pid, protected_after.start_ticks))

    def test_authenticated_mail_terminates_disposable_worker_before_marking_seen(self) -> None:
        session = f"cfgmailstop{time.time_ns()}"
        executable = "node -e 'setInterval(()=>{},1000)'"
        started = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "agent", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, started.returncode, started.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        protected = subprocess.run(["tmux", "new-window", "-d", "-t", session, "-n", "protected", executable], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(0, protected.returncode, protected.stderr)
        target = f"{session}:0"
        self.worker_task.write_text(task_text(runat=target, managerat="mgr:1", pending_items=("keep me",)), encoding="utf-8")
        (self.root / "TODO.md").write_text(f"current:\nworker.md {target}\nsubmanager.md mgr:1\n", encoding="utf-8")
        mime = (
            f"From: Human <human@example.test>\r\nMessage-ID: <exact-stop@example.test>\r\n"
            f"Subject: Re: [{target}] work\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "Terminate this agent.\r\n"
        ).encode()

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", mime)]
                raise AssertionError((command, arguments))

        previous = omo_codex_start.resolve_pane(target)
        protected_before = omo_codex_start.resolve_pane(f"{session}:1")

        def mark_after_stop(*_arguments: object, **_options: object) -> bool:
            self.assertNotEqual(previous.pane_pid, omo_codex_start.resolve_pane(target).pane_pid)
            return True

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "verified_reply_owner_target", return_value=target),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "mark_seen_after_human_intake", side_effect=mark_after_stop) as marked,
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
            patch.object(watcher, "try_route_amh_message", return_value=watcher.AmhRouteDisposition.FALLBACK),
            patch.object(omo_codex_start, "require_restartable_codex", side_effect=omo_codex_start.verify_same_process),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]
        marked.assert_called_once()
        self.assertNotEqual(previous.pane_pid, omo_codex_start.resolve_pane(target).pane_pid)
        protected_after = omo_codex_start.resolve_pane(f"{session}:1")
        self.assertEqual((protected_before.pane_pid, protected_before.start_ticks), (protected_after.pane_pid, protected_after.start_ticks))
        self.assertEqual(("keep me",), parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root).pending_task_items)

    def test_missing_main_manager_custody_refuses_fallback(self) -> None:
        self.main_file.unlink()
        with self.assertRaisesRegex(RuntimeError, "main-manager transport custody"):
            watcher.append_agent_lifecycle_pending(
                self.args,
                self.mail,
                "Re: [missing:4] work",
                watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
                self.message_id,
            )
        self.assertFalse(self.main_file.exists())

    def test_missing_main_manager_pane_refuses_transport(self) -> None:
        with (
            patch.object(watcher, "require_sendable_codex_target", side_effect=RuntimeError("target does not exist")),
            self.assertRaisesRegex(RuntimeError, "main-manager transport custody.*target does not exist"),
        ):
            watcher.append_agent_lifecycle_pending(
                self.args,
                self.mail,
                "Re: [main] work",
                watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
                self.message_id,
            )
        self.assertEqual("main manager log\n", self.main_file.read_text(encoding="utf-8"))

    def test_main_manager_replacement_uses_manager_rotation(self) -> None:
        self.mail.write_bytes(
            b"Subject: Re: [wl:1] DeepWiki root ownership restored\n\n"
            b"Replace this agent.\r\nPrevious main manager failed to follow the replacement request."
        )
        command = watcher.agent_lifecycle_command(
            "Replace this agent.\r\nPrevious main manager failed to follow the replacement request."
        )
        assert command is not None
        live_args = watcher.replace(self.args, manager_target="wl:1")
        route, _line = watcher.append_agent_lifecycle_pending(
            live_args,
            self.mail,
            "Re: [wl:1] DeepWiki root ownership restored",
            command,
            self.message_id,
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        text = self.main_file.read_text(encoding="utf-8")
        self.assertIn("requested_action: replace", text)
        self.assertIn("decision: stop this agent; give its successor the complete original Human request", text)
        self.assertNotIn("Previous main manager failed to follow the replacement request.", text)
        self.assertLess(len(pending_watcher.find_markers(self.root, [self.main_file])[0].block_text.splitlines()), 10)

    def test_live_wl1_replacement_invokes_rotation_instead_of_forwarding_to_old_agent(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Subject: Re: [wl:1] DeepWiki root ownership restored\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Replace this agent.\r\nPrevious main manager failed to follow the replacement request.\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("1751", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"1751"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "execute_main_manager_replacement", return_value="audit_record: /private/rotation.json") as rotate,
            patch.object(watcher, "push_email_ref") as direct_push,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), live_args))  # type: ignore[arg-type]

        rotate.assert_called_once()
        replacement_mail = rotate.call_args.args[1]
        self.assertEqual("manager_mail/1751.txt", str(replacement_mail.relative_to(self.root)))
        direct_push.assert_not_called()
        self.assertNotIn("(pending)", self.main_file.read_text(encoding="utf-8"))
        self.assertIn("authenticated main-manager replacement completed", self.main_file.read_text(encoding="utf-8"))

    def test_main_replacement_runner_is_exact_and_success_receipt_prevents_replay(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        audit = self.root / "state" / "rotation.json"
        audit.write_text(
            json.dumps({"outcome": "succeeded", "target": "wl:1.0", "replacement_email_file": str(self.mail)}) + "\n",
            encoding="utf-8",
        )
        events: list[str] = []
        prepared = Mock()
        with (
            patch.object(rotation, "preflight", side_effect=lambda _args: events.append("preflight") or prepared) as preflight,
            patch.object(rotation, "acquire_lock", side_effect=lambda _state: watcher.os.open("/dev/null", watcher.os.O_RDONLY)),
            patch.object(rotation, "resolve_exact_pane", return_value=prepared.pane),
            patch.object(watcher, "stop_terminated_lifecycle_agent", side_effect=lambda *_args: events.append("stop")) as stop,
            patch.object(rotation, "execute_rotation", side_effect=lambda _prepared: events.append("launch") or audit) as launch,
        ):
            first = watcher.execute_main_manager_replacement(live_args, self.mail)
            second = watcher.execute_main_manager_replacement(live_args, self.mail)

        self.assertEqual(first, second)
        self.assertEqual(["preflight", "stop", "launch"], events)
        preflight.assert_called_once()
        stop.assert_called_once_with(live_args, self.mail, "wl:1")
        launch.assert_called_once()
        args = preflight.call_args.args[0]
        self.assertEqual("wl:1", args.target)
        self.assertEqual(self.mail, args.replacement_email_file)

    def test_main_replacement_runner_rejects_handoff_or_failed_audit(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        with (
            patch.object(rotation, "preflight", side_effect=rotation.RotationError("bad pane")),
            patch.object(rotation, "acquire_lock", side_effect=lambda _state: watcher.os.open("/dev/null", watcher.os.O_RDONLY)),
            patch.object(watcher, "stop_terminated_lifecycle_agent") as stop,
        ):
            with self.assertRaisesRegex(watcher.MainReplacementUncertain, "main manager stopped; successor preflight failed: bad pane"):
                watcher.execute_main_manager_replacement(live_args, self.mail)
        stop.assert_called_once_with(live_args, self.mail, "wl:1")

    def test_main_replacement_startup_failure_stops_once_and_never_replays(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        prepared = Mock()
        with (
            patch.object(rotation, "preflight", return_value=prepared),
            patch.object(rotation, "acquire_lock", side_effect=lambda _state: watcher.os.open("/dev/null", watcher.os.O_RDONLY)),
            patch.object(rotation, "resolve_exact_pane", return_value=prepared.pane),
            patch.object(watcher, "stop_terminated_lifecycle_agent") as stop,
            patch.object(rotation, "execute_rotation", side_effect=rotation.RotationError("startup failed")) as launch,
        ):
            with self.assertRaisesRegex(watcher.MainReplacementUncertain, "startup failed"):
                watcher.execute_main_manager_replacement(live_args, self.mail)
            with self.assertRaisesRegex(watcher.MainReplacementUncertain, "do not retry"):
                watcher.execute_main_manager_replacement(live_args, self.mail)
        stop.assert_called_once()
        launch.assert_called_once()

    def test_stopped_main_manager_records_one_recovery_marker_without_live_codex(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        mail_digest = hashlib.sha256(self.mail.read_bytes()).hexdigest()
        receipt = live_args.state_dir / "email-lifecycle-stops" / f"{mail_digest}.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "outcome": "stopped", "requested_action": "replace", "target": "wl:1",
            "mail_sha256": mail_digest, "owner": str(self.main_file.resolve()),
        }), encoding="utf-8")
        decision = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "successor startup failed")
        with patch.object(watcher, "require_sendable_codex_target", side_effect=RuntimeError("pane is empty shell")):
            _route, first_line = watcher.append_main_lifecycle_review(live_args, self.mail, self.message_id, decision, "wl:1")
            _route, second_line = watcher.append_main_lifecycle_review(live_args, self.mail, self.message_id, decision, "wl:1")
        self.assertEqual(first_line, second_line)
        self.assertEqual(1, self.main_file.read_text(encoding="utf-8").count("(pending)"))
        self.assertIn("successor startup failed", self.main_file.read_text(encoding="utf-8"))

    def test_stopped_main_manager_recovery_rejects_wrong_receipt(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        receipt = live_args.state_dir / "email-lifecycle-stops" / f"{hashlib.sha256(self.mail.read_bytes()).hexdigest()}.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"outcome": "started", "target": "wl:1"}), encoding="utf-8")
        decision = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "successor startup failed")
        with patch.object(watcher, "require_sendable_codex_target", side_effect=RuntimeError("pane is empty shell")):
            with self.assertRaisesRegex(RuntimeError, "transport custody is unavailable"):
                watcher.append_main_lifecycle_review(live_args, self.mail, self.message_id, decision, "wl:1")
        self.assertNotIn("(pending)", self.main_file.read_text(encoding="utf-8"))

    def test_started_receipt_recovers_only_verified_stopped_main_pane(self) -> None:
        session = f"cfgmaincrash{time.time_ns()}"
        created = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-n", "manager", "node -e 'setInterval(()=>{},1000)'"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(0, created.returncode, created.stderr)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
        target = f"{session}:0"
        args = watcher.replace(self.args, manager_target=target)
        self.mail.write_text(f"Subject: Re: [{target}] work\n\nReplace this agent.\n", encoding="utf-8")
        digest = hashlib.sha256(self.mail.read_bytes()).hexdigest()
        original = omo_codex_start.resolve_pane(target)
        receipt = args.state_dir / "email-lifecycle-stops" / f"{digest}.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "outcome": "started", "requested_action": "replace", "target": target,
            "mail_sha256": digest, "owner": str(self.main_file.resolve()),
            "pane_id": original.pane_id, "pane_pid": original.pane_pid,
            "pane_start_ticks": original.start_ticks,
        }), encoding="utf-8")
        decision = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "startup uncertain")
        with patch.object(watcher, "require_sendable_codex_target", side_effect=RuntimeError("pane is empty shell")):
            with self.assertRaisesRegex(RuntimeError, "transport custody is unavailable"):
                watcher.append_main_lifecycle_review(args, self.mail, self.message_id, decision, target)
            stopped = omo_codex_start.stop_unverified_replacement(original, 5.0)
            self.assertEqual(original.pane_id, stopped.pane_id)
            _route, line = watcher.append_main_lifecycle_review(args, self.mail, self.message_id, decision, target)
        self.assertGreater(line, 0)
        self.assertEqual(1, self.main_file.read_text(encoding="utf-8").count("(pending)"))

    def test_uncertain_main_replacement_routes_one_review_without_retry(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Message-ID: <uncertain-main@example.test>\r\n"
            b"Subject: Re: [wl:1] manager update\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Replace this agent. Make the explanation simpler.\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
            patch.object(watcher, "execute_main_manager_replacement", side_effect=watcher.MainReplacementUncertain("unverified previous attempt")) as rotate,
        ):
            self.assertTrue(watcher.handle_unseen(Client(), live_args))  # type: ignore[arg-type]

        rotate.assert_called_once()
        markers = pending_watcher.find_markers(self.root, [self.main_file])
        self.assertEqual(1, len(markers))
        self.assertIn("requested_action: review", markers[0].block_text)
        self.assertIn("unverified previous attempt", markers[0].block_text)
        self.assertNotIn("decision: replace the main manager in place", markers[0].block_text)

    def test_started_main_replacement_receipt_fails_closed_without_new_rotation(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        digest = hashlib.sha256(self.mail.read_bytes()).hexdigest()
        directory = live_args.state_dir / "email-lifecycle-replacements"
        directory.mkdir(parents=True)
        (directory / f"{digest}.json").write_text(
            json.dumps({"mail_sha256": digest, "target": "wl:1", "outcome": "started", "audit_output": ""}), encoding="utf-8"
        )
        with patch.object(watcher.subprocess, "run") as execute:
            with self.assertRaisesRegex(watcher.MainReplacementUncertain, "do not retry"):
                watcher.execute_main_manager_replacement(live_args, self.mail)
        execute.assert_not_called()

    def test_new_tagged_message_is_not_reply_bound(self) -> None:
        self.assertFalse(watcher.lifecycle_reply_candidate("[worker:2] work"))
        self.assertTrue(watcher.lifecycle_reply_candidate("Re: [worker:2] work"))
        self.assertTrue(watcher.lifecycle_reply_candidate("Re: worker:2 work"))
        self.assertEqual("", watcher.lifecycle_subject_target(self.args, "[worker:2] work"))
        self.assertEqual("worker:2", watcher.lifecycle_subject_target(self.args, "Re: [worker:2] work"))
        self.assertEqual("worker:2", watcher.lifecycle_subject_target(self.args, "Re: worker:2 work"))

    def test_unresolved_amh_reply_command_is_intercepted_before_amh(self) -> None:
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Message-ID: <unresolved-amh@example.test>\r\n"
            b"Subject: Re: [pb] work\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Replace this agent\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "try_route_amh_message") as amh_route,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]

        amh_route.assert_not_called()
        text = self.main_file.read_text(encoding="utf-8")
        self.assertIn("requested_action: review", text)
        self.assertIn("no exact active task custody", text)

    def test_replay_converts_ordinary_worker_marker_before_delivery(self) -> None:
        self.worker_task.write_text(
            f"{self.worker_task.read_text(encoding='utf-8')}(pending)\n(record and delegate manager_mail/7.txt)\n",
            encoding="utf-8",
        )
        replay_mail = self.root / "manager_mail" / "7.txt"
        replay_mail.write_text("Subject: Re: work\n\nTerminate this agent\n", encoding="utf-8")
        watcher.save_processed_uids(watcher.unaccepted_pending_uids_path(self.args), {"7"})
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Message-ID: <replay-7@example.test>\r\n"
            b"Subject: Re: [worker:2] work\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Terminate this agent\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "search_processed_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "try_route_amh_message") as amh_route,
            patch.object(watcher, "push_email_ref") as direct_push,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]

        amh_route.assert_not_called()
        direct_push.assert_not_called()
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = [pending_watcher.source_attachment(self.root, marker.delegate_source)]
        self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
        self.assertEqual("mgr:1", pending_watcher.marker_for_manager_target(self._pending_args(), marker))
        self.assertIn("requested_action: terminate", marker.block_text)

    def test_ambiguous_owner_fails_closed_to_main_manager(self) -> None:
        duplicate = self.root / "worker_duplicate.md"
        duplicate.write_text(task_text(runat="worker:2", managerat="mgr:1"), encoding="utf-8")
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
            self.message_id,
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        self.assertIn("found 2", self.main_file.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", self.worker_task.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", duplicate.read_text(encoding="utf-8"))

    def test_lifecycle_transport_replay_is_idempotent(self) -> None:
        command = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE)
        _route, first_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command, self.message_id
        )
        after_first = self.worker_task.read_bytes()

        _route, replay_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command, self.message_id
        )

        self.assertEqual(after_first, self.worker_task.read_bytes())
        self.assertGreater(first_line, 0)
        self.assertGreater(replay_line, 0)
        self.assertEqual(1, len(pending_watcher.find_markers(self.root, [self.worker_task])))

    def test_failed_lifecycle_marker_append_rolls_back_new_guards(self) -> None:
        command = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE)
        with (
            patch.object(watcher, "append_lifecycle_marker_locked", side_effect=RuntimeError("append failed")),
            self.assertRaisesRegex(RuntimeError, "append failed"),
        ):
            watcher.append_bound_lifecycle_command(
                self.args,
                self.mail,
                self.message_id,
                command,
                "worker:2",
            )
        guard_dir = self.args.state_dir / "worker-lifecycle-report-guards"
        self.assertEqual([], list(guard_dir.iterdir()))

    def test_failed_main_review_append_rolls_back_new_guards(self) -> None:
        command = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "no active owner")
        with (
            patch.object(watcher, "append_lifecycle_marker_locked", side_effect=RuntimeError("append failed")),
            self.assertRaisesRegex(RuntimeError, "append failed"),
        ):
            watcher.append_main_lifecycle_review(
                self.args,
                self.mail,
                self.message_id,
                command,
                "worker:2",
            )
        guard_dir = self.args.state_dir / "worker-lifecycle-report-guards"
        self.assertEqual([], list(guard_dir.iterdir()))

    def _pending_args(self) -> pending_watcher.Args:
        return pending_watcher.Args(
            root=self.root,
            manager_url="",
            state=self.root / "seen.tsv",
            interval_s=1.0,
            full_scan_interval_s=1.0,
            idle_status_interval_s=1.0,
            status_script=Path("/bin/false"),
            once=True,
            dry_run=True,
            manager_target="main:0",
        )

if __name__ == "__main__":
    unittest.main()
