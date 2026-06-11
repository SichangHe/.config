from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.email_idle_watcher import append_pending, current_manager_file, dated_manager_file, existing_source_pending_line
from omo_manager.omo_pending_watch import Args, find_markers


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

        class Client:
            sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def readline(self) -> bytes:
                return b""

        with self.assertRaises(ConnectionError):
            watcher.idle_once(Client(), 0)

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
            self.assertIn("Subject: Re: [omo_manager] Update\n", text)
            self.assertNotIn("From:", text)
            self.assertNotIn("Date:", text)
            self.assertNotIn("UID:", text)

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

    def test_email_watcher_marks_existing_pending_seen_even_if_submit_fails(self) -> None:
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
            self.assertEqual(calls, [1])
            self.assertEqual(len(client.stores), 1)
            self.assertIn("13	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

    def test_email_watcher_marks_new_pending_seen_after_markdown_even_if_submit_fails(self) -> None:
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
            self.assertEqual(client.stores, [("14", "+FLAGS", r"(\Seen)")])
            self.assertIn("14	", (state / "email-processed-uids.tsv").read_text(encoding="utf-8"))

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

    def test_email_watcher_searches_seen_uid_range_after_processed_state(self) -> None:
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
                    env={**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual("", result.stderr)
                self.assertEqual(0, result.returncode)
            task = root / "task.md"
            text = task.read_text(encoding="utf-8")
            self.assertIn("(pending)\n[omo-message-source: origin=agent agent=agent-4002 via=omo_report.sh status=done", text)
            self.assertIn("(from agent agent-4002 via omo_report.sh status=done)\n", text)
            self.assertEqual(1, text.count("(from agent agent-4002 via omo_report.sh status=done)"))
            self.assertIn("[message-sha256: ", text)
            self.assertIn("message:\n> done\n", text)
            self.assertNotIn("PWD:", text)
            self.assertNotIn("OPENCODE:", text)
            self.assertNotIn("TMUX:", text)
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))
            self.assertEqual("agent", markers[0].origin)

    def test_omo_report_reads_stdin_body_by_default(self) -> None:
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
                    "agent-stdin",
                ],
                input="stdin report\n",
                cwd=tmp,
                env={**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            text = (root / "task.md").read_text(encoding="utf-8")
            self.assertIn("[omo-message-source: origin=agent agent=agent-stdin via=omo_report.sh status=done", text)
            self.assertIn("(from agent agent-stdin via omo_report.sh status=done)", text)
            self.assertIn("message:\n> stdin report\n", text)

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
                ],
                input="tmux report\n",
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
            self.assertIn("tmux_session=cfg", text)
            self.assertIn("tmux_window_index=7", text)
            self.assertIn("tmux_pane_index=0", text)
            self.assertIn("tmux_pane_id=%1701", text)
            self.assertIn("tmux_target=cfg:7.0", text)
            self.assertIn("tmux_window_name=manager%20window", text)

    def test_omo_report_concurrent_distinct_reports_keep_all_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            messages = []
            for idx in range(12):
                msg = Path(tmp) / f"msg-{idx}.md"
                _ = msg.write_text(f"distinct report {idx}\n", encoding="utf-8")
                messages.append(msg)
            env = {**os.environ, "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing-local.env")}
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
            self.assertEqual(12, text.count("(from agent agent-4002 via omo_report.sh status=done)"))
            for idx in range(12):
                self.assertIn(f"message:\n> distinct report {idx}\n", text)
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

    def test_idle_status_pushes_only_when_status_script_reports_problem(self) -> None:
        from omo_manager import omo_pending_watch as watcher

        with tempfile.TemporaryDirectory() as tmp:
            status_script = Path(tmp) / "status.py"
            _ = status_script.write_text("#!/usr/bin/env python3\nprint('agent-problems: not_codex=1 error=0 ready=0 done-registry-stale=0')\nprint('not_codex: task=task.md evidence=target=cfg:1')\nraise SystemExit(3)\n", encoding="utf-8")
            status_script.chmod(0o700)
            args = Args(Path(tmp), "", Path(tmp) / "seen.tsv", 1.0, 1.0, 30.0, status_script, False, True)
            out = StringIO()
            with redirect_stdout(out):
                pushed = watcher.maybe_push_idle_status(args, 100.0, 130.0)
            self.assertTrue(pushed)
            text = out.getvalue()
            self.assertIn("manager agent problem: idle watcher found", text)
            self.assertIn("agent-problems: not_codex=1", text)
            self.assertIn("not_codex: task=task.md", text)

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
            self.assertIn("not_codex: task=task.md", text)

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

    def test_omo_email_human_skips_duplicate_subject_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            helper_dir = home / ".config" / "helper.sh"
            helper_dir.mkdir(parents=True)
            helper = helper_dir / "email_me.py"
            sent_log = Path(tmp) / "sent.log"
            _ = helper.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "import sys\n"
                "Path(os.environ['SENT_LOG']).open('a', encoding='utf-8').write(sys.argv[1] + '\\n' + sys.stdin.read())\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            env = {
                **os.environ,
                "HOME": str(home),
                "SENT_LOG": str(sent_log),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
            }
            cmd = [
                str(Path.home() / ".config/omo_manager/omo_email_human.sh"),
                "--subject",
                "Manager update",
            ]
            first = subprocess.run(cmd, input="same body\n", text=True, capture_output=True, timeout=10, env=env, check=False)
            second = subprocess.run(cmd, input="same body\n", text=True, capture_output=True, timeout=10, env=env, check=False)
            self.assertEqual(0, first.returncode)
            self.assertEqual("Emailed the human\n", first.stdout)
            self.assertEqual(0, second.returncode)
            self.assertEqual("Skipped duplicate human email\n", second.stdout)
            self.assertEqual("[omo_manager] Manager update\nsame body\n", sent_log.read_text(encoding="utf-8"))

    def test_omo_email_human_safe_stdin_and_file_bodies_are_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            helper_dir = home / ".config" / "helper.sh"
            helper_dir.mkdir(parents=True)
            helper = helper_dir / "email_me.py"
            sent_log = Path(tmp) / "sent.log"
            _ = helper.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "import sys\n"
                "Path(os.environ['SENT_LOG']).open('a', encoding='utf-8').write("
                "sys.argv[1] + '\\n' + sys.stdin.read() + '\\n--END--\\n')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            cmd_sentinel = Path(tmp) / "cmd-substitution-ran"
            backtick_sentinel = Path(tmp) / "backtick-ran"
            stdin_body = (
                "literal $HOME\n"
                f"literal $(touch {cmd_sentinel})\n"
                f"literal `touch {backtick_sentinel}`\n"
                "> quoted markdown line\n"
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "SENT_LOG": str(sent_log),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
            }
            script = Path.home() / ".config/omo_manager/omo_email_human.sh"
            heredoc_cmd = f"""{shlex.quote(str(script))} --subject 'Literal stdin safety' <<'EMAIL_BODY'
{stdin_body}EMAIL_BODY
"""
            stdin_result = subprocess.run(
                ["bash", "-c", heredoc_cmd],
                text=True,
                capture_output=True,
                timeout=10,
                env=env,
                check=False,
            )
            self.assertEqual(0, stdin_result.returncode, stdin_result.stderr)
            message_file = Path(tmp) / "body.md"
            file_body = stdin_body.replace("literal $HOME", "literal file $HOME")
            message_file.write_text(file_body, encoding="utf-8")
            file_result = subprocess.run(
                [
                    str(script),
                    "--subject",
                    "Literal file safety",
                    "--message-file",
                    str(message_file),
                ],
                text=True,
                capture_output=True,
                timeout=10,
                env=env,
                check=False,
            )
            self.assertEqual(0, file_result.returncode, file_result.stderr)
            self.assertFalse(cmd_sentinel.exists())
            self.assertFalse(backtick_sentinel.exists())
            text = sent_log.read_text(encoding="utf-8")
            self.assertIn("[omo_manager] Literal stdin safety\n" + stdin_body + "\n--END--\n", text)
            self.assertIn("[omo_manager] Literal file safety\n" + file_body + "\n--END--\n", text)

    def test_omo_email_human_help_shows_safe_body_pattern(self) -> None:
        script = Path.home() / ".config/omo_manager/omo_email_human.sh"
        result = subprocess.run([str(script), "--help"], text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(0, result.returncode)
        self.assertIn("cat > /tmp/body.md <<'EOF_BODY'", result.stdout)
        self.assertIn('omo_email_human.sh --subject \'Subject\' < /tmp/body.md', result.stdout)
        self.assertIn("extra double-quoted sh -c or zsh -c string", result.stdout)

    def test_omo_email_human_rejects_placeholder_subject_and_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            helper_dir = home / ".config" / "helper.sh"
            helper_dir.mkdir(parents=True)
            helper = helper_dir / "email_me.py"
            sent_log = Path(tmp) / "sent.log"
            _ = helper.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ['SENT_LOG']).write_text('sent', encoding='utf-8')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            env = {
                **os.environ,
                "HOME": str(home),
                "SENT_LOG": str(sent_log),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
            }
            for subject in ("SUBJECT", "SUBJECT\\", "[omo_manager] SUBJECT\\"):
                cmd = [str(Path.home() / ".config/omo_manager/omo_email_human.sh"), "--subject", subject]
                bad_subject = subprocess.run(cmd, input="real body\n", text=True, capture_output=True, timeout=10, env=env, check=False)
                self.assertEqual(2, bad_subject.returncode)
                self.assertIn("placeholder SUBJECT", bad_subject.stderr)
            cmd = [str(Path.home() / ".config/omo_manager/omo_email_human.sh"), "--subject", "Real subject"]
            empty_body = subprocess.run(cmd, input="\n", text=True, capture_output=True, timeout=10, env=env, check=False)
            self.assertEqual(2, empty_body.returncode)
            self.assertIn("email body must not be empty", empty_body.stderr)
            self.assertFalse(sent_log.exists())


if __name__ == "__main__":
    _ = unittest.main()
