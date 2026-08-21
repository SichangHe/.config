from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from omo_manager import email_idle_watcher as watcher


def split_args(root: Path, state: Path, manager_file: Path, *, manager_target: str = "", inbox_identity: str) -> watcher.Args:
    return watcher.Args(
        root,
        "",
        root / "manager_mail",
        state,
        manager_file,
        True,
        "human@example.test",
        0,
        Path("/bin/false"),
        manager_target=manager_target,
        mail_thresholds=False,
        inbox_identity=inbox_identity,
    )


class AmhEmailWatcherRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        email_tmp = tempfile.TemporaryDirectory(prefix="omo-amh-email-route.")
        self.addCleanup(email_tmp.cleanup)
        email_root = Path(email_tmp.name)
        env_patch = patch.dict(
            os.environ,
            {
                "EMAIL_ME_FAKE_SEND_LOG": str(email_root / "sent.log"),
                "OMO_AGENT_GMAIL_ADDRESS": "",
                "OMO_AGENT_GMAIL_APP_PASSWORD": "",
                "OMO_HUMAN_EMAIL_ADDRESS": "",
                "OMO_HUMAN_EMAIL_CONFIG_PATH": str(email_root / "missing-email-config.toml"),
                "OMO_MANAGER_LOCAL_ENV": str(email_root / "missing-local.env"),
                "OMO_MANAGER_STATE_DIR": str(email_root / "state"),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_live_mailbox_approval_only_requires_once(self) -> None:
        with self.assertRaises(SystemExit):
            watcher.parse_args(["--live-mailbox-approval-only"])

    def test_split_email_watcher_routes_exact_amh_subject_tag_before_legacy_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <human@example.test>\r\n"
                b"Return-Path: <human@example.test>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=human@example.test\r\n"
                b"Subject: [pb] AMH route\r\n"
                b"Content-Type: text/plain; charset=iso-8859-1\r\n"
                b"\r\n"
                b"caf\xe9"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []
                    self.selects: list[tuple[str, bool]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.selects.append((mailbox, readonly))
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"61"]
                    if command == "fetch" and args == ("61", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("61", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"61 (FLAGS () X-GM-MSGID 12345 X-GM-THRID 67890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            class FakeIngress:
                identities: list[object] = []

                class ResponsibleAgentRoute:
                    agent_id = "pb"

                class ProviderMessageIdentity:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs
                        FakeIngress.identities.append(kwargs)

                @staticmethod
                def derive_replay_ids(_identity: object) -> object:
                    class Ids:
                        operation_id = "op-amh-route"

                    return Ids()

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            test = self

            class FakeBridge:
                observed_subjects: list[str] = []
                observed_messages: list[object] = []

                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitFailure:
                    pass

                class BridgeConfig:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                class WatcherMessage:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs
                        FakeBridge.observed_messages.append(kwargs)

                @staticmethod
                def route_from_watcher_subject(subject: str, *, enabled_agent_ids: frozenset[str]) -> FakeDecision:
                    FakeBridge.observed_subjects.append(subject)
                    test.assertEqual(frozenset({"pb"}), enabled_agent_ids)
                    return FakeDecision()

                @staticmethod
                def bridge_watcher_message(_message: object, config: object, persist_cursor: object, mark_seen: object, ownership: object) -> object:
                    del config, ownership
                    persist_cursor()
                    test.assertTrue(mark_seen())
                    return FakeBridge.MailboxAdvanced()

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            launched: list[str] = []
            FakeIngress.identities = []
            FakeBridge.observed_subjects = []
            FakeBridge.observed_messages = []
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=lambda _args, operation_id: launched.append(operation_id)),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertEqual(["op-amh-route"], launched)
            self.assertEqual(["[pb] AMH route"], FakeBridge.observed_subjects)
            self.assertEqual(
                {"provider": "gmail", "account_id": "agent@example.test", "message_id": "12345", "thread_id": "67890"},
                FakeIngress.identities[0],
            )
            self.assertEqual(raw_mime, FakeBridge.observed_messages[0]["raw_mime"])
            self.assertEqual(b"caf\xe9", FakeBridge.observed_messages[0]["decoded_prompt"])
            self.assertEqual(b"[pb] AMH route", FakeBridge.observed_messages[0]["decoded_subject"])
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())
            self.assertEqual([("61", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertIn("61\t", watcher.processed_uids_path(args).read_text(encoding="utf-8"))

    def test_split_email_watcher_enables_main_subject_tag_as_main_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[main] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []
                    self.selects: list[tuple[str, bool]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.selects.append((mailbox, readonly))
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"64"]
                    if command == "fetch" and args == ("64", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("64", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"64 (FLAGS () X-GM-MSGID 42345 X-GM-THRID 97890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "main-manager"

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        pass

                @staticmethod
                def derive_replay_ids(_identity: object) -> object:
                    class Ids:
                        operation_id = "op-main-route"

                    return Ids()

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitFailure:
                    pass

                class BridgeConfig:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                class WatcherMessage:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                @staticmethod
                def route_from_watcher_subject(subject: str, *, enabled_agent_ids: frozenset[str]) -> FakeDecision:
                    self.assertEqual("[main] AMH route", subject)
                    self.assertEqual(frozenset({"main-manager"}), enabled_agent_ids)
                    return FakeDecision()

                @staticmethod
                def bridge_watcher_message(_message: object, config: object, persist_cursor: object, mark_seen: object, ownership: object) -> object:
                    del config, ownership
                    persist_cursor()
                    self.assertTrue(mark_seen())
                    return FakeBridge.MailboxAdvanced()

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            launched: list[str] = []
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=lambda _args, operation_id: launched.append(operation_id)),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertEqual(["op-main-route"], launched)
            self.assertFalse(manager_file.exists())
            self.assertEqual([("64", "+FLAGS", "(\\Seen)")], client.stores)

    def test_amh_agent_status_uses_configured_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "amh"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(watcher, "DEFAULT_AMH_EXECUTABLE", executable), patch.object(
                watcher, "DEFAULT_AMH_RUNTIME_ROOT", runtime
            ), patch.object(subprocess, "run", side_effect=fake_run):
                self.assertTrue(watcher.amh_agent_status_is_supported("pb"))
            self.assertEqual(
                [str(executable), "--runtime-root", str(runtime), "agent", "status", "pb"],
                calls[0],
            )

    def test_split_email_watcher_status_check_uses_runtime_root_on_public_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            executable = Path(tmp) / "amh"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[pb] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []
                    self.selects: list[tuple[str, bool]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.selects.append((mailbox, readonly))
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"68"]
                    if command == "fetch" and args == ("68", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("68", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"68 (FLAGS () X-GM-MSGID 82345 X-GM-THRID 223344 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "pb"

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        pass

                @staticmethod
                def derive_replay_ids(_identity: object) -> object:
                    class Ids:
                        operation_id = "op-status-runtime-root"

                    return Ids()

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitFailure:
                    pass

                class BridgeConfig:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                class WatcherMessage:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                @staticmethod
                def route_from_watcher_subject(_subject: str, *, enabled_agent_ids: frozenset[str]) -> FakeDecision:
                    self.assertEqual(frozenset({"pb"}), enabled_agent_ids)
                    return FakeDecision()

                @staticmethod
                def bridge_watcher_message(_message: object, config: object, persist_cursor: object, mark_seen: object, ownership: object) -> object:
                    del config, ownership
                    persist_cursor()
                    self.assertTrue(mark_seen())
                    return FakeBridge.MailboxAdvanced()

            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test")
            launched: list[str] = []
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=lambda _args, operation_id: launched.append(operation_id)),
                patch.object(watcher, "DEFAULT_AMH_EXECUTABLE", executable),
                patch.object(watcher, "DEFAULT_AMH_RUNTIME_ROOT", runtime),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
                patch.object(subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertEqual(["op-status-runtime-root"], launched)
            self.assertEqual(
                [str(executable), "--runtime-root", str(runtime), "agent", "status", "pb"],
                calls[0],
            )

    def test_split_email_watcher_falls_back_before_amh_without_gmail_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <human@example.test>\r\n"
                b"Return-Path: <human@example.test>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=human@example.test\r\n"
                b"Subject: [pb] AMH route\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Please handle this."
            )

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"65"]
                    if command == "fetch" and args == ("65", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("65", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"65 (FLAGS () X-GM-MSGID 52345 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            with patch.object(watcher, "load_amh_bridge_modules", side_effect=AssertionError("precommit fallback should not load AMH")):
                self.assertTrue(watcher.handle_unseen(client, args))
            mail_name = watcher.mail_artifact_name(args, "65")
            self.assertIn(f"(record and delegate manager_mail/{mail_name})", manager_file.read_text(encoding="utf-8"))
            self.assertEqual([("65", "+FLAGS", "(\\Seen)")], client.stores)

    def test_split_email_watcher_does_not_record_processed_when_mark_seen_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <human@example.test>\r\n"
                b"Return-Path: <human@example.test>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=human@example.test\r\n"
                b"Subject: ordinary request\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Please handle this."
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"66"]
                    if command == "fetch" and args == ("66", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("66", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"66 (FLAGS () X-GM-MSGID 62345 X-GM-THRID 107890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "NO", [b"store failed"]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            with patch.object(watcher, "push_email_ref", return_value=True):
                self.assertFalse(watcher.handle_unseen(client, args))
            self.assertEqual([("66", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertFalse(watcher.processed_uids_path(args).exists())
            self.assertIn("record and delegate manager_mail", manager_file.read_text(encoding="utf-8"))

    def test_split_email_watcher_replay_does_not_record_processed_when_mark_seen_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <human@example.test>\r\n"
                b"Return-Path: <human@example.test>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=human@example.test\r\n"
                b"Subject: ordinary request\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Please handle this."
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"67"]
                    if command == "fetch" and args == ("67", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("67", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"67 (FLAGS () X-GM-MSGID 72345 X-GM-THRID 207890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "NO", [b"store failed"]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            with patch.object(watcher, "push_email_ref", return_value=True):
                self.assertFalse(watcher.handle_unseen(Client(), args))
                self.assertFalse(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.processed_uids_path(args).exists())
            self.assertIn("record and delegate manager_mail", manager_file.read_text(encoding="utf-8"))

    def test_normal_watcher_does_not_write_live_mailbox_approval_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )
            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []
                    self.selects: list[tuple[str, bool]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.selects.append((mailbox, readonly))
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and args == (
                        "UNSEEN",
                        "FROM",
                        '"stevensichanghe@gmail.com"',
                    ):
                        return "OK", [b"72"]
                    if command == "search" and args == (
                        None,
                        "HEADER",
                        "Message-ID",
                        watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                    ):
                        return "OK", [b"71"]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            receipt_dir = watcher.live_mailbox_approval_receipts_dir(args)
            self.assertFalse(receipt_dir.exists())
            self.assertEqual([], client.selects)
            self.assertIn("record and delegate manager_mail", manager_file.read_text(encoding="utf-8"))
            self.assertEqual([("72", "+FLAGS", "(\\Seen)")], client.stores)

    def test_live_mailbox_approval_only_leaves_final_receipt_dir_fsync_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        if "Message-ID" in args:
                            return "OK", [b"71"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            receipts_dir = watcher.live_mailbox_approval_receipts_dir(args)
            receipt_fsyncs: list[Path] = []

            def fsync_receipts_second_time(path: Path) -> None:
                if path == receipts_dir:
                    receipt_fsyncs.append(path)
                    if len(receipt_fsyncs) == 2:
                        raise OSError("fsync failed")

            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(watcher, "fsync_directory", side_effect=fsync_receipts_second_time),
            ):
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertEqual([], client.stores)
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())
            self.assertFalse(list(watcher.live_mailbox_approval_receipts_dir(args).glob("*.receipt")))

    def test_live_mailbox_approval_only_removes_linked_receipt_on_dir_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        if "Message-ID" in args:
                            return "OK", [b"71"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            receipts_dir = watcher.live_mailbox_approval_receipts_dir(args)
            receipt_fsyncs: list[Path] = []

            def fail_after_link(path: Path) -> None:
                if path == receipts_dir:
                    receipt_fsyncs.append(path)
                    if len(receipt_fsyncs) == 2:
                        raise OSError("fsync failed")

            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(watcher, "fsync_directory", side_effect=fail_after_link),
            ):
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertEqual([], client.stores)
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())
            self.assertFalse(list(receipts_dir.glob("*.receipt")))

    def test_live_mailbox_approval_only_searches_pinned_thread_not_all_human_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )
            case = self

            class Client:
                def __init__(self) -> None:
                    self.searches: list[tuple[object, ...]] = []
                    self.stores: list[tuple[object, ...]] = []
                    self.selects: list[tuple[str, bool]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.selects.append((mailbox, readonly))
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        self.searches.append(args)
                        if args == (
                            None,
                            "HEADER",
                            "Message-ID",
                            watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                        ):
                            return "OK", [b"71"]
                        case.assertIn("SUBJECT", args)
                        case.assertIn("HEADER", args)
                        case.assertNotEqual(("UNSEEN", "FROM", '"stevensichanghe@gmail.com"'), args)
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
            ):
                self.assertTrue(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertEqual(3, len(client.searches))
            self.assertEqual(
                (
                    None,
                    "HEADER",
                    "Message-ID",
                    watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                ),
                client.searches[-1],
            )
            self.assertEqual([("72", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertEqual(1, len(list(watcher.live_mailbox_approval_receipts_dir(args).glob("*.receipt"))))

    def test_live_mailbox_approval_only_leaves_receipt_scan_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            receipts = state / "amh-live-mailbox-approval-receipts"
            receipts.mkdir(parents=True)
            (receipts / "existing.receipt").write_text("unreadable\n", encoding="utf-8")
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(watcher, "_live_mailbox_receipt_values", side_effect=OSError("read failed")),
            ):
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertEqual([], client.stores)
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())

    def test_normal_watcher_leaves_receipt_scan_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            receipts = state / "amh-live-mailbox-approval-receipts"
            receipts.mkdir(parents=True)
            (receipts / "existing.receipt").write_text("unreadable\n", encoding="utf-8")
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and args == (
                        "UNSEEN",
                        "FROM",
                        '"stevensichanghe@gmail.com"',
                    ):
                        return "OK", [b"72"]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(watcher, "_live_mailbox_receipt_values", side_effect=OSError("read failed")),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("approval must not route as ordinary AMH mail"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertEqual([("72", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertTrue(watcher.processed_uids_path(args).exists())
            self.assertTrue(manager_file.exists())

    def test_live_mailbox_approval_only_leaves_request_metadata_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        if "Message-ID" in args:
                            return "OK", [b""]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
            ):
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertEqual([], client.stores)
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_normal_watcher_leaves_live_approval_request_metadata_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and args == (
                        "UNSEEN",
                        "FROM",
                        '"stevensichanghe@gmail.com"',
                    ):
                        return "OK", [b"72"]
                    if command == "search" and args == (
                        None,
                        "HEADER",
                        "Message-ID",
                        watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                    ):
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("approval must not route as ordinary AMH mail"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertEqual([("72", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertTrue(watcher.processed_uids_path(args).exists())
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())
            self.assertTrue(manager_file.exists())

    def test_live_mailbox_approval_only_leaves_all_mail_restore_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    if mailbox == "INBOX":
                        return "NO", [b"restore failed"]
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "In-Reply-To" in args:
                            return "OK", [b"72"]
                        if "Message-ID" in args:
                            return "OK", [b"71"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
            ):
                with self.assertRaisesRegex(
                    watcher.LiveMailboxApprovalReconnectRequired,
                    "failed to restore INBOX",
                ):
                    watcher.handle_live_mailbox_approval_replies(client, args)
            self.assertEqual([], client.stores)
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_normal_watcher_leaves_all_mail_restore_failure_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    if mailbox == "INBOX":
                        return "NO", [b"restore failed"]
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and args == (
                        "UNSEEN",
                        "FROM",
                        '"stevensichanghe@gmail.com"',
                    ):
                        return "OK", [b"72"]
                    if command == "search" and args == (
                        None,
                        "HEADER",
                        "Message-ID",
                        watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                    ):
                        return "OK", [b"71"]
                    if command == "fetch" and args == ("72", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("72", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"72 (FLAGS () X-GM-MSGID 5555 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7777 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("approval must not route as ordinary AMH mail"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertEqual([("72", "+FLAGS", "(\\Seen)")], client.stores)
            self.assertTrue(watcher.processed_uids_path(args).exists())
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())
            self.assertTrue(manager_file.exists())

    def test_split_email_watcher_refuses_live_mailbox_receipt_wrong_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <stevensichanghe@gmail.com>\r\n"
                b"Return-Path: <stevensichanghe@gmail.com>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                b"To: Agent <sichangheagent@gmail.com>\r\n"
                b"Subject: wrong approval thread\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"77"]
                    if command == "fetch" and args == ("77", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("77", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"77 (FLAGS () X-GM-MSGID 5560 X-GM-THRID 7781 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_mailbox_receipt_wrong_request_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    "In-Reply-To: <other-request@test.local>\r\n"
                    "References: <other-request@test.local>\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"80"]
                    if command == "fetch" and args == ("80", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("80", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"80 (FLAGS () X-GM-MSGID 5563 X-GM-THRID 7784 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_mailbox_receipt_wrong_request_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and args == (
                        "UNSEEN",
                        "FROM",
                        '"stevensichanghe@gmail.com"',
                    ):
                        return "OK", [b"81"]
                    if command == "search" and args == (
                        None,
                        "HEADER",
                        "Message-ID",
                        watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID,
                    ):
                        return "OK", [b"71"]
                    if command == "fetch" and args == ("81", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("81", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"81 (FLAGS () X-GM-MSGID 5564 X-GM-THRID 7785 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 9999 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_receipt_with_duplicate_auth_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"82"]
                    if command == "fetch" and args == ("82", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("82", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"82 (FLAGS () X-GM-MSGID 5565 X-GM-THRID 7786 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertFalse(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_receipt_with_cc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    "Cc: extra@example.test\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"83"]
                    if command == "fetch" and args == ("83", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("83", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"83 (FLAGS () X-GM-MSGID 5566 X-GM-THRID 7787 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_live_mailbox_approval_only_leaves_receipt_request_without_sent_label_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "Message-ID" in args:
                            return "OK", [b"71"]
                        if "In-Reply-To" in args:
                            return "OK", [b"84"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("84", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("84", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"84 (FLAGS () X-GM-MSGID 5567 X-GM-THRID 7788 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Inbox))", b"")]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7788 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
            ):
                client = Client()
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(client, args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())
            self.assertFalse(watcher.live_mailbox_approval_processed_uids_path(args).exists())

    def test_split_email_watcher_refuses_duplicate_live_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            receipts = state / "amh-live-mailbox-approval-receipts"
            receipts.mkdir(parents=True)
            (receipts / "existing.receipt").write_text(
                "\n".join(
                    [
                        f"schema={watcher.AMH_LIVE_MAILBOX_APPROVAL_RECEIPT_SCHEMA}",
                        "stage=email1",
                        f"approval_request_message_id={watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}",
                        "receipt_finalized=true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"85"]
                    if command == "fetch" and args == ("85", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("85", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"85 (FLAGS () X-GM-MSGID 5568 X-GM-THRID 7789 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertEqual(["existing.receipt"], sorted(path.name for path in receipts.glob("*.receipt")))

    def test_split_email_watcher_refuses_atomic_duplicate_live_receipt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            receipts = state / "amh-live-mailbox-approval-receipts"
            receipts.mkdir(parents=True)
            inbox_identity = "sichangheagent@gmail.com\x00123"
            receipt_id = hashlib.sha256(
                (
                    f"{inbox_identity}\0email1\0"
                    f"{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}"
                ).encode()
            ).hexdigest()
            existing = receipts / f"{receipt_id}.receipt"
            existing.write_text("incomplete concurrent receipt\n", encoding="utf-8")
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                    self.readonly = readonly
                    return "OK", [b""]

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search" and "HEADER" in args:
                        if "Message-ID" in args:
                            return "OK", [b"71"]
                        if "In-Reply-To" in args:
                            return "OK", [b"86"]
                        return "OK", [b""]
                    if command == "fetch" and args == ("86", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("86", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"86 (FLAGS () X-GM-MSGID 5569 X-GM-THRID 7790 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"71 (FLAGS () X-GM-MSGID 4444 X-GM-THRID 7790 "
                                b"X-GM-LABELS (\\Sent) "
                                b'INTERNALDATE "21-Aug-2026 11:58:20 +0000")',
                                b"",
                            )
                        ]
                    if command == "fetch" and args == ("71", "(X-GM-LABELS)"):
                        return "OK", [(b"71 (X-GM-LABELS (\\Sent))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity=inbox_identity,
                live_mailbox_approval_only=True,
                live_mailbox_stage="email1",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
            ):
                self.assertFalse(watcher.handle_live_mailbox_approval_replies(Client(), args))
            self.assertEqual(["incomplete concurrent receipt\n"], [existing.read_text(encoding="utf-8")])
            self.assertEqual([existing.name], sorted(path.name for path in receipts.glob("*.receipt")))

    def test_split_email_watcher_refuses_live_mailbox_receipt_after_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"78"]
                    if command == "fetch" and args == ("78", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("78", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"78 (FLAGS () X-GM-MSGID 5561 X-GM-THRID 7782 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "22-Aug-2026 15:30:00 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(watcher.time, "time", return_value=1787412599.0),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_mailbox_receipt_processed_after_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"79"]
                    if command == "fetch" and args == ("79", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("79", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [
                            (
                                b"79 (FLAGS () X-GM-MSGID 5562 X-GM-THRID 7783 "
                                b"X-GM-LABELS (\\Inbox) "
                                b'INTERNALDATE "21-Aug-2026 12:00:10 +0000")',
                                b"",
                            )
                        ]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(watcher.time, "time", return_value=1787412600.0),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_live_mailbox_approval_parser_requires_one_exact_command_line(self) -> None:
        email2 = (
            "Approved: send Email 2 only for Gmail threading test "
            "from sichangheagent@gmail.com to stevensichanghe@gmail.com using "
            "Email 1 Message-ID: <gmail-threading-test-1@test.local>. "
            "Approval code GT-20260821-EMAIL2."
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"> {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"Do not send. {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f" {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT} \n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n{email2}\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"\n{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n\n"
            )
        )
        self.assertIsNone(
            watcher._live_mailbox_stage(  # noqa: SLF001
                f"> {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n{email2}\n"
            ),
        )

    def test_live_mailbox_exact_sender_rejects_duplicate_identity_headers(self) -> None:
        for header_name, duplicate_line in (
            ("From", "From: Human <stevensichanghe@gmail.com>\r\n"),
            ("Sender", "Sender: Human <stevensichanghe@gmail.com>\r\n"),
            ("Return-Path", "Return-Path: <stevensichanghe@gmail.com>\r\n"),
        ):
            with self.subTest(header_name=header_name):
                raw = (
                    b"From: Human <stevensichanghe@gmail.com>\r\n"
                    b"Sender: Human <stevensichanghe@gmail.com>\r\n"
                    b"Return-Path: <stevensichanghe@gmail.com>\r\n"
                    + duplicate_line.encode("utf-8")
                    + b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    b"\r\n"
                    + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                    + b"\n"
                )
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                self.assertFalse(
                    watcher.exact_human_sender(
                        msg,
                        "stevensichanghe@gmail.com",
                        require_transport_identity=True,
                    )
                )

    def test_split_email_watcher_refuses_live_receipt_without_provider_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <stevensichanghe@gmail.com>\r\n"
                b"Return-Path: <stevensichanghe@gmail.com>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                b"To: Agent <sichangheagent@gmail.com>\r\n"
                b"Subject: live approval\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"73"]
                    if command == "fetch" and args == ("73", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("73", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"73 (FLAGS () X-GM-MSGID 5556 X-GM-THRID 7778 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_live_receipt_without_gmail_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <stevensichanghe@gmail.com>\r\n"
                b"Return-Path: <stevensichanghe@gmail.com>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                b"To: Agent <sichangheagent@gmail.com>\r\n"
                b"Subject: live approval\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"74"]
                    if command == "fetch" and args == ("74", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("74", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"74 (FLAGS () X-GM-MSGID 5557 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_multipart_live_approval_receipt_with_mismatched_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <stevensichanghe@gmail.com>"
            msg["Return-Path"] = "<stevensichanghe@gmail.com>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com"
            msg["To"] = "Agent <sichangheagent@gmail.com>"
            msg["Subject"] = watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT
            msg["In-Reply-To"] = watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID
            msg["References"] = watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID
            msg.set_content(f"{watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT}\n")
            msg.add_alternative("<p>Do not send.</p>", subtype="html")
            raw_mime = msg.as_bytes()

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"75"]
                    if command == "fetch" and args == ("75", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("75", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"75 (FLAGS () X-GM-MSGID 5558 X-GM-THRID 7779 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_refuses_attachment_live_approval_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                (
                    "From: Human <stevensichanghe@gmail.com>\r\n"
                    "Return-Path: <stevensichanghe@gmail.com>\r\n"
                    "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=stevensichanghe@gmail.com\r\n"
                    "To: Agent <sichangheagent@gmail.com>\r\n"
                    f"Subject: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT}\r\n"
                    f"In-Reply-To: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    f"References: {watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "Content-Disposition: attachment; filename=\"approval.txt\"\r\n"
                    "\r\n"
                ).encode()
                + watcher.AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT.encode("utf-8")
                + b"\n"
            )

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"76"]
                    if command == "fetch" and args == ("76", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("76", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"76 (FLAGS () X-GM-MSGID 5559 X-GM-THRID 7780 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        return "OK", [b""]
                    raise AssertionError((command, args))

            manager_file = root / "work_manager_today.md"
            args = watcher.Args(
                root,
                "",
                root / "manager_mail",
                state,
                manager_file,
                True,
                "stevensichanghe@gmail.com",
                0,
                Path("/bin/false"),
                manager_target="wl:1",
                mail_thresholds=False,
                inbox_identity="sichangheagent@gmail.com\x00123",
            )
            with (
                patch.object(watcher, "TRUST_LIVE_MAILBOX_AUTH_RESULTS", True),
                patch.object(
                    watcher,
                    "AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR",
                    root / "manager_mail",
                ),
                patch.object(watcher, "AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR", state),
                patch.object(
                    watcher,
                    "load_amh_bridge_modules",
                    side_effect=AssertionError("untagged approval must not enter AMH"),
                ),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(watcher.live_mailbox_approval_receipts_dir(args).exists())

    def test_split_email_watcher_falls_back_before_amh_for_unsupported_agent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            raw_mime = (
                b"From: Human <human@example.test>\r\n"
                b"Return-Path: <human@example.test>\r\n"
                b"Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=human@example.test\r\n"
                b"Subject: [DisplayName] AMH route\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Please handle this."
            )

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"66"]
                    if command == "fetch" and args == ("66", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("66", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"66 (FLAGS () X-GM-MSGID 62345 X-GM-THRID 112233 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        self.stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "DisplayName"

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        raise AssertionError("unsupported agent must not construct identity")

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> FakeDecision:
                    return FakeDecision()

            client = Client()
            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00999")
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=False),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=AssertionError("unsupported agent must not launch")),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertFalse(watcher.amh_committed_messages_path(args).exists())
            mail_name = watcher.mail_artifact_name(args, "66")
            self.assertIn(f"(record and delegate manager_mail/{mail_name})", manager_file.read_text(encoding="utf-8"))
            self.assertEqual([("66", "+FLAGS", "(\\Seen)")], client.stores)

    def test_split_email_watcher_does_not_fallback_after_ambiguous_amh_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[pb] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()

            class Client:
                def __init__(self) -> None:
                    self.stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"62"]
                    if command == "fetch" and args == ("62", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("62", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"62 (FLAGS () X-GM-MSGID 22345 X-GM-THRID 77890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        raise AssertionError("ambiguous AMH result must not mark Seen")
                    raise AssertionError((command, args))

            class FakeKind:
                value = "timeout"

            class FakeFailure:
                kind = FakeKind()

            class FakeOutcome:
                failure = FakeFailure()
                detail = "ingest timed out; AMH commit is unconfirmed"

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "pb"

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        pass

                @staticmethod
                def derive_replay_ids(_identity: object) -> object:
                    class Ids:
                        operation_id = "op-ambiguous"

                    return Ids()

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitFailure:
                    pass

                class BridgeConfig:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                class WatcherMessage:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> FakeDecision:
                    return FakeDecision()

                @staticmethod
                def bridge_watcher_message(_message: object, **_kwargs: object) -> object:
                    return FakeOutcome()

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test")
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=AssertionError("ambiguous AMH result must not launch")),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse(watcher.processed_uids_path(args).exists())

    def test_split_email_watcher_keeps_amh_mail_replayable_when_route_launch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[pb] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()

            class Client:
                stores: list[tuple[object, ...]] = []

                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"63"]
                    if command == "fetch" and args == ("63", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("63", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"63 (FLAGS () X-GM-MSGID 32345 X-GM-THRID 87890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        raise AssertionError("failed AMH launch must not mark Seen")
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    pass

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        pass

                @staticmethod
                def derive_replay_ids(_identity: object) -> object:
                    class Ids:
                        operation_id = "op-launch-fails"

                    return Ids()

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitStage:
                    class CURSOR:
                        value = "cursor"

                class PostCommitFailure:
                    def __init__(self, **kwargs: object) -> None:
                        self.stage = kwargs["stage"]
                        self.detail = kwargs["detail"]

                class BridgeConfig:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                class WatcherMessage:
                    def __init__(self, **kwargs: object) -> None:
                        self.kwargs = kwargs

                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> FakeDecision:
                    return FakeDecision()

                @staticmethod
                def bridge_watcher_message(_message: object, persist_cursor: object, **_kwargs: object) -> object:
                    try:
                        persist_cursor()
                    except Exception as exc:
                        return FakeBridge.PostCommitFailure(stage=FakeBridge.PostCommitStage.CURSOR, detail=str(exc))
                    raise AssertionError("failed launcher must be reported as postcommit failure")

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test")
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=RuntimeError("launcher down")),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())
            self.assertFalse(watcher.processed_uids_path(args).exists())
            self.assertTrue(watcher.amh_committed_messages_path(args).is_file())

            class BridgeUnavailableClient:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"63"]
                    if command == "fetch" and args == ("63", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("63", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"63 (FLAGS () X-GM-MSGID 32345 X-GM-THRID 87890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        raise AssertionError("AMH-committed replay must not mark Seen")
                    raise AssertionError((command, args))

            changed_epoch_args = split_args(root, state, manager_file, manager_target="wl:1", inbox_identity="agent@example.test\x00888")
            self.assertEqual(watcher.amh_committed_messages_path(args), watcher.amh_committed_messages_path(changed_epoch_args))
            with patch.object(watcher, "load_amh_bridge_modules", return_value=None):
                self.assertTrue(watcher.handle_unseen(BridgeUnavailableClient(), changed_epoch_args))
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())

            class MissingMetadataClient:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"63"]
                    if command == "fetch" and args == ("63", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("63", watcher.GMAIL_METADATA_FETCH):
                        return "NO", []
                    if command == "store":
                        raise AssertionError("AMH-committed replay must not mark Seen")
                    raise AssertionError((command, args))

            with patch.object(watcher, "load_amh_bridge_modules", side_effect=AssertionError("metadata failure must stop before bridge")):
                self.assertTrue(watcher.handle_unseen(MissingMetadataClient(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())

            class SubjectRouteRemovedClient:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"63"]
                    if command == "fetch" and args == ("63", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("63", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"63 (FLAGS () X-GM-MSGID 32345 X-GM-THRID 87890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        raise AssertionError("AMH-committed subject route removal must not mark Seen")
                    raise AssertionError((command, args))

            class NotAmhDecision:
                routes_to_amh = False
                route = object()
                reason = "not AMH now"

            class SubjectRouteRemovedBridge(FakeBridge):
                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> object:
                    return NotAmhDecision()

            with patch.object(watcher, "load_amh_bridge_modules", return_value=(SubjectRouteRemovedBridge, FakeIngress)):
                self.assertTrue(watcher.handle_unseen(SubjectRouteRemovedClient(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())

            class ReplayNotCommittedOutcome:
                detail = "temporary replay refusal"

            class ReplayNotCommittedBridge(FakeBridge):
                @staticmethod
                def bridge_watcher_message(_message: object, **_kwargs: object) -> object:
                    return ReplayNotCommittedOutcome()

            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(ReplayNotCommittedBridge, FakeIngress)),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(BridgeUnavailableClient(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse((root / "manager_mail").exists())

    def test_split_email_watcher_refuses_amh_new_work_without_manager_target_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[pb] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()
            stores: list[tuple[object, ...]] = []

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"64"]
                    if command == "fetch" and args == ("64", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("64", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"64 (FLAGS () X-GM-MSGID 42345 X-GM-THRID 97890 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        stores.append(args)
                        return "OK", [b""]
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "pb"

                class ProviderMessageIdentity:
                    def __init__(self, **_kwargs: object) -> None:
                        pass

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                class SideBySideOwner:
                    AMH = "amh"

                class MailboxAdvanced:
                    pass

                class PostCommitFailure:
                    pass

                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> FakeDecision:
                    return FakeDecision()

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, inbox_identity="agent@example.test")
            client = Client()
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=AssertionError("must not launch")),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            self.assertFalse(watcher.amh_committed_messages_path(args).exists())
            mail_name = watcher.mail_artifact_name(args, "64")
            self.assertIn(f"(record and delegate manager_mail/{mail_name})", manager_file.read_text(encoding="utf-8"))
            self.assertEqual([("64", "+FLAGS", "(\\Seen)")], stores)

    def test_split_email_watcher_does_not_fallback_for_committed_amh_without_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            root.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            msg = EmailMessage()
            msg["From"] = "Human <human@example.test>"
            msg["Return-Path"] = "<human@example.test>"
            msg["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=human@example.test"
            msg["Subject"] = "[pb] AMH route"
            msg.set_content("Please handle this in AMH.")
            raw_mime = msg.as_bytes()

            class Client:
                def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                    if command == "search":
                        return "OK", [b"67"]
                    if command == "fetch" and args == ("67", "(BODY.PEEK[])"):
                        return "OK", [(b"RFC822", raw_mime)]
                    if command == "fetch" and args == ("67", watcher.GMAIL_METADATA_FETCH):
                        return "OK", [(b"67 (FLAGS () X-GM-MSGID 72345 X-GM-THRID 445566 X-GM-LABELS (\\Inbox))", b"")]
                    if command == "store":
                        raise AssertionError("committed AMH replay must not mark Seen through fallback")
                    raise AssertionError((command, args))

            class FakeIngress:
                class ResponsibleAgentRoute:
                    agent_id = "pb"

            class FakeDecision:
                routes_to_amh = True
                route = FakeIngress.ResponsibleAgentRoute()
                reason = "test route"

            class FakeBridge:
                @staticmethod
                def route_from_watcher_subject(_subject: str, **_kwargs: object) -> FakeDecision:
                    return FakeDecision()

            manager_file = root / "work_manager_today.md"
            args = split_args(root, state, manager_file, inbox_identity="agent@example.test")
            watcher.record_amh_committed_message(args, "old-uid", "72345", "op-committed")
            with (
                patch.object(watcher, "load_amh_bridge_modules", return_value=(FakeBridge, FakeIngress)),
                patch.object(watcher, "amh_agent_status_is_supported", return_value=True),
                patch.object(watcher, "launch_amh_worker_for_route", side_effect=AssertionError("committed replay must not launch from fallback guard")),
                patch.object(watcher, "DEFAULT_AMH_WORKDIR", root),
            ):
                self.assertTrue(watcher.handle_unseen(Client(), args))
            self.assertFalse(manager_file.exists())
            self.assertFalse(watcher.processed_uids_path(args).exists())


if __name__ == "__main__":
    unittest.main()
