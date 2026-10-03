from __future__ import annotations

import unittest
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_email_subject import (
    MailRouteProfile,
    RecentHeader,
    SubjectInputError,
    authenticated_referenced_thread_target,
    find_recent_thread_for_tmux_target,
    find_recent_thread_matching,
    prepare_latest_thread_for_tmux_target,
    prepare_subject_and_headers,
    select_recent_thread,
    subject_tmux_target,
    subject_task_stem,
    subject_base,
)


class EmailSubjectRouteContinuityTests(unittest.TestCase):
    profile = MailRouteProfile("agent@example.test", "human@example.test", "primary")

    def test_authenticated_sender_keeps_agent_address_and_exact_once_receipt(self) -> None:
        module_path = Path(__file__).resolve().parents[2] / "helper.sh" / "email_me.py"
        spec = importlib.util.spec_from_file_location("email_me_route_test", module_path)
        assert spec is not None and spec.loader is not None
        sender = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sender
        spec.loader.exec_module(sender)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n", encoding="utf-8")
            (root / "worker_0927.md").write_text("---\nversion: v1.0.0\nstatus: running\nrunat: dw:64\ntool: codex\nmanagerat: dw:1\nis_manager: false\npending_task_items: []\n---\n", encoding="utf-8")
            with patch.dict(os.environ, {"OMO_WORK_LOGS_ROOT": str(root)}):
                self.assertEqual("worker_0927", sender.email_subject_target("dw:64"))
                self.assertEqual("worker_0927", sender.email_subject_target("dw:64"))
                receipt, _payload = sender.exact_once_email_record("Update", "[worker_0927] Update", "body", "dw:64")
                self.assertTrue(receipt)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    sender.exact_once_email_record("Update", "[other:64] Update", "body", "dw:64")
            (root / "work_manager_today.md").write_text("manager log\n", encoding="utf-8")
            with patch.dict(os.environ, {"OMO_WORK_LOGS_ROOT": str(root)}):
                self.assertEqual("wl:1", sender.email_subject_target("wl:1"))
            (root / "pb.md").write_text((root / "worker_0927.md").read_text(encoding="utf-8").replace("dw:64", "pb:1"), encoding="utf-8")
            (root / "TODO.md").write_text("current:\n- worker_0927.md dw:64\n- pb.md pb:1\n", encoding="utf-8")
            with patch.dict(os.environ, {"OMO_WORK_LOGS_ROOT": str(root)}):
                self.assertEqual("pb:1", sender.email_subject_target("pb:1"))

    def test_task_tag_preserves_original_human_thread_and_legacy_tags(self) -> None:
        parent = RecentHeader("human@example.test", "Original Human subject", datetime.now().astimezone(), "<human@example.test>")
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent):
            subject, headers = prepare_subject_and_headers("Re: Original Human subject", "worker_0927", route_profile=self.profile)
        self.assertEqual("Re: [worker_0927] Original Human subject", subject)
        self.assertEqual(parent.message_id, headers["In-Reply-To"])
        self.assertEqual("worker_0927", subject_task_stem(subject))
        self.assertEqual("", subject_tmux_target(subject))
        self.assertEqual("Original Human subject", subject_base(subject))
        self.assertEqual("", subject_tmux_target("Original Human subject"))
        self.assertEqual("wl:1", subject_tmux_target("Re: [wl:1] Original Human subject"))
        self.assertEqual("og:legacy", subject_tmux_target("[og:legacy] Original Human subject"))
        self.assertEqual("worker_0927", subject_task_stem("Re: [a] [worker_0927] Original Human subject"))
        self.assertEqual("Original Human subject", subject_base("Re: [worker_0927.md] Original Human subject"))
        self.assertEqual("Original Human subject", subject_base("Re: [worker_0927.md] Original Human subject"))

    def test_different_task_cannot_retag_task_thread(self) -> None:
        parent = RecentHeader("human@example.test", "[other_0927] Original Human subject", datetime.now().astimezone(), "<human@example.test>")
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent):
            with self.assertRaisesRegex(SubjectInputError, "may not retag"):
                prepare_subject_and_headers("Re: Original Human subject", "worker_0927", route_profile=self.profile)

    def test_first_task_tag_in_thread_remains_bound_after_retag(self) -> None:
        root = "<root@example.test>"
        first = RecentHeader("agent@example.test", "[worker_0927] Update", datetime.fromisoformat("2026-09-12T08:00:00+00:00"), "<first@example.test>", root)
        retagged = RecentHeader("agent@example.test", "[other_0927] Update", datetime.fromisoformat("2026-09-12T09:00:00+00:00"), "<latest@example.test>", f"{root} {first.message_id}")
        selected = select_recent_thread([retagged, first], reject_ambiguous=True)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("worker_0927", selected.thread_target)
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=selected):
            with self.assertRaisesRegex(SubjectInputError, "may not retag"):
                prepare_subject_and_headers("Re: Update", "other_0927", route_profile=self.profile)

    def test_exact_recovery_preserves_verified_thread_target(self) -> None:
        parent = RecentHeader(
            "human@example.test",
            "Re: [config:2] Try Pangram for hard data",
            datetime.now().astimezone(),
            "<source2048@example.test>",
            thread_target="dw:15",
        )
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent):
            with self.assertRaisesRegex(SubjectInputError, "may not retag"):
                prepare_subject_and_headers("Re: Try Pangram for hard data", "DeGenTWeb_writeup:0", route_profile=self.profile)
            prepared, headers = prepare_subject_and_headers(
                "Re: Try Pangram for hard data",
                "DeGenTWeb_writeup:0",
                route_profile=self.profile,
                preserve_verified_thread_target="dw:15",
            )
        self.assertEqual("Re: [dw:15] Try Pangram for hard data", prepared)
        self.assertEqual(parent.message_id, headers["In-Reply-To"])
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent), self.assertRaisesRegex(SubjectInputError, "required preserved target"):
            prepare_subject_and_headers(
                "Re: Try Pangram for hard data",
                "DeGenTWeb_writeup:0",
                route_profile=self.profile,
                preserve_verified_thread_target="config:2",
            )

    def test_reply_cannot_retag_another_agents_addressed_thread(self) -> None:
        parent = RecentHeader(
            "human@example.test",
            "Re: [config:24] Investigate config agent task file problems",
            datetime.now().astimezone(),
            "<source1733@example.test>",
            "<root@example.test>",
            thread_target="config:24",
        )

        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent):
            with self.assertRaisesRegex(SubjectInputError, "addressed to config:24; wl:1 may not retag it"):
                prepare_subject_and_headers("Re: Investigate config agent task file problems", "wl:1", route_profile=self.profile)

    def test_reply_keeps_matching_target_and_allows_untagged_parent(self) -> None:
        for parent_subject in (
            "Re: [config:24] Investigate config agent task file problems",
            "Investigate config agent task file problems",
        ):
            with self.subTest(parent_subject=parent_subject):
                parent = RecentHeader(
                    "human@example.test",
                    parent_subject,
                    datetime.now().astimezone(),
                    "<parent@example.test>",
                )
                with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=parent):
                    subject, headers = prepare_subject_and_headers("Re: Investigate config agent task file problems", "config:24.0", route_profile=self.profile)

                self.assertEqual("Re: [config:24] Investigate config agent task file problems", subject)
                self.assertEqual("<parent@example.test>", headers["In-Reply-To"])

    def test_thread_route_uses_first_authenticated_target_not_latest_retag(self) -> None:
        root = "<root@example.test>"
        first = RecentHeader(
            "agent@example.test",
            "Re: [config:24] Investigate config agent task file problems",
            datetime.fromisoformat("2026-09-12T08:00:00+00:00"),
            "<config24@example.test>",
            root,
        )
        retagged = RecentHeader(
            "agent@example.test",
            "Re: [wl:1] Investigate config agent task file problems",
            datetime.fromisoformat("2026-09-12T09:00:00+00:00"),
            "<wl1@example.test>",
            f"{root} <config24@example.test>",
        )

        selected = select_recent_thread([retagged, first], reject_ambiguous=True)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("<wl1@example.test>", selected.message_id)
        self.assertEqual("config:24", selected.thread_target)
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=selected):
            subject, _headers = prepare_subject_and_headers("Re: Investigate config agent task file problems", "config:24.0", route_profile=self.profile)
        self.assertEqual("Re: [config:24] Investigate config agent task file problems", subject)

    def test_lookup_recovers_older_changed_subject_target_through_in_reply_to(self) -> None:
        root = RecentHeader(
            "human@example.test",
            "Original subject",
            datetime.fromisoformat("2026-08-01T08:00:00+00:00"),
            "<root@example.test>",
            recipient="agent@example.test",
        )
        first = RecentHeader(
            "agent@example.test",
            "Re: [config:24] Original subject",
            datetime.fromisoformat("2026-08-01T09:00:00+00:00"),
            "<config24@example.test>",
            recipient="human@example.test",
            in_reply_to=root.message_id,
        )
        latest = RecentHeader(
            "agent@example.test",
            "Re: [wl:1] Renamed subject",
            datetime.now().astimezone(),
            "<wl1@example.test>",
            recipient="human@example.test",
            in_reply_to=first.message_id,
        )
        headers = {"9": latest, "2": first, "1": root}

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            mailbox = ""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def login(self, *_args: object) -> None:
                return None

            def logout(self) -> None:
                return None

            def select(self, mailbox: str, **_kwargs: object) -> tuple[str, list[bytes]]:
                self.mailbox = mailbox.strip('"')
                return "OK", [b""]

            def uid(self, command: str, *arguments: object) -> tuple[str, list[bytes]]:
                if command != "search":
                    raise AssertionError((command, arguments))
                if "HEADER" in arguments:
                    message_id = str(arguments[-1]).strip('"')
                    if self.mailbox == "[Gmail]/Sent Mail" and message_id == first.message_id:
                        return "OK", [b"2"]
                    if self.mailbox == "INBOX" and message_id == root.message_id:
                        return "OK", [b"1"]
                    return "OK", [b""]
                return "OK", [b"9" if self.mailbox == "[Gmail]/Sent Mail" else b""]

        with (
            patch("omo_manager.omo_email_subject.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.omo_email_subject.imaplib.IMAP4_SSL", FakeClient),
            patch("omo_manager.omo_email_subject.fetch_recent_headers", side_effect=lambda _client, uids: [headers[uid] for uid in uids]),
        ):
            selected = find_recent_thread_matching(lambda _header: True, route_profile=self.profile, reject_ambiguous=True)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("<wl1@example.test>", selected.message_id)
        self.assertEqual("config:24", selected.thread_target)

    def test_exact_message_id_routes_two_same_subject_replies_independently(self) -> None:
        original = RecentHeader("agent@example.test", "[config:24] Update", datetime.now().astimezone(), "<original@example.test>", recipient="human@example.test")
        replies = [
            RecentHeader("human@example.test", "Re: Update", datetime.now().astimezone(), f"<reply-{number}@example.test>", recipient="agent@example.test", in_reply_to=original.message_id)
            for number in (1, 2)
        ]
        headers = {"1": replies[0], "2": replies[1], "3": original}

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class Client:
            mailbox = ""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def login(self, *_args: object) -> None:
                return None

            def logout(self) -> None:
                return None

            def select(self, mailbox: str, **_kwargs: object) -> tuple[str, list[bytes]]:
                self.mailbox = mailbox.strip('"')
                return "OK", [b""]

            def uid(self, command: str, *arguments: object) -> tuple[str, list[bytes]]:
                if command != "search" or "HEADER" not in arguments:
                    raise AssertionError((command, arguments))
                message_id = str(arguments[-1]).strip('"')
                if self.mailbox == "INBOX":
                    return "OK", [b"1" if message_id == replies[0].message_id else b"2" if message_id == replies[1].message_id else b""]
                return "OK", [b"3" if message_id == original.message_id else b""]

        with (
            patch("omo_manager.omo_email_subject.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.omo_email_subject.imaplib.IMAP4_SSL", Client),
            patch("omo_manager.omo_email_subject.fetch_recent_headers", side_effect=lambda _client, uids: [headers[uid] for uid in uids]),
        ):
            for reply in replies:
                target = authenticated_referenced_thread_target(
                    Client(), reply, [reply], [
                        ("[Gmail]/Sent Mail", "agent@example.test", "human@example.test"),
                        ("INBOX", "human@example.test", "agent@example.test"),
                    ],
                )
                self.assertEqual("config:24", target)

    def test_verified_human_retag_cannot_supply_owner_without_agent_ancestor(self) -> None:
        original = RecentHeader("agent@example.test", "[config:24] Update", datetime.now().astimezone(), "<agent-root@example.test>", recipient="human@example.test")
        reply = RecentHeader("human@example.test", "Re: [victim_task] Update", datetime.now().astimezone(), "<human-reply@example.test>", recipient="agent@example.test", in_reply_to=original.message_id)
        target = authenticated_referenced_thread_target(
            object(), reply, [reply, original], [],
            agent_sender="agent@example.test", human_recipient="human@example.test",
        )
        self.assertEqual("config:24", target)
        human_root = RecentHeader("human@example.test", "[victim_task] Update", datetime.now().astimezone(), "<human-root@example.test>", recipient="agent@example.test")
        unowned = RecentHeader("human@example.test", "Re: [victim_task] Update", datetime.now().astimezone(), "<human-latest@example.test>", recipient="agent@example.test", in_reply_to=human_root.message_id)
        self.assertEqual(
            "",
            authenticated_referenced_thread_target(
                object(), unowned, [unowned, human_root], [],
                agent_sender="agent@example.test", human_recipient="human@example.test",
            ),
        )

    def test_ancestor_lookup_fails_closed_for_missing_ambiguous_or_wrong_route(self) -> None:
        first_id = "<config24@example.test>"
        latest = RecentHeader(
            "agent@example.test",
            "Re: [wl:1] Renamed subject",
            datetime.now().astimezone(),
            "<wl1@example.test>",
            recipient="human@example.test",
            in_reply_to=first_id,
        )
        good = RecentHeader(
            "agent@example.test",
            "Re: [config:24] Original subject",
            datetime.now().astimezone(),
            first_id,
            recipient="human@example.test",
        )

        class FakeClient:
            def __init__(self, headers: list[RecentHeader]) -> None:
                self.headers = headers

            def select(self, *_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
                return "OK", [b""]

            def uid(self, command: str, *_arguments: object) -> tuple[str, list[bytes]]:
                if command == "search":
                    return "OK", [b"1 2" if self.headers else b""]
                raise AssertionError(command)

        searches = [("[Gmail]/Sent Mail", "agent@example.test", "human@example.test")]
        cases = (
            ([], "unavailable"),
            ([good, good], "ambiguous"),
            ([RecentHeader(**{**good.__dict__, "recipient": "other@example.test"})], "unavailable"),
        )
        for headers, error in cases:
            with self.subTest(error=error), patch("omo_manager.omo_email_subject.fetch_recent_headers", return_value=headers), self.assertRaisesRegex(SubjectInputError, error):
                authenticated_referenced_thread_target(FakeClient(headers), latest, [latest], searches)  # type: ignore[arg-type]

    def test_root_without_in_reply_to_remains_valid(self) -> None:
        root = RecentHeader(
            "human@example.test",
            "[config:24] New topic",
            datetime.now().astimezone(),
            "<root@example.test>",
            recipient="agent@example.test",
        )
        self.assertEqual(
            "config:24",
            authenticated_referenced_thread_target(object(), root, [root], []),  # type: ignore[arg-type]
        )

    def test_session_bound_lookup_accepts_an_untagged_subject(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            "agent@example.test",
            "Re: Request",
            datetime.now().astimezone(),
            "<sent@example.test>",
            recipient="human@example.test",
            agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread_matching", return_value=header) as lookup:
            self.assertEqual(
                header,
                find_recent_thread_for_tmux_target("wl:1", self.profile, required_agent_session=session),
            )
        self.assertTrue(lookup.call_args.args[0](header))
        self.assertEqual((), lookup.call_args.args[1:])
        self.assertEqual(session, lookup.call_args.kwargs["required_agent_session"])

    def test_session_bound_latest_thread_preserves_exact_subject(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            "agent@example.test",
            " Re: [omo] exact Human wording ",
            datetime.now().astimezone(),
            "<sent@example.test>",
            recipient="human@example.test",
            agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.verified_recent_thread_header", return_value=header):
            subject, reply_headers = prepare_latest_thread_for_tmux_target(
                "wl:1",
                route_profile=self.profile,
                required_agent_session=session,
            )
        self.assertEqual(header.subject, subject)
        self.assertEqual(header.message_id, reply_headers["In-Reply-To"])

    def test_same_subject_primary_update_without_reply_prefix_chains_session(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            "agent@example.test",
            "[shut_down_codex] Closing Codex and turning off fast",
            datetime.now().astimezone(),
            "<previous@example.test>",
            recipient="human@example.test",
            agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread", return_value=header) as lookup:
            subject, headers = prepare_subject_and_headers(
                "Closing Codex and turning off fast",
                "shut_down_codex",
                route_profile=self.profile,
                required_agent_session=session,
            )
        self.assertEqual("Re: [shut_down_codex] Closing Codex and turning off fast", subject)
        self.assertEqual(header.message_id, headers["In-Reply-To"])
        self.assertEqual(header.message_id, headers["References"])
        self.assertEqual(session, lookup.call_args.kwargs["required_agent_session"])

    def test_new_primary_subject_starts_fresh_without_same_session_parent(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        with patch("omo_manager.omo_email_subject.find_recent_thread", return_value=None):
            subject, headers = prepare_subject_and_headers(
                "New topic", "shut_down_codex", route_profile=self.profile,
                required_agent_session=session,
            )
        self.assertEqual("[shut_down_codex] New topic", subject)
        self.assertEqual({}, headers)

    def test_changed_explicit_reply_uses_authenticated_session_thread(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        subject = "Re: [shut_down_codex] Closing Codex and turning off fast"
        parent_id = "<179079635152.1477563.10754487308162452655@gmail.com>"
        now = datetime.now().astimezone()
        previous = RecentHeader(
            self.profile.agent_address, subject, now - timedelta(minutes=2), parent_id,
            recipient=self.profile.counterparty_address, agent_session=session,
        )
        other_session = RecentHeader(
            self.profile.agent_address, subject + " — current", now,
            "<179088292457.1170842.468749574602537914@gmail.com>",
            recipient=self.profile.counterparty_address,
            agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99b",
        )
        inbound = RecentHeader(
            self.profile.counterparty_address, subject, now,
            "<human@example.test>", recipient=self.profile.agent_address,
            in_reply_to=parent_id,
        )
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                if not readonly:
                    raise AssertionError("thread lookup must be read-only")
                return "OK", []

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes]]:
                if command != "search":
                    raise AssertionError("unexpected mailbox mutation")
                return "OK", [b"1 2 3"]

            def logout(self) -> None:
                return None

        with (
            patch("omo_manager.omo_email_subject.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.omo_email_subject.imaplib.IMAP4_SSL", FakeClient),
            patch("omo_manager.omo_email_subject.fetch_recent_headers", return_value=[previous, other_session, inbound]),
        ):
            prepared, headers = prepare_subject_and_headers(
                subject + " — current", "shut_down_codex", route_profile=self.profile,
                required_agent_session=session,
            )
        self.assertEqual(subject, prepared)
        self.assertEqual({"In-Reply-To": parent_id, "References": parent_id}, headers)

    def test_first_explicit_reply_retains_exact_subject_lookup(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", return_value=None), patch(
            "omo_manager.omo_email_subject.find_recent_thread", return_value=None,
        ) as lookup:
            with self.assertRaisesRegex(SubjectInputError, "no exact email thread"):
                prepare_subject_and_headers(
                    "Re: First reply", "shut_down_codex", route_profile=self.profile,
                    required_agent_session=session,
                )
        self.assertEqual("first reply", lookup.call_args.args[0])
        self.assertEqual(session, lookup.call_args.kwargs["required_agent_session"])

    def test_explicit_reply_to_nonreply_parent_keeps_canonical_subject(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            self.profile.agent_address, "[shut_down_codex] Closing Codex and turning off fast",
            datetime.now().astimezone(), "<previous@example.test>",
            recipient=self.profile.counterparty_address, agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", return_value=header):
            subject, headers = prepare_subject_and_headers(
                "Re: Closing Codex and turning off fast — current", "shut_down_codex",
                route_profile=self.profile, required_agent_session=session,
            )
        self.assertEqual("Re: [shut_down_codex] Closing Codex and turning off fast", subject)
        self.assertEqual({"In-Reply-To": header.message_id, "References": header.message_id}, headers)

    def test_unprofiled_explicit_reply_does_not_override_selected_subject(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target") as latest, patch(
            "omo_manager.omo_email_subject.find_recent_thread", return_value=None,
        ) as exact:
            subject, headers = prepare_subject_and_headers(
                "Re: Deliberately separate topic", "shut_down_codex", required_agent_session=session,
            )
        self.assertEqual("Re: [shut_down_codex] Deliberately separate topic", subject)
        self.assertEqual({}, headers)
        latest.assert_not_called()
        self.assertEqual("deliberately separate topic", exact.call_args.args[0])
        self.assertEqual(session, exact.call_args.kwargs["required_agent_session"])

    def test_explicit_reply_lookup_failure_refuses_delivery(self) -> None:
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", side_effect=OSError("mailbox unavailable")):
            with self.assertRaisesRegex(SubjectInputError, "mailbox unavailable"):
                prepare_subject_and_headers(
                    "Re: Changed title", "shut_down_codex", route_profile=self.profile,
                    required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
                )

    def test_changed_explicit_reply_still_rejects_another_task_thread(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            self.profile.agent_address, "Re: [other_task] Topic", datetime.now().astimezone(),
            "<other@example.test>", recipient=self.profile.counterparty_address,
            agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", return_value=header):
            with self.assertRaisesRegex(SubjectInputError, "may not retag"):
                prepare_subject_and_headers(
                    "Re: Topic — current", "shut_down_codex", route_profile=self.profile,
                    required_agent_session=session,
                )

    def test_repeated_primary_subject_still_rejects_other_task_parent(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        header = RecentHeader(
            "agent@example.test", "[other_task] Topic", datetime.now().astimezone(),
            "<other@example.test>", recipient="human@example.test", agent_session=session,
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread", return_value=header):
            with self.assertRaisesRegex(SubjectInputError, "may not retag"):
                prepare_subject_and_headers(
                    "Topic", "shut_down_codex", route_profile=self.profile,
                    required_agent_session=session,
                )

    def test_repeated_subject_lookup_failure_does_not_start_separate_thread(self) -> None:
        with patch("omo_manager.omo_email_subject.find_recent_thread", side_effect=OSError("mailbox unavailable")):
            with self.assertRaisesRegex(SubjectInputError, "mailbox unavailable"):
                prepare_subject_and_headers(
                    "Topic", "shut_down_codex", route_profile=self.profile,
                    required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
                )

    def test_omitted_subject_rejects_retag_against_recovered_first_target(self) -> None:
        latest = RecentHeader(
            "agent@example.test",
            "Re: [wl:1] Renamed subject",
            datetime.now().astimezone(),
            "<wl1@example.test>",
            thread_target="config:24",
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", return_value=latest), self.assertRaisesRegex(SubjectInputError, "addressed to config:24; wl:1 may not retag it"):
            prepare_latest_thread_for_tmux_target("wl:1", route_profile=self.profile)


if __name__ == "__main__":
    unittest.main()
