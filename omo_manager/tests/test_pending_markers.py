from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from email.utils import format_datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.email_idle_watcher import append_pending, current_manager_file, dated_manager_file, existing_source_pending_line, normalize_human_subject
from omo_manager.omo_email_subject import RecentHeader, fetch_recent_header, normalized_subject_key, prepare_subject, prepare_subject_and_headers, reply_headers_for_subject
from omo_manager.omo_pending_watch import Args, find_markers


def agent_pointer_paths(text: str) -> list[Path]:
    return [Path(match) for match in re.findall(r"^\(from agent [^ ]+ (/tmp/[^)]+)\)$", text, flags=re.MULTILINE)]


def assert_concise_agent_report(test: unittest.TestCase, report_text: str, *, agent: str, tmux: str, task_file: str, message: bytes | None = None) -> None:
    header, sep, _body = report_text.partition("message:\n")
    test.assertEqual("message:\n", sep)
    test.assertRegex(header, re.compile(rf"^\(sent from {re.escape(agent)} via omo_report\.sh tmux={re.escape(tmux)} time=\d{{2}}:\d{{2}} task-file={re.escape(task_file)}\)$", re.MULTILINE))
    if message is None:
        test.assertIn("[message-sha256: ", header)
    else:
        test.assertIn(f"[message-sha256: {hashlib.sha256(message).hexdigest()}]", header)
    test.assertNotIn("[omo-message-source: ", header)
    test.assertNotIn("(from agent ", header)
    test.assertNotIn("(report manager ", header)
    test.assertNotIn("report-file=", header)
    test.assertNotIn("message-file: ", header)
    test.assertNotIn("task-pointer: ", header)
    test.assertNotIn("tmux_pane_id=", header)
    test.assertNotIn("tmux_window_name=", header)


class PendingMarkerTests(unittest.TestCase):
    def test_email_pending_block_has_single_source_marker_and_is_not_generic_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            line = append_pending(root, mail)
            self.assertEqual(line, existing_source_pending_line(root, mail))
            path = dated_manager_file(root)
            text = path.read_text(encoding="utf-8")
            self.assertIn("(from email manager_mail/4002.txt)", text)
            self.assertNotIn("[source: email manager_mail/4002.txt]", text)
            self.assertNotIn("[summary: human reply to manager]", text)
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            self.assertEqual("email", markers[0].source)
            self.assertIn("origin=human source=email action=ack-human", markers[0].ref)

    def test_manual_pending_block_is_human_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            path.write_text("(pending)\nplease handle this\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("human", markers[0].origin)
            self.assertEqual("manual", markers[0].source)
            self.assertEqual("ack-human", markers[0].action)

    def test_agent_source_marker_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                "(pending)\n"
                "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done tmux_pane=%22]\n"
                "(from agent agent-4002 via omo_report.sh status=done)\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("agent", markers[0].source)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_agent_source_marker_later_in_pending_block_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                "(pending)\n"
                "(report manager 2026-06-10 12:02 agent=agent-4002 status=done)\n"
                "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_manager_agent_problem_pending_block_is_agent_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            path.write_text(
                "(pending)\n"
                "[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=agent-problem]\n"
                "manager agent problem: running task marker needs attention.\n"
                "agent-problems: stuck_input=1\n",
                encoding="utf-8",
            )
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)
            self.assertEqual("agent", markers[0].source)
            self.assertEqual("no-human-ack", markers[0].action)

    def test_email_pending_block_uses_configured_active_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active_log = root / "work_manager_20260527.md"
            mail = root / "manager_mail" / "4333.txt"
            mail.parent.mkdir()
            _ = mail.write_text("body\n", encoding="utf-8")
            line = append_pending(root, mail, active_log)
            self.assertEqual(line, existing_source_pending_line(root, mail, active_log))
            self.assertFalse((root / "work_manager.md").exists())
            text = active_log.read_text(encoding="utf-8")
            self.assertIn("(from email manager_mail/4333.txt)", text)
            markers = find_markers(root, [active_log])
            self.assertEqual(1, len(markers))

    def test_email_watcher_idle_wait_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--idle-wait-s", "7.5", "--once"])
        self.assertEqual(7.5, args.idle_wait_s)

    def test_email_watcher_imap_timeout_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--imap-timeout-s", "8.5", "--once"])
        self.assertEqual(8.5, args.imap_timeout_s)

    def test_email_watcher_idle_eof_raises_instead_of_spinning(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = None

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                return b""

        with self.assertRaises(ConnectionError):
            watcher.idle_once(Client(), 0)

    def test_email_watcher_idle_start_read_timeout_raises_and_restores_socket_timeout(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = 42.0

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                raise TimeoutError("socket timed out")

        client = Client()
        with self.assertRaises(TimeoutError):
            watcher.idle_once(client, 0)
        self.assertEqual(42.0, client.sock.timeout_s)

    def test_email_watcher_idle_done_reads_buffered_completion(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Socket:
            timeout_s: float | None = None

            def gettimeout(self) -> float | None:
                return self.timeout_s

            def settimeout(self, timeout_s: float | None) -> None:
                self.timeout_s = timeout_s

        class Client:
            sent: list[bytes] = []
            lines = [b"+ idling\r\n", b"* 1 EXISTS\r\n", b"OMOIDLE OK IDLE terminated\r\n"]
            sock = Socket()

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def socket(self) -> Socket:
                return self.sock

            def readline(self) -> bytes:
                return self.lines.pop(0)

        client = Client()
        with patch.object(watcher.select, "select", return_value=([object()], [], [])):
            watcher.idle_once(client, 0)
        self.assertEqual([], client.lines)
        self.assertEqual([b"OMOIDLE IDLE\r\n", b"DONE\r\n"], client.sent)

    def test_email_watcher_manager_file_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--manager-file", "work_manager_20260527.md", "--once"])
        self.assertEqual(Path("/tmp/root/work_manager_20260527.md"), args.manager_file)

    def test_email_watcher_ignores_legacy_active_log_env_by_default(self) -> None:
        from unittest.mock import patch
        from omo_manager.email_idle_watcher import parse_args

        with patch.dict(os.environ, {"OMO_MANAGER_ACTIVE_LOG": "/tmp/root/work_manager.md"}):
            args = parse_args(["--root", "/tmp/root", "--once"])
        self.assertIsNone(args.manager_file)
        self.assertEqual(dated_manager_file(Path("/tmp/root")), current_manager_file(args))

    def test_write_mail_omits_redundant_self_headers(self) -> None:
        from email.message import EmailMessage
        from omo_manager.email_idle_watcher import Args as EmailArgs, write_mail

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msg = EmailMessage()
            msg["From"] = "Steven Sīchàng Hé <stevensichanghe@gmail.com>"
            msg["Subject"] = "Re: [omo_manager] Update"
            msg["Date"] = "Mon, 25 May 2026 15:09:06 -0700"
            msg.set_content("body\n")
            args = EmailArgs(root, "http://127.0.0.1:18790", root / "manager_mail", root / "state", root / "work_manager.md", True, "self@example.test", 900, Path("/bin/false"))
            path = write_mail(args, "4146", msg, str(msg["From"]), str(msg["Subject"]))
            text = path.read_text(encoding="utf-8")
            self.assertIn("Subject: Re: Update\n", text)
            self.assertNotIn("From:", text)
            self.assertNotIn("Date:", text)
            self.assertNotIn("UID:", text)

    def test_email_watcher_normalizes_manager_reply_subjects_for_storage(self) -> None:
        self.assertEqual("Re: Book demo regression fixed pb_book_demo_regression_5818.md", normalize_human_subject("Re: [omo_manager] Book demo regression fixed pb_book_demo_regression_5818.md"))
        self.assertEqual("Re: Book demo regression fixed pb_book_demo_regression_5818.md", normalize_human_subject("Re: [a] Book demo regression fixed pb_book_demo_regression_5818.md"))
        self.assertEqual("Re: Re: VL supervisor follow-up vl_supervisor_5410.md", normalize_human_subject("Re:[omo_manager] Re: VL supervisor follow-up vl_supervisor_5410.md"))

    def test_email_watcher_accepts_no_space_manager_reply_subject(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        self.assertIn("Re:[a]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertIn("Re:[omo_manager]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertNotIn("Re: [omo]", watcher.NORMAL_REPLY_SEARCH_PREFIXES)
        self.assertTrue(watcher.is_manager_subject("Re:[a] VL supervisor follow-up vl_supervisor_5410.md"))
        self.assertTrue(watcher.is_manager_subject("Re:[omo_manager] VL supervisor follow-up vl_supervisor_5410.md"))
        self.assertFalse(watcher.is_manager_subject("Re: [omo] direct agent follow-up"))
        self.assertFalse(watcher.is_manager_subject("Re: pb news"))
        self.assertFalse(watcher.is_manager_subject("Re: pb news setup"))
        self.assertNotIn("Re: pb news", watcher.NORMAL_REPLY_SEARCH_PREFIXES)

    def test_email_subject_normalization_strips_re_and_manager_tags(self) -> None:
        self.assertEqual("topic", normalized_subject_key("Re: Re: [a] Topic"))
        self.assertEqual("topic", normalized_subject_key("Re:[omo_manager] Topic"))
        self.assertEqual("topic", normalized_subject_key("[omo] Re: topic"))

    def test_email_subject_recent_thread_uses_reply_subject(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda key: key == "topic"
        try:
            self.assertEqual("Re: [a] Topic", prepare_subject("Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[omo_manager] Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("Re: [omo_manager] Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[a] Re: Topic"))
            self.assertEqual("Re: [a] Topic", prepare_subject("[a] [omo] Topic"))
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_recent_thread_builds_reply_headers(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_header = subject.recent_thread_header
        subject.recent_thread_header = lambda key: RecentHeader("me@example.test", "Topic", datetime.now().astimezone(), "<prior@example.test>", "<root@example.test>")
        try:
            self.assertEqual(
                {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"},
                reply_headers_for_subject("Re: [a] Topic"),
            )
        finally:
            subject.recent_thread_header = old_recent_thread_header

    def test_email_subject_prepare_subject_and_headers_uses_one_recent_lookup(self) -> None:
        from omo_manager import omo_email_subject as subject

        calls: list[str] = []
        old_recent_thread_header = subject.recent_thread_header

        def recent_thread_header(key: str) -> RecentHeader:
            calls.append(key)
            return RecentHeader("me@example.test", "Topic", datetime.now().astimezone(), "<prior@example.test>", "<root@example.test>")

        subject.recent_thread_header = recent_thread_header
        try:
            self.assertEqual(
                ("Re: [a] Topic", {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"}),
                prepare_subject_and_headers("Topic"),
            )
            self.assertEqual(["topic"], calls)
        finally:
            subject.recent_thread_header = old_recent_thread_header

    def test_email_subject_fetch_recent_header_reads_thread_headers(self) -> None:
        header_date = format_datetime(datetime.now().astimezone())

        class FakeClient:
            def uid(self, _command: str, *_args: str) -> tuple[str, list[tuple[bytes, bytes]]]:
                return (
                    "OK",
                    [
                        (
                            b"1",
                            (
                                f"Date: {header_date}\r\n"
                                "From: me@example.test\r\n"
                                "Subject: Topic\r\n"
                                "Message-ID: <prior@example.test>\r\n"
                                "References: <root@example.test>\r\n"
                                "\r\n"
                            ).encode(),
                        )
                    ],
                )

        header = fetch_recent_header(FakeClient(), "1")  # type: ignore[arg-type]
        self.assertIsNotNone(header)
        assert header is not None
        self.assertEqual("<prior@example.test>", header.message_id)
        self.assertEqual("<root@example.test>", header.references)

    def test_email_subject_lookup_error_falls_back_to_new_tag(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda _key: (_ for _ in ()).throw(RuntimeError("imap down"))
        try:
            self.assertEqual("[a] Topic", prepare_subject("Topic"))
            self.assertEqual("[a] Topic", prepare_subject("[omo_manager] Topic"))
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_lookup_deadline_falls_back_for_recorded_timeout_subject(self) -> None:
        from omo_manager import omo_email_subject as subject

        old_recent_thread_exists = subject.recent_thread_exists
        subject.recent_thread_exists = lambda _key: time.sleep(10) or False
        try:
            started_s = time.monotonic()
            with patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}):
                self.assertEqual("[a] Updates on manager email filtering manager_market_alert_email_filter_7564.md", prepare_subject("Updates on manager email filtering manager_market_alert_email_filter_7564.md"))
            self.assertLess(time.monotonic() - started_s, 1.0)
        finally:
            subject.recent_thread_exists = old_recent_thread_exists

    def test_email_subject_lookup_deadline_skips_slow_logout(self) -> None:
        from omo_manager import omo_email_subject as subject

        self.assertFalse(issubclass(subject.SubjectLookupTimeout, OSError))

        class SlowLogoutClient:
            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.readonly = readonly
                return ("OK", [])

            def uid(self, _command: str, *_args: str) -> tuple[str, list[bytes]]:
                time.sleep(10)
                return ("OK", [])

            def logout(self) -> None:
                time.sleep(10)

            def shutdown(self) -> None:
                return None

        started_s = time.monotonic()
        with patch.object(subject.imaplib, "IMAP4_SSL", return_value=SlowLogoutClient()), patch.object(subject, "parse_env_config", return_value={"host": "imap.example", "user": "me@example.com", "password": "secret"}), patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}):
            self.assertFalse(subject.has_recent_thread("topic"))
        self.assertLess(time.monotonic() - started_s, 1.0)

    def test_email_subject_slow_logout_preserves_found_recent_thread(self) -> None:
        from omo_manager import omo_email_subject as subject

        header_date = format_datetime(datetime.now().astimezone())

        class MatchingSlowLogoutClient:
            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.readonly = readonly
                return ("OK", [])

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes] | list[tuple[bytes, bytes]]]:
                if command == "search":
                    return ("OK", [b"1"])
                return ("OK", [(b"1", f"Date: {header_date}\r\nFrom: me@example.com\r\nSubject: Topic\r\n\r\n".encode())])

            def logout(self) -> None:
                time.sleep(10)

            def shutdown(self) -> None:
                return None

        started_s = time.monotonic()
        with patch.object(subject.imaplib, "IMAP4_SSL", return_value=MatchingSlowLogoutClient()), patch.object(subject, "parse_env_config", return_value={"host": "imap.example", "user": "me@example.com", "password": "secret"}), patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S": "0.05"}):
            self.assertTrue(subject.has_recent_thread("topic"))
        self.assertLess(time.monotonic() - started_s, 1.0)

    def test_legacy_email_source_block_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            _ = path.write_text("(pending)\n[source: email manager_mail/3979.txt]\n", encoding="utf-8")
            markers = find_markers(root, [path])
            self.assertEqual(1, len(markers))


    def test_email_watcher_submits_new_pending_ref_before_mark_seen(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] submit me"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"12"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file.relative_to(args.root), line_no, args.manager_target))
                return True

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(Path("work_manager_today.md"), 2, "wl:1.0")])
            self.assertEqual(len(client.stores), 1)
            self.assertIn("12	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_direct_push_runs_submit_before_mark_read(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            manager_file = root / "work_manager_today.md"
            state = Path(tmp) / "state"
            calls = []

            def run(push: watcher.EmailPush) -> bool:
                calls.append(push)
                return True

            old_run = watcher.run_email_push
            watcher.run_email_push = run
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.push_email_ref(args, 2))
                self.assertTrue(watcher.push_email_ref(args, 3))
            finally:
                watcher.run_email_push = old_run
            self.assertEqual([2, 3], [push.line_no for push in calls])
            push = calls[0]
            command = push.command
            env = push.env
            self.assertIn("--submit", command)
            self.assertEqual(env["OMO_MANAGER_TMUX_READY_TIMEOUT_S"], str(watcher.DEFAULT_EMAIL_PUSH_READY_TIMEOUT_S))
            self.assertEqual(env["OMO_MANAGER_TMUX_SUBMIT_VERIFY_TIMEOUT_S"], str(watcher.DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S))

            def fail(_push: watcher.EmailPush) -> bool:
                return False

            watcher.run_email_push = fail
            try:
                self.assertFalse(watcher.push_email_ref(args, 4))
            finally:
                watcher.run_email_push = old_run

    def test_email_watcher_tracks_manager_mail_counts(self) -> None:
        from datetime import datetime
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            recent_date = format_datetime(datetime.now().astimezone())

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"2 3"]
                        if "SINCE" in args:
                            return "OK", [b"1 2 3"]
                        return "OK", [b"1 2 3 4"]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg["Date"] = recent_date
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = lambda *_args: (_ for _ in ()).throw(AssertionError("threshold should not trigger"))
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            counts_text = (state / "email-manager-mail-counts.tsv").read_text(encoding="utf-8")
            self.assertIn("manager_total\t4\n", counts_text)
            self.assertIn("manager_unread\t2\n", counts_text)
            self.assertIn("manager_human_recent_total\t3\n", counts_text)

    def test_email_watcher_threshold_counts_ignore_ignored_uids(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-ignored-uids.tsv").write_text("1\t1\n2\t1\n3\t1\n", encoding="utf-8")

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"1 2 3"]
                        if "SINCE" in args:
                            return "OK", [b"1 2 3"]
                        return "OK", [b"1 2 3"]
                    raise AssertionError(command)

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = lambda *_args: (_ for _ in ()).throw(AssertionError("threshold should not trigger"))
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            counts_text = (state / "email-manager-mail-counts.tsv").read_text(encoding="utf-8")
            self.assertIn("manager_total\t0\n", counts_text)
            self.assertIn("manager_unread\t0\n", counts_text)
            self.assertIn("manager_human_recent_total\t0\n", counts_text)

    def test_email_watcher_triggers_unread_compression_once(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            total = " ".join(str(uid) for uid in range(1, 21)).encode()
            unread = " ".join(str(uid) for uid in range(1, 18)).encode()
            calls: list[tuple[int, str]] = []

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN":
                            return "OK", [unread]
                        return "OK", [total]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(Client(), args))
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "unread-compression")], calls)
            self.assertEqual(1, manager_text.count(watcher.threshold_marker("unread-compression")))
            self.assertIn("docs/manager-mail-compression.md", manager_text)
            self.assertIn("unread-compression\t1\n", (state / "email-manager-mail-thresholds.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_unread_threshold_can_retrigger_after_count_drops(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            calls: list[tuple[int, str]] = []

            class Client:
                unread_n = 17

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN":
                            return "OK", [" ".join(str(uid) for uid in range(1, self.unread_n + 1)).encode()]
                        return "OK", [b"1 2 3 4"]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            client = Client()
            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(client, args))
                client.unread_n = 16
                self.assertFalse(watcher.handle_manager_mail_thresholds(client, args))
                client.unread_n = 17
                self.assertTrue(watcher.handle_manager_mail_thresholds(client, args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "unread-compression"), (10, "unread-compression")], calls)
            self.assertEqual(2, manager_text.count(watcher.threshold_marker("unread-compression")))

    def test_email_watcher_recent_cleanup_threshold_filters_to_last_24h(self) -> None:
        from datetime import datetime, timedelta
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        now = datetime.now().astimezone()
        recent_date = format_datetime(now - timedelta(hours=1))
        old_date = format_datetime(now - timedelta(hours=25))
        uids = [str(uid) for uid in range(1, 66)]

        class Client:
            def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                if command == "search":
                    if "SINCE" in args:
                        return "OK", [" ".join(uids).encode()]
                    return "OK", [b""]
                if command == "fetch":
                    uid = str(args[0])
                    msg = EmailMessage()
                    msg["From"] = "Manager <me@example.com>"
                    msg["Subject"] = "[omo_manager] item"
                    msg["Date"] = old_date if uid == "65" else recent_date
                    msg.set_content("body")
                    return "OK", [(b"HEADER", msg.as_bytes())]
                raise AssertionError(command)

        counts = watcher.manager_mail_counts(Client(), "me@example.com", 24 * 60 * 60, 64, now)
        self.assertTrue(counts.recent_exact)
        self.assertEqual(64, counts.recent_total)

    def test_email_watcher_triggers_recent_cleanup_once(self) -> None:
        from datetime import datetime, timedelta
        from email.message import EmailMessage
        from email.utils import format_datetime
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            uids = [str(uid) for uid in range(1, 66)]
            encoded_uids = " ".join(uids).encode()
            recent_date = format_datetime(datetime.now().astimezone() - timedelta(hours=1))
            calls: list[tuple[int, str]] = []

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        return "OK", [encoded_uids]
                    if command == "fetch":
                        msg = EmailMessage()
                        msg["From"] = "Manager <me@example.com>"
                        msg["Subject"] = "[omo_manager] item"
                        msg["Date"] = recent_date
                        msg.set_content("body")
                        return "OK", [(b"HEADER", msg.as_bytes())]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int, kind: str) -> bool:
                calls.append((line_no, kind))
                return True

            old_push = watcher.push_manager_mail_threshold_ref
            watcher.push_manager_mail_threshold_ref = push
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                self.assertTrue(watcher.handle_manager_mail_thresholds(Client(), args))
                self.assertFalse(watcher.handle_manager_mail_thresholds(Client(), args))
            finally:
                watcher.push_manager_mail_threshold_ref = old_push
            manager_text = (root / "work_manager_today.md").read_text(encoding="utf-8")
            self.assertEqual([(2, "recent-cleanup")], calls)
            self.assertEqual(1, manager_text.count(watcher.threshold_marker("recent-cleanup")))
            self.assertIn("docs/manager-mail-cleanup.md", manager_text)
            self.assertIn("recent-cleanup\t1\n", (state / "email-manager-mail-thresholds.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_async_worker_start_failure_can_retry(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        class Thread:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread start failed")

        old_thread = watcher.threading.Thread
        old_started = watcher._email_push_worker_started
        watcher.threading.Thread = Thread
        watcher._email_push_worker_started = False
        try:
            with self.assertRaises(RuntimeError):
                watcher.start_email_push_worker()
            self.assertFalse(watcher._email_push_worker_started)
        finally:
            watcher.threading.Thread = old_thread
            watcher._email_push_worker_started = old_started

    def test_email_watcher_keeps_existing_pending_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "13.txt"
            mail.parent.mkdir()
            mail.write_text("body", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(pending)\n(from email manager_mail/13.txt)\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] retry me"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN" and self.stores:
                            return "OK", [b""]
                        if "UID" in args and "14:*" in args:
                            return "OK", [b""]
                        return "OK", [b"13"]
                    if command == "fetch":
                        raise AssertionError("existing pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return False

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [1, 1])
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_marks_existing_pending_read_after_submit_accepts(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            mail = root / "manager_mail" / "13.txt"
            mail.parent.mkdir()
            mail.write_text("body", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(pending)\n(from email manager_mail/13.txt)\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] retry me"
            msg.set_content("PWD: /tmp/agent-work\n\nagent body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if args and args[0] == "UNSEEN" and self.stores:
                            return "OK", [b""]
                        if "UID" in args and "14:*" in args:
                            return "OK", [b""]
                        return "OK", [b"13"]
                    if command == "fetch":
                        raise AssertionError("existing pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return True

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [1])
            self.assertEqual(client.stores, [("13", "+FLAGS", r"(\Seen)")])
            self.assertIn("13	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_rejects_omo_agent_reply_with_pwd(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo] direct agent reply"
            msg.set_content("body\n\nPWD: /tmp/agent-work\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"48"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "48.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_rejects_pb_news_reply(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: pb news"
            msg.set_content("PB news reply body.\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"50"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "50.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_ignores_manager_authored_reply_echo_with_pwd(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Manager <me@example.com>"
            msg["Subject"] = "Re: [a] Update on manager_email_watcher_read_after_forward_6901.md"
            msg.set_content(
                "Acknowledged. I will route this to a separate VL worker now.\n\n"
                "Scope I am giving it: inspect why VL build artifacts leaked into the manager work-log PWD.\n\n"
                "PWD: /ssd1/sichangheagent/work_logs\n"
            )

            class Client:
                fetches = 0
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"49"]
                    if command == "fetch":
                        self.fetches += 1
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            watcher.handle_unseen(client, args)
            self.assertEqual(1, client.fetches)
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail" / "49.txt").exists())
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())
            self.assertIn("49\t", (state / "email-ignored-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_recovers_processed_uid_without_current_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("41\t1\n", encoding="utf-8")
            calls = []
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] stale root"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        return "OK", [b"41"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return False

            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(from email manager_mail/41.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(calls, [2, 2])
            self.assertEqual(client.stores, [])
            self.assertIn("41	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_processed_uid_with_old_root_source_can_mark_read(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("42\t1\n", encoding="utf-8")
            (root / "work_manager_2026-06-13.md").write_text("(pending)\n(from email manager_mail/42.txt)\n", encoding="utf-8")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"42"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual(client.stores, [("42", "+FLAGS", r"(\Seen)")])

    def test_email_watcher_retries_unaccepted_processed_pending_in_old_log(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("43\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("43\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(pending)\n(from email manager_mail/43.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"43"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(old_log, 1)])
            self.assertEqual(client.stores, [])
            self.assertIn("43	", (state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_clears_unaccepted_pending_after_submit_accepts(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("49\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("49\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text("(pending)\n(from email manager_mail/49.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if self.stores:
                            return "OK", [b""]
                        return "OK", [b"49"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return True

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(manager_file, 1)])
            self.assertEqual(client.stores, [("49", "+FLAGS", r"(\Seen)")])
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_retries_unaccepted_unprocessed_pending_in_old_log(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-unaccepted-pending-uids.tsv").write_text("44\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(pending)\n(from email manager_mail/44.txt)\n", encoding="utf-8")
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"44"]
                    if command == "fetch":
                        raise AssertionError("unaccepted old pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_2026-06-14.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(old_log, 1)])
            self.assertEqual(client.stores, [])
            self.assertFalse((root / "work_manager_2026-06-14.md").exists())

    def test_email_watcher_accepts_processed_unaccepted_source_without_pending(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            old_log = root / "work_manager_2026-06-13.md"
            old_log.write_text("(done)\n(from email manager_mail/45.txt)\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"45"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertFalse(manager_file.exists())
            self.assertEqual(client.stores, [("45", "+FLAGS", r"(\Seen)")])
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_reprocesses_old_processed_unaccepted_uid_without_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("45\t1\n500\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("45\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] old unaccepted"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        if "UID" in args and "45" in args:
                            return "OK", [b"45"]
                        return "OK", [b""]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(from email manager_mail/45.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [])
            self.assertIn("45\t", (state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_accepts_unprocessed_stale_unaccepted_source(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-unaccepted-pending-uids.tsv").write_text("46\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text("(done)\n(from email manager_mail/46.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"46"]
                    if command == "fetch":
                        raise AssertionError("consumed source should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual("(done)\n(from email manager_mail/46.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("46", "+FLAGS", r"(\Seen)")])
            self.assertIn("46\t", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))
            self.assertFalse((state / "email-unaccepted-pending-uids.tsv").read_text(encoding="utf-8").strip())

    def test_email_watcher_retries_unaccepted_pending_with_distant_source_marker(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("47\t1\n", encoding="utf-8")
            (state / "email-unaccepted-pending-uids.tsv").write_text("47\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_2026-06-14.md"
            manager_file.write_text(
                "(pending)\n"
                "manager note line 1\n"
                "manager note line 2\n"
                "manager note line 3\n"
                "manager note line 4\n"
                "(from email manager_mail/47.txt)\n",
                encoding="utf-8",
            )
            calls = []

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"47"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(args: watcher.Args, line_no: int) -> bool:
                calls.append((args.manager_file, line_no))
                return False

            old_push = watcher.push_email_ref
            watcher.push_email_ref = push
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertEqual(calls, [(manager_file, 1)])
            self.assertEqual(client.stores, [])

    def test_email_watcher_keeps_new_pending_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] durable first"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"14"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(from email manager_mail/14.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [])
            self.assertFalse((state / "email-processed-uids.tsv").exists())

    def test_email_watcher_source_without_pending_stays_unread_when_submit_fails(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n(from email manager_mail/17.txt)\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] source only"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"17"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: False
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            text = manager_file.read_text(encoding="utf-8")
            self.assertIn("(done)\n(from email manager_mail/17.txt)\n\n(pending)\n(from email manager_mail/17.txt)\n", text)
            self.assertEqual(client.stores, [])

    def test_email_watcher_processed_source_without_pending_marks_read_without_duplicate_pending(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("18\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n(from email manager_mail/18.txt)\n", encoding="utf-8")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if "SINCE" in args:
                            return "OK", [b""]
                        return "OK", [b"18"]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual("(done)\n(from email manager_mail/18.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("18", "+FLAGS", r"(\Seen)")])

    def test_email_watcher_does_not_recover_old_processed_uid_without_source(self) -> None:
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("18\t1\n500\t1\n", encoding="utf-8")
            manager_file = root / "work_manager_today.md"
            manager_file.write_text("(done)\n", encoding="utf-8")

            class Client:
                fetches = 0

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        joined = " ".join(str(arg) for arg in args)
                        if "UID" in args and "18" in args:
                            raise AssertionError("old UID should not be searched for recovery")
                        if "UNSEEN" in joined:
                            return "OK", [b"18"]
                        return "OK", [b""]
                    if command == "fetch":
                        self.fetches += 1
                        raise AssertionError("old processed UID outside recovery window should not be fetched")
                    raise AssertionError(command)

            client = Client()
            args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
            watcher.handle_unseen(client, args)
            self.assertEqual("(done)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(0, client.fetches)

    def test_email_watcher_keeps_processed_state_when_mark_seen_returns_no(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] mark read fails"
            msg.set_content("body")

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"15"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        return "NO", [b"temporary failure"]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(Client(), args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(from email manager_mail/15.txt)\n", (root / "work_manager_today.md").read_text(encoding="utf-8"))
            self.assertIn("15	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_searches_unseen_and_seen_uid_range_after_processed_state(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("12\t1\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] already seen"
            msg.set_content("body")

            class Client:
                searches: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b""]
                        self.searches.append(args)
                        return "OK", [b"13"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                client = Client()
                args = watcher.Args(root, "", root / "manager_mail", state, root / "work_manager_today.md", True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertTrue(client.searches)
            self.assertEqual(client.searches[0][:6], (None, "UID", "13:*", "FROM", '"me@example.com"', "SUBJECT"))
            self.assertIn("13	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_marks_new_pending_read_after_submit_succeeds(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [a] push raises"
            msg.set_content("body\n\n> PWD: /tmp/agent-work\n")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"16"]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            pushes = []

            def run(push: watcher.EmailPush) -> bool:
                pushes.append(push)
                return True

            old_run = watcher.run_email_push
            watcher.run_email_push = run
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.run_email_push = old_run
            self.assertIn("(pending)\n(from email manager_mail/16.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("16", "+FLAGS", r"(\Seen)")])
            self.assertIn("16	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))
            self.assertEqual(1, len(pushes))
            self.assertEqual(2, pushes[0].line_no)

    def test_email_watcher_processes_lower_unread_uid_after_higher_processed_uid(self) -> None:
        from email.message import EmailMessage
        from omo_manager import email_idle_watcher as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "email-processed-uids.tsv").write_text("20\t1\n", encoding="utf-8")
            msg = EmailMessage()
            msg["From"] = "Human <me@example.com>"
            msg["Subject"] = "Re: [omo_manager] lower uid"
            msg.set_content("body")

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        if args and args[0] == "UNSEEN":
                            return "OK", [b"19"]
                        return "OK", [b""]
                    if command == "fetch":
                        return "OK", [(b"RFC822", msg.as_bytes())]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            old_push = watcher.push_email_ref
            watcher.push_email_ref = lambda *_args: True
            try:
                client = Client()
                manager_file = root / "work_manager_today.md"
                args = watcher.Args(root, "", root / "manager_mail", state, manager_file, True, "me@example.com", 0, Path("/bin/false"), manager_target="wl:1.0")
                watcher.handle_unseen(client, args)
            finally:
                watcher.push_email_ref = old_push
            self.assertIn("(pending)\n(from email manager_mail/19.txt)\n", manager_file.read_text(encoding="utf-8"))
            self.assertEqual(client.stores, [("19", "+FLAGS", r"(\Seen)")])
            self.assertIn("19	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_omo_report_writes_agent_pending_source_without_direct_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            duplicate_msg = Path(tmp) / "duplicate-msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            _ = duplicate_msg.write_text("done\n", encoding="utf-8")
            for message_file in (msg, duplicate_msg):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-4002",
                        "--message-file",
                        str(message_file),
                    ],
                    cwd=tmp,
                    env={**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            task = root / "task.md"
            text = task.read_text(encoding="utf-8")
            self.assertRegex(text, re.compile(r"^\(pending\)\n\(from agent agent-4002 /tmp/omo-agent-messages-\d+/agent-4002_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("[omo-message-source: ", text)
            self.assertNotIn("(report manager ", text)
            self.assertNotIn("[message-sha256: ", text)
            self.assertNotIn("message-file: ", text)
            self.assertNotIn("message:\n", text)
            self.assertNotIn("> done", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_path = report_paths[0]
            self.assertEqual(Path("/tmp") / f"omo-agent-messages-{os.getuid()}", report_path.parent)
            report_text = report_path.read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-4002", tmux="agent-4002", task_file="task.md", message=b"done\n")
            self.assertTrue(report_text.endswith("message:\ndone\n"))
            self.assertEqual(0o600, report_path.stat().st_mode & 0o777)
            self.assertEqual(0o700, report_path.parent.stat().st_mode & 0o777)
            self.assertNotIn("PWD:", text)
            self.assertNotIn("OPENCODE:", text)
            self.assertNotIn("TMUX:", text)
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)

    def test_omo_report_same_body_different_task_keeps_task_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("same done\n", encoding="utf-8")
            env = {**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
            for task_name in ("task-a.md", "task-b.md"):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        task_name,
                        "--status",
                        "done",
                        "--agent",
                        "agent-4002",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            task_a_text = (root / "task-a.md").read_text(encoding="utf-8")
            task_b_text = (root / "task-b.md").read_text(encoding="utf-8")
            task_a_paths = agent_pointer_paths(task_a_text)
            task_b_paths = agent_pointer_paths(task_b_text)
            self.assertEqual(1, len(task_a_paths))
            self.assertEqual(1, len(task_b_paths))
            self.assertNotEqual(task_a_paths[0], task_b_paths[0])
            assert_concise_agent_report(self, task_a_paths[0].read_text(encoding="utf-8"), agent="agent-4002", tmux="agent-4002", task_file="task-a.md")
            assert_concise_agent_report(self, task_b_paths[0].read_text(encoding="utf-8"), agent="agent-4002", tmux="agent-4002", task_file="task-b.md")

    def test_omo_report_rejects_corrupt_existing_tmp_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            env = {**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
            base_cmd = [
                str(Path.home() / ".config/omo_manager/omo_report.sh"),
                "--root",
                str(root),
                "--manager-url",
                "http://127.0.0.1:1",
                "--task-file",
                "task.md",
                "--status",
                "done",
                "--agent",
                "agent-4002",
                "--message-file",
                str(msg),
            ]
            first = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", first.stderr)
            self.assertEqual(0, first.returncode)
            report_path = agent_pointer_paths((root / "task.md").read_text(encoding="utf-8"))[0]
            report_path.write_text("wrong\n", encoding="utf-8")
            second = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(0, second.returncode)
            self.assertIn("stale or corrupt report file", second.stderr)

    def test_omo_report_rewrites_existing_verbose_tmp_pointer_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            msg.write_text("done\n", encoding="utf-8")
            env = {**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
            base_cmd = [
                str(Path.home() / ".config/omo_manager/omo_report.sh"),
                "--root",
                str(root),
                "--manager-url",
                "http://127.0.0.1:1",
                "--task-file",
                "task.md",
                "--status",
                "done",
                "--agent",
                "agent-4002",
                "--message-file",
                str(msg),
            ]
            first = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", first.stderr)
            self.assertEqual(0, first.returncode)
            task = root / "task.md"
            report_path = agent_pointer_paths(task.read_text(encoding="utf-8"))[0]
            message_hash = hashlib.sha256(b"done\n").hexdigest()
            report_path.write_text(
                "\n".join(
                    [
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        f"(report manager 2026-06-10 12:02 agent=agent-4002 status=done report-file={report_path})",
                        f"[message-sha256: {message_hash}]",
                        f"message-file: {report_path}",
                        f"task-file: {task}",
                        f"task-pointer: (from agent agent-4002 {report_path})",
                        "message:",
                        "done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            second = subprocess.run(base_cmd, cwd=tmp, env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual("", second.stderr)
            self.assertEqual(0, second.returncode)
            self.assertEqual(1, task.read_text(encoding="utf-8").count("(pending)"))
            assert_concise_agent_report(self, report_path.read_text(encoding="utf-8"), agent="agent-4002", tmux="agent-4002", task_file="task.md")

    def test_omo_report_deduplicates_old_format_pending_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"done\n").hexdigest()
            task = root / "task.md"
            task.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        "(report manager 2026-06-10 12:02 agent=agent-4002 status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message-file: /tmp/msg.md",
                        "message:",
                        "> done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--root",
                    str(root),
                    "--manager-url",
                    "http://127.0.0.1:1",
                    "--task-file",
                    "task.md",
                    "--status",
                    "done",
                    "--agent",
                    "agent-4002",
                    "--message-file",
                    str(msg),
                ],
                cwd=tmp,
                env={**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = task.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("(pending)"))
            self.assertEqual(1, text.count("[message-sha256: "))

    def test_omo_report_ignores_routed_block_when_deduplicating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            msg = Path(tmp) / "msg.md"
            _ = msg.write_text("done\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"done\n").hexdigest()
            task = root / "task.md"
            task.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "(manager routed: to `done.md`.)",
                        "[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done]",
                        "(from agent agent-4002 via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--root",
                    str(root),
                    "--manager-url",
                    "http://127.0.0.1:1",
                    "--task-file",
                    "task.md",
                    "--status",
                    "done",
                    "--agent",
                    "agent-4002",
                    "--message-file",
                    str(msg),
                ],
                cwd=tmp,
                env={**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = task.read_text(encoding="utf-8")
            self.assertEqual(2, text.count("(pending)"))
            self.assertEqual(1, text.count("[message-sha256: "))
            self.assertRegex(text, re.compile(r"^\(from agent agent-4002 /tmp/omo-agent-messages-\d+/agent-4002_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))

    def test_omo_report_old_format_tmux_route_dedupes_only_same_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *%1701*) printf 'cfg\\t7\\t0\\t%%1701\\tleft\\n' ;;\n"
                "  *%1702*) printf 'cfg\\t8\\t0\\t%%1702\\tright\\n' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"same body\n").hexdigest()
            task = root / "task.md"
            task.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-tmux via=omo_report.sh status=done tmux_session=cfg tmux_window_index=7 tmux_pane_index=0 tmux_pane_id=%1701 tmux_target=cfg:7.0 tmux_window_name=left]",
                        "(from agent agent-tmux via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message:",
                        "> same body",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            base_env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env"),
            }
            for pane, expected_blocks in (("%1701", 1), ("%1702", 2)):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-tmux",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env={**base_env, "TMUX_PANE": pane},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
                self.assertEqual(expected_blocks, task.read_text(encoding="utf-8").count("(pending)"))

    def test_omo_report_old_format_tmux_route_dedupes_after_window_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'cfg\\t7\\t0\\t%%1701\\tright\\n'\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            old_hash = hashlib.sha256(b"same body\n").hexdigest()
            task = root / "task.md"
            task.write_text(
                "\n".join(
                    [
                        "(pending)",
                        "[omo-message-source: origin=agent agent=agent-tmux via=omo_report.sh status=done tmux_session=cfg tmux_window_index=7 tmux_pane_index=0 tmux_pane_id=%old tmux_target=cfg:7.0 tmux_window_name=left]",
                        "(from agent agent-tmux via omo_report.sh status=done)",
                        f"[message-sha256: {old_hash}]",
                        "message:",
                        "> same body",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--root",
                    str(root),
                    "--manager-url",
                    "http://127.0.0.1:1",
                    "--task-file",
                    "task.md",
                    "--status",
                    "done",
                    "--agent",
                    "agent-tmux",
                    "--message-file",
                    str(msg),
                ],
                cwd=tmp,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                    "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env"),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual(1, task.read_text(encoding="utf-8").count("(pending)"))

    def test_omo_report_requires_message_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--root",
                    str(root),
                    "--manager-url",
                    "http://127.0.0.1:1",
                    "--task-file",
                    "task.md",
                    "--status",
                    "done",
                    "--agent",
                    "agent-file-required",
                ],
                cwd=tmp,
                env={**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("--message-file", result.stderr)
            self.assertFalse((root / "task.md").exists())

    def test_omo_report_derives_tmux_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'cfg\\t7\\t0\\t%%1701\\tmanager window\\n'\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("tmux report\n", encoding="utf-8")
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_report.sh"),
                    "--root",
                    str(root),
                    "--manager-url",
                    "http://127.0.0.1:1",
                    "--task-file",
                    "task.md",
                    "--status",
                    "done",
                    "--agent",
                    "agent-tmux",
                    "--message-file",
                    str(msg),
                ],
                cwd=tmp,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                    "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env"),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = (root / "task.md").read_text(encoding="utf-8")
            self.assertRegex(text, re.compile(r"^\(from agent cfg:7 /tmp/omo-agent-messages-\d+/agent-tmux_done_[0-9a-f]{64}\.md\)$", re.MULTILINE))
            self.assertNotIn("tmux_session=cfg", text)
            self.assertNotIn("message-file: ", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_text = report_paths[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-tmux", tmux="cfg:7", task_file="task.md")
            self.assertNotIn("tmux_session=", report_text)
            self.assertNotIn("tmux_window_index=", report_text)
            self.assertNotIn("tmux_pane_index=", report_text)

    def test_omo_report_tmux_window_rename_updates_tmp_without_duplicate_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'cfg\\t7\\t0\\t%%1701\\t%s\\n' \"$OMO_FAKE_WINDOW_NAME\"\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            base_env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
                "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env"),
            }
            for window_name in ("left", "right"):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-tmux",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env={**base_env, "OMO_FAKE_WINDOW_NAME": window_name},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            text = (root / "task.md").read_text(encoding="utf-8")
            self.assertEqual(1, text.count("(pending)"))
            report_paths = agent_pointer_paths(text)
            self.assertEqual(1, len(report_paths))
            report_text = report_paths[0].read_text(encoding="utf-8")
            assert_concise_agent_report(self, report_text, agent="agent-tmux", tmux="cfg:7", task_file="task.md")

    def test_omo_report_same_hash_different_tmux_routes_append_distinct_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tmux = bin_dir / "tmux"
            tmux.write_text(
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *%1701*) printf 'cfg\\t7\\t0\\t%%1701\\tleft\\n' ;;\n"
                "  *%1702*) printf 'cfg\\t8\\t0\\t%%1702\\tright\\n' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            tmux.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            msg.write_text("same body\n", encoding="utf-8")
            for pane in ("%1701", "%1702"):
                result = subprocess.run(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-tmux",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env={
                        **os.environ,
                        "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "TMUX_PANE": pane,
                        "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env"),
                    },
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            text = (root / "task.md").read_text(encoding="utf-8")
            self.assertEqual(2, text.count("(pending)"))
            self.assertEqual(0, text.count("message-file: "))
            self.assertIn("(from agent cfg:7 ", text)
            self.assertIn("(from agent cfg:8 ", text)
            report_text = "\n".join(path.read_text(encoding="utf-8") for path in agent_pointer_paths(text))
            self.assertIn("tmux=cfg:7 ", report_text)
            self.assertIn("tmux=cfg:8 ", report_text)
            self.assertNotIn("tmux_target=", report_text)
            self.assertNotIn("message:\n", text)

    def test_omo_report_concurrent_distinct_reports_keep_all_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            messages = []
            for idx in range(12):
                msg = Path(tmp) / f"msg-{idx}.md"
                _ = msg.write_text(f"distinct report {idx}\n", encoding="utf-8")
                messages.append(msg)
            env = {**{key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
            procs = [
                subprocess.Popen(
                    [
                        str(Path.home() / ".config/omo_manager/omo_report.sh"),
                        "--root",
                        str(root),
                        "--manager-url",
                        "http://127.0.0.1:1",
                        "--task-file",
                        "task.md",
                        "--status",
                        "done",
                        "--agent",
                        "agent-4002",
                        "--message-file",
                        str(msg),
                    ],
                    cwd=tmp,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for msg in messages
            ]
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual("", stdout)
                self.assertEqual("", stderr)
                self.assertEqual(0, proc.returncode)
            task = root / "task.md"
            text = task.read_text(encoding="utf-8")
            self.assertEqual(12, text.count("(from agent agent-4002 "))
            self.assertEqual(0, text.count("message-file: "))
            self.assertNotIn("message:\n", text)
            for idx in range(12):
                self.assertNotIn(f"> distinct report {idx}", text)
            report_paths = agent_pointer_paths(text)
            self.assertEqual(12, len(report_paths))
            report_bodies = sorted(path.read_text(encoding="utf-8").rsplit("message:\n", 1)[1] for path in report_paths)
            self.assertEqual(sorted(f"distinct report {idx}\n" for idx in range(12)), report_bodies)
            self.assertEqual(12, len(find_markers(root, [task])))

    def test_new_email_source_is_delivered_by_pending_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            _ = path.write_text("(pending)\n(from email manager_mail/4002.txt)\n[source: email manager_mail/4002.txt]\n", encoding="utf-8")
            seen: dict[str, float] = {}
            args = Args(root=root, manager_url="", state=Path(tmp) / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True)
            from omo_manager.omo_pending_watch import scan_once

            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, seen, [path]))
            self.assertIn("origin=human source=email action=ack-human", out.getvalue())

    def test_pending_watch_can_add_manager_policy_reminder(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            path.write_text("(pending)\n(from email manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 0.0,
                reminder_choice=lambda reminders: reminders[1],
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("origin=human source=email action=ack-human", text)
            self.assertIn("Reminder: stay high level; route concrete work to agents.", text)

    def test_pending_watch_skips_manager_policy_reminder_when_not_selected(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 1.0,
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("pending: file=task.md", text)
            self.assertNotIn("Reminder:", text)

    def test_email_pending_ref_can_add_email_policy_reminder(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = dated_manager_file(root)
            path.write_text("(pending)\n(from email manager_mail/4002.txt)\n", encoding="utf-8")
            args = Args(
                root=root,
                manager_url="",
                state=root / "seen.tsv",
                interval_s=1.0,
                full_scan_interval_s=1.0,
                idle_status_interval_s=1800.0,
                status_script=Path("/bin/false"),
                once=True,
                dry_run=True,
                reminder_random=lambda: 0.0,
                reminder_choice=lambda reminders: reminders[-1],
            )
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            self.assertIn("Reminder: acknowledge human email first, then delegate.", out.getvalue())

    def test_oversized_pending_task_file_output_includes_continuation_warning(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            lines = ["(pending)", "please route", *["history" for _ in range(1999)]]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            args = Args(root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True)
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("pending: file=task.md", text)
            self.assertIn("task-file length warning: this file has 2001 lines", text)
            self.assertIn("move future content into a linked continuation file and cross-link both files", text)

    def test_normal_size_pending_task_file_output_has_no_continuation_warning(self) -> None:
        from omo_manager.omo_pending_watch import scan_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(root=root, manager_url="", state=root / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, idle_status_interval_s=1800.0, status_script=Path("/bin/false"), once=True, dry_run=True)
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(scan_once(args, {}, [path]))
            text = out.getvalue()
            self.assertIn("pending: file=task.md", text)
            self.assertNotIn("task-file length warning", text)

    def test_seen_pending_markers_do_not_expire_into_repush_loop(self) -> None:
        from omo_manager.omo_pending_watch import expire_seen

        seen = {"root:task.md:2:digest": 1.0}
        self.assertEqual(seen, expire_seen(seen, 3601.0))

    def test_manager_routed_pending_marker_is_not_redelivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "work_manager.md"
            _ = path.write_text("(pending)\n(manager routed: to `task.md`.)\n(from email manager_mail/4480.txt)\n", encoding="utf-8")
            self.assertEqual([], find_markers(root, [path]))

    def test_idle_status_pushes_full_periodic_agent_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text("#!/usr/bin/env python3\nprint('agent-status: not_codex=0 running=2 error=0 ready=0 done-registry-stale=0 pruned=0')\nprint('running: task=a.md evidence=target=cfg:1')\nprint('running: task=b.md evidence=target=cfg:2')\n", encoding="utf-8")
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            out = StringIO()
            with redirect_stdout(out):
                pushed = watcher.maybe_push_idle_status(args, 100.0, 130.0)
            self.assertTrue(pushed)
            text = out.getvalue()
            self.assertIn("manager agent status: periodic running-agent status.", text)
            self.assertIn("agent-status: not_codex=0 running=2", text)
            self.assertIn("running: task=a.md", text)
            self.assertIn("running: task=b.md", text)

    def test_idle_status_tick_updates_after_due_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
        with patch("omo_manager.omo_pending_watch.maybe_push_idle_status", return_value=False) as push:
            self.assertEqual((100.0, None), watcher.update_idle_status_check(args, 100.0, 129.9, None))
            self.assertEqual((130.0, None), watcher.update_idle_status_check(args, 100.0, 130.0, None))
        push.assert_called_once_with(args, 100.0, 130.0)

    def test_idle_status_tick_starts_background_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "http://127.0.0.1:1", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, False)
        with patch("omo_manager.omo_pending_watch.start_command", return_value="run") as start:
            self.assertEqual((130.0, "run"), watcher.update_idle_status_check(args, 100.0, 130.0, None))
        start.assert_called_once_with("idle status check", ["/status.py", "--root", "/tmp"], 30)

    def test_status_command_passes_manager_target_to_problem_check(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, "cfg:0.0")
        self.assertEqual(["/status.py", "--root", "/tmp", "--manager-target", "cfg:0.0", "--problems-only"], watcher.status_command(args, True))

    def test_agent_problem_check_uses_compaction_aware_timeout(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True)
        result = subprocess.CompletedProcess(["/status.py"], 0, "", "")
        with patch("omo_manager.omo_pending_watch.subprocess.run", return_value=result) as run:
            self.assertFalse(watcher.maybe_push_agent_problems(args, {}, 1000.0))
        run.assert_called_once()
        self.assertEqual(watcher.DEFAULT_AGENT_PROBLEM_TIMEOUT_S, run.call_args.kwargs["timeout"])

    def test_periodic_status_text_formats_full_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        result = watcher.CommandOutput("idle status check", 0, "agent-status: running=1\nrunning: task=a.md\n", "")
        self.assertEqual(
            "manager agent status: periodic running-agent status.\nagent-status: running=1\nrunning: task=a.md",
            watcher.periodic_status_text(Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True), result),
        )

    def test_idle_status_delivery_timeout_matches_push_budget(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        self.assertEqual(325.0, watcher.manager_push_timeout_s())

    def test_idle_status_stays_quiet_before_idle_interval(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True)
        out = StringIO()
        with redirect_stdout(out):
            pushed = watcher.maybe_push_idle_status(args, 100.0, 129.9)
        self.assertFalse(pushed)
        self.assertEqual("", out.getvalue())

    def test_agent_problem_check_pushes_only_changed_or_unthrottled_problem(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text("#!/usr/bin/env python3\nprint('agent-problems: not_codex=1 error=0 ready=0 done-registry-stale=0')\nprint('not_codex: task=task.md evidence=target=cfg:1')\nraise SystemExit(3)\n", encoding="utf-8")
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True, agent_problem_repeat_s=300.0)
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.maybe_push_agent_problems(args, seen, 1000.0))
                self.assertFalse(watcher.maybe_push_agent_problems(args, seen, 1200.0))
                self.assertTrue(watcher.maybe_push_agent_problems(args, seen, 1300.0))
            text = out.getvalue()
            self.assertEqual(2, text.count("manager agent problem: running task marker needs attention."))
            self.assertEqual(2, text.count("[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=agent-problem]"))
            self.assertIn("not_codex: task=task.md", text)

    def test_agent_problem_check_does_not_throttle_unstuck_reports(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=task.md evidence=target=cfg:1 unstick=sent_enter\nunstuck: target=cfg:1 task=task.md action=sent_enter\n",
            "",
        )
        seen: dict[str, float] = {}
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1000.0))
            self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 1001.0))
        self.assertEqual(2, out.getvalue().count("unstuck: target=cfg:1"))
        self.assertEqual(2, out.getvalue().count("[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=agent-problem]"))

    def test_agent_problem_check_pushes_blocked_idle_reports(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: blocked_idle=1\nmanager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker\nblocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked idle_status=ready reason=image lacks codex\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("agent-problems: blocked_idle=1", text)
        self.assertIn("manager-action: blocked_idle>0 inspect blocked agents", text)
        self.assertIn("blocked_idle: task=vl_worker.md", text)

    def test_agent_problem_check_can_add_manager_policy_reminder(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(
            Path("/tmp"),
            "",
            Path("/tmp/seen.tsv"),
            1.0,
            1.0,
            30.0,
            Path("/status.py"),
            False,
            True,
            agent_problem_repeat_s=300.0,
            reminder_random=lambda: 0.0,
            reminder_choice=lambda reminders: reminders[0],
        )
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: blocked_idle=1\nblocked_idle: task=vl_worker.md evidence=target=vl:9 role=blocked_idle_vl task_status=blocked idle_status=ready reason=image lacks codex\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("manager agent problem: running task marker needs attention.", text)
        self.assertIn("Reminder: delegate work; do not do worker work in the manager.", text)

    def test_agent_problem_check_suppresses_manager_self_stuck_prompt(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=manager evidence=target=wl:1.0 role=manager unstick=sent_enter\nunstuck: target=wl:1.0 task=manager action=sent_enter\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertFalse(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)
        self.assertNotIn("unstuck:", text)

    def test_agent_problem_check_emails_human_for_manager_error(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("manager human email due: manager watcher detected manager error", text)
        self.assertIn("manager-problems: error=1", text)
        self.assertIn("error: task=manager evidence=target=wl:1.0 role=manager", text)
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)

    def test_agent_problem_check_invokes_human_email_helper_for_manager_error(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "fake-email.sh"
            log = root / "email.log"
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "subject_file=\n"
                "message_file=\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --manager-human) shift ;;\n"
                "    --subject-file) subject_file=\"$2\"; shift 2 ;;\n"
                "    --message-file) message_file=\"$2\"; shift 2 ;;\n"
                "    *) echo \"bad arg: $1\" >&2; exit 2 ;;\n"
                "  esac\n"
                "done\n"
                f"{{ printf 'subject:\\n'; cat \"$subject_file\"; printf 'body:\\n'; cat \"$message_file\"; }} > {str(log)!r}\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, root / "status.py", False, False, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
            result = watcher.CommandOutput(
                "agent-problems",
                3,
                "agent-problems: error=1\nerror: task=manager evidence=target=wl:1.0 role=manager output=Selected model is at capacity\n",
                "",
            )
            with patch.object(watcher, "DEFAULT_HUMAN_EMAIL_HELPER", helper):
                self.assertTrue(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
            text = log.read_text(encoding="utf-8")
            self.assertIn("subject:\nmanager watcher detected manager error\n", text)
            self.assertIn("body:\nThe manager watcher detected a manager pane problem", text)
            self.assertIn("manager-problems: error=1", text)
            self.assertIn("error: task=manager evidence=target=wl:1.0 role=manager", text)

    def test_agent_problem_check_suppresses_manager_target_alias_prompt(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        args = Args(Path("/tmp"), "", Path("/tmp/seen.tsv"), 1.0, 1.0, 30.0, Path("/status.py"), False, True, manager_target="wl:1.0", agent_problem_repeat_s=300.0)
        result = watcher.CommandOutput(
            "agent-problems",
            3,
            "agent-problems: stuck_input=1\nstuck_input: task=active.md evidence=target=wl:1 unstick=sent_enter\nunstuck: target=wl:1 task=active.md action=sent_enter\n",
            "",
        )
        out = StringIO()
        with redirect_stdout(out):
            self.assertFalse(watcher.handle_agent_problem_result(args, {}, result, 1000.0))
        text = out.getvalue()
        self.assertIn("suppressed manager self-problem report", text)
        self.assertNotIn("manager agent problem: running task marker needs attention.", text)
        self.assertNotIn("unstuck:", text)

    def test_markdown_inotify_watcher_reports_new_file(self) -> None:
        from omo_manager.omo_pending_watch import MarkdownChangeWatcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watcher = MarkdownChangeWatcher.open(root)
            if watcher is None:
                self.skipTest("inotify unavailable")
            try:
                path = root / "task.md"
                path.write_text("(pending)\nnew event\n", encoding="utf-8")
                files, full_scan, notified = watcher.wait(2.0)
            finally:
                watcher.close()
            self.assertTrue(notified)
            self.assertFalse(full_scan)
            self.assertIn(path, files)

    def test_pending_watch_poll_backstop_interval_is_configurable(self) -> None:
        from omo_manager.omo_pending_watch import parse_args

        args = parse_args(["--root", "/tmp/root", "--poll-backstop-interval-s", "12.5", "--once"])
        self.assertEqual(12.5, args.poll_backstop_interval_s)

    def test_pending_watch_poll_backstop_detects_silent_missed_event(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text("old\n", encoding="utf-8")
            state = watcher.FileState(mtimes_ns={})
            self.assertEqual([path], watcher.mtime_changed_markdown_files(root, state))
            self.assertEqual([], watcher.mtime_changed_markdown_files(root, state))
            path.write_text("(pending)\nmissed by inotify\n", encoding="utf-8")
            os.utime(path, ns=(2_000_000_000, 2_000_000_000))
            changed = watcher.mtime_changed_markdown_files(root, state)
            self.assertEqual([path], changed)
            args = Args(root, "", root / "seen.tsv", 1.0, 300.0, 30.0, Path("/missing-status.py"), False, True)
            seen: dict[str, float] = {}
            out = StringIO()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, changed))
            self.assertIn("pending: file=task.md", out.getvalue())

    def test_background_agent_problem_check_does_not_block_pending_scan(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_script = root / "slow-status.sh"
            status_script.write_text(
                "#!/usr/bin/env bash\n"
                "sleep 0.5\n"
                "printf 'agent-problems: not_codex=1 error=0 ready=0 done-registry-stale=0\\n'\n"
                "exit 3\n",
                encoding="utf-8",
            )
            status_script.chmod(0o700)
            run = watcher.start_command("agent problem check", [str(status_script)], 30)
            self.assertIsNotNone(run)
            assert run is not None
            self.assertIsNone(watcher.poll_command(run, time.time()))
            path = root / "task.md"
            path.write_text("(pending)\nplease route\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            seen: dict[str, float] = {}
            out = StringIO()
            started_s = time.monotonic()
            with redirect_stdout(out):
                self.assertTrue(watcher.scan_once(args, seen, [path]))
            self.assertLess(time.monotonic() - started_s, 0.2)
            self.assertIn("pending: file=task.md", out.getvalue())
            while True:
                result = watcher.poll_command(run, time.time())
                if result is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(3, result.returncode)

    def test_agent_problem_check_stays_quiet_on_healthy_status(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.maybe_push_agent_problems(args, {}, 1000.0))
            self.assertEqual("", out.getvalue())

    def test_idle_digest_delivery_waits_for_one_hour_without_human_email(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            mail = mail_dir / "5125.txt"
            _ = mail.write_text("Subject: test\n", encoding="utf-8")
            os.utime(mail, (1000.0, 1000.0))
            _ = (root / "manager_digest.md").write_text("digest item\n", encoding="utf-8")
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, True, mail_dir=mail_dir, digest_script=root / "scripts" / "manager-digest", digest_idle_after_s=3600.0)
            out = StringIO()
            with redirect_stdout(out):
                self.assertFalse(watcher.maybe_deliver_idle_digest(args, 0.0, 4599.9))
                self.assertTrue(watcher.maybe_deliver_idle_digest(args, 0.0, 4600.0))
            self.assertIn("no human email for 3600s", out.getvalue())

    def test_idle_digest_delivery_runs_manager_digest_script(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            mail = mail_dir / "5125.txt"
            _ = mail.write_text("Subject: old\n", encoding="utf-8")
            os.utime(mail, (1000.0, 1000.0))
            _ = (root / "manager_digest.md").write_text("digest item\n", encoding="utf-8")
            script = root / "manager-digest"
            log = root / "deliver.log"
            _ = script.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$PWD $*\" > {log}\n", encoding="utf-8")
            script.chmod(0o700)
            args = Args(root, "", root / "seen.tsv", 1.0, 1.0, 30.0, Path("/missing-status.py"), False, False, mail_dir=mail_dir, digest_script=script, digest_idle_after_s=3600.0)
            self.assertTrue(watcher.maybe_deliver_idle_digest(args, 0.0, 4600.0))
            self.assertEqual(f"{root} deliver\n", log.read_text(encoding="utf-8"))

if __name__ == "__main__":
    _ = unittest.main()
