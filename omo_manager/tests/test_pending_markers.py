from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.email_idle_watcher import append_pending, dated_manager_file, existing_source_pending_line
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

    def test_email_watcher_manager_file_is_configurable(self) -> None:
        from omo_manager.email_idle_watcher import parse_args

        args = parse_args(["--root", "/tmp/root", "--manager-file", "work_manager_20260527.md", "--once"])
        self.assertEqual(Path("/tmp/root/work_manager_20260527.md"), args.manager_file)

    def test_email_watcher_ignores_legacy_active_log_env_by_default(self) -> None:
        from unittest.mock import patch
        from omo_manager.email_idle_watcher import parse_args

        with patch.dict(os.environ, {"OMO_MANAGER_ACTIVE_LOG": "/tmp/root/work_manager.md"}):
            args = parse_args(["--root", "/tmp/root", "--once"])
        self.assertEqual(dated_manager_file(Path("/tmp/root")), args.manager_file)

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

    def test_email_watcher_retries_existing_pending_until_submit_succeeds(self) -> None:
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
                        return "OK", [b"13"]
                    if command == "fetch":
                        raise AssertionError("existing pending should not refetch")
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError(command)

            def push(_args: watcher.Args, line_no: int) -> bool:
                calls.append(line_no)
                return len(calls) == 2

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
            self.assertEqual(len(client.stores), 1)
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
            self.assertIn("(pending)\n(from agent agent-4002 via omo_report.sh status=done)\n", text)
            self.assertEqual(1, text.count("(from agent agent-4002 via omo_report.sh status=done)"))
            self.assertIn("[message-sha256: ", text)
            self.assertIn("message:\n> done\n", text)
            self.assertNotIn("PWD:", text)
            self.assertNotIn("OPENCODE:", text)
            self.assertNotIn("TMUX:", text)
            markers = find_markers(root, [task])
            self.assertEqual(1, len(markers))

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
            args = Args(root=root, manager_url="", state=Path(tmp) / "seen.tsv", interval_s=1.0, full_scan_interval_s=1.0, once=True, dry_run=True)
            from omo_manager.omo_pending_watch import scan_once

            with redirect_stdout(StringIO()):
                self.assertTrue(scan_once(args, seen, [path]))

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
                "Path(os.environ['SENT_LOG']).open('a', encoding='utf-8').write(sys.argv[1] + '\\n')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            msg = Path(tmp) / "msg.md"
            _ = msg.write_text("same body\n", encoding="utf-8")
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
                "--message-file",
                str(msg),
            ]
            first = subprocess.run(cmd, text=True, capture_output=True, timeout=10, env=env, check=False)
            second = subprocess.run(cmd, text=True, capture_output=True, timeout=10, env=env, check=False)
            self.assertEqual(0, first.returncode)
            self.assertEqual("Emailed the human\n", first.stdout)
            self.assertEqual(0, second.returncode)
            self.assertEqual("Skipped duplicate human email\n", second.stdout)
            self.assertEqual("[omo_manager] Manager update\n", sent_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
