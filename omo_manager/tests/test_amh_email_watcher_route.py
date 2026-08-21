from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from email.message import EmailMessage
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
