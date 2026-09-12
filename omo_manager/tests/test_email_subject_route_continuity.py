from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from omo_manager.omo_email_subject import (
    MailRouteProfile,
    RecentHeader,
    SubjectInputError,
    authenticated_referenced_thread_target,
    find_recent_thread_matching,
    prepare_latest_thread_for_tmux_target,
    prepare_subject_and_headers,
    select_recent_thread,
)


class EmailSubjectRouteContinuityTests(unittest.TestCase):
    profile = MailRouteProfile("agent@example.test", "human@example.test", "primary")

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
                    subject, headers = prepare_subject_and_headers(
                        "Re: Investigate config agent task file problems", "config:24.0", route_profile=self.profile
                    )

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
            subject, _headers = prepare_subject_and_headers(
                "Re: Investigate config agent task file problems", "config:24.0", route_profile=self.profile
            )
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
            with self.subTest(error=error), patch(
                "omo_manager.omo_email_subject.fetch_recent_headers", return_value=headers
            ), self.assertRaisesRegex(SubjectInputError, error):
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

    def test_omitted_subject_rejects_retag_against_recovered_first_target(self) -> None:
        latest = RecentHeader(
            "agent@example.test",
            "Re: [wl:1] Renamed subject",
            datetime.now().astimezone(),
            "<wl1@example.test>",
            thread_target="config:24",
        )
        with patch("omo_manager.omo_email_subject.find_recent_thread_for_tmux_target", return_value=latest), self.assertRaisesRegex(
            SubjectInputError, "addressed to config:24; wl:1 may not retag it"
        ):
            prepare_latest_thread_for_tmux_target("wl:1", route_profile=self.profile)


if __name__ == "__main__":
    unittest.main()
