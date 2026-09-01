from __future__ import annotations

import unittest
from collections.abc import Callable
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import override

from omo_manager.omo_email_config import AgentMailSettings
from omo_manager.omo_source1300_reply_peek import (
    ACCOUNT,
    HEADER_FETCH,
    HEADER_RESPONSE,
    HUMAN,
    MAILBOX,
    MESSAGE_FETCH,
    METADATA_FETCH,
    PARENT_MESSAGE_ID,
    LookupFailure,
    ReplyEvidence,
    _attributes,  # pyright: ignore[reportPrivateUsage]
    _body,  # pyright: ignore[reportPrivateUsage]
    _flags,  # pyright: ignore[reportPrivateUsage]
    lookup_reply,
)


def message(*, message_id: str, sender: str, recipient: str, parent: str | None = None, body: str = "Pause summaries\n", authenticated: bool = False) -> bytes:
    result = EmailMessage()
    result["Message-ID"] = message_id
    result["From"] = sender
    result["To"] = recipient
    if parent is not None:
        result["In-Reply-To"] = parent
        result["References"] = parent
    if authenticated:
        result["Return-Path"] = f"<{sender}>"
        result["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={sender}"
    result.set_content(body)
    return result.as_bytes()


PARENT_RAW = message(message_id=PARENT_MESSAGE_ID, sender=ACCOUNT, recipient=HUMAN, body="question\n")
REPLY_ID = "<source-1300-reply@gmail.com>"
REPLY_RAW = message(message_id=REPLY_ID, sender=HUMAN, recipient=ACCOUNT, parent=PARENT_MESSAGE_ID, authenticated=True)


def edit_message(raw: bytes, *, delete: tuple[str, ...] = (), replace: tuple[tuple[str, str], ...] = (), add: tuple[tuple[str, str], ...] = ()) -> bytes:
    result = BytesParser(policy=policy.default).parsebytes(raw)
    for name in delete:
        del result[name]
    for name, value in replace:
        result.replace_header(name, value)
    for name, value in add:
        result[name] = value
    return result.as_bytes()


def duplicate_header(raw: bytes, name: str, value: str) -> bytes:
    return f"{name}: {value}\n".encode() + raw


class FakeClient:
    def __init__(self) -> None:
        self.uidvalidities: list[bytes] = [b"12", b"12", b"12"]
        self.parent_searches: list[bytes] = [b"7", b"7", b"7"]
        self.thread_searches: list[bytes] = [b"7 8", b"7 8", b"7 8"]
        self.raw_by_uid: dict[str, bytes] = {"7": PARENT_RAW, "8": REPLY_RAW}
        self.metadata_by_uid: dict[str, tuple[str, str, tuple[str, ...]]] = {"7": ("100", "200", ()), "8": ("101", "200", ())}
        self.payload_n: dict[tuple[str, str], int] = {}
        self.calls: list[tuple[str, tuple[str | None, ...]]] = []
        self.selected: list[tuple[str, bool]] = []

    def login(self, address: str, _password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (address,)))
        return "OK", [b""]

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [b"IMAP4rev1 X-GM-EXT-1"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected.append((mailbox, readonly))
        return "OK", [b""]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        self.calls.append(("response", (name,)))
        return "UIDVALIDITY", [self.uidvalidities.pop(0)]

    def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
        self.calls.append((command, args))
        if command == "search" and args[1:3] == ("HEADER", "Message-ID"):
            return "OK", [self.parent_searches.pop(0)]
        if command == "search" and args[1:3] == ("X-GM-THRID", "200"):
            return "OK", [self.thread_searches.pop(0)]
        if command != "fetch" or args[0] is None:
            raise AssertionError((command, args))
        uid = args[0]
        expression = args[1]
        gmail_message_id, gmail_thread_id, flags = self.metadata_by_uid[uid]
        flags_text = " ".join(flags)
        prefix = f"{uid} (UID {uid} FLAGS ({flags_text}) X-GM-MSGID {gmail_message_id} X-GM-THRID {gmail_thread_id}"
        if expression == METADATA_FETCH:
            return "OK", [f"{prefix})".encode()]
        if expression in {HEADER_FETCH, MESSAGE_FETCH}:
            key = (uid, expression)
            n_fetch = self.payload_n.get(key, 0)
            self.payload_n[key] = n_fetch + 1
            raw = self.raw(uid, expression, n_fetch)
            payload = raw.partition(b"\n\n")[0] + b"\n\n" if expression == HEADER_FETCH else raw
            section = HEADER_RESPONSE if expression == HEADER_FETCH else "BODY[]"
            return "OK", [(f"{prefix} {section} {{{len(payload)}}}".encode(), payload), b")"]
        raise AssertionError(expression)

    def raw(self, uid: str, _expression: str, _n_fetch: int) -> bytes:
        return self.raw_by_uid[uid]

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", ()))
        return "BYE", [b""]


class Factory:
    def __init__(self, client: FakeClient) -> None:
        self.client: FakeClient = client
        self.calls: list[tuple[str, float]] = []

    def __call__(self, host: str, *, timeout: float) -> FakeClient:
        self.calls.append((host, timeout))
        return self.client


class Source1300ReplyPeekTest(unittest.TestCase):
    settings: AgentMailSettings = AgentMailSettings(ACCOUNT, "password", HUMAN)

    def lookup(self, client: FakeClient) -> ReplyEvidence | LookupFailure:
        return lookup_reply(self.settings, Factory(client))  # pyright: ignore[reportArgumentType]

    def assert_failure(self, client: FakeClient, expected: str) -> None:
        result = self.lookup(client)
        self.assertIsInstance(result, LookupFailure)
        assert isinstance(result, LookupFailure)
        self.assertIn(expected, result.reason)

    def assert_message_failure(self, expected: str, *, parent: bytes = PARENT_RAW, reply: bytes = REPLY_RAW) -> None:
        client = FakeClient()
        client.raw_by_uid = {"7": parent, "8": reply}
        self.assert_failure(client, expected)

    def test_success_returns_only_stable_identity_and_body_without_seen_mutation(self) -> None:
        client = FakeClient()
        result = self.lookup(client)
        self.assertIsInstance(result, ReplyEvidence)
        assert isinstance(result, ReplyEvidence)
        self.assertEqual(REPLY_ID, result.identity.rfc_message_id)
        self.assertEqual((ACCOUNT, MAILBOX, "12", "8", "101", "200"), tuple(result.identity.__dict__.values())[:6])
        self.assertEqual("Pause summaries\n", result.body)
        self.assertEqual([('"[Gmail]/All Mail"', True)], client.selected)
        self.assertFalse(any(call[0].lower() == "store" for call in client.calls))
        self.assertTrue(all(args[1] in {METADATA_FETCH, HEADER_FETCH, MESSAGE_FETCH} for command, args in client.calls if command == "fetch"))
        expressions = [args[1] for command, args in client.calls if command == "fetch" and isinstance(args[1], str)]
        self.assertTrue(all("BODY.PEEK" in expression for expression in expressions if "BODY" in expression))
        self.assertEqual(["8", "8"], [args[0] for command, args in client.calls if command == "fetch" and args[1] == MESSAGE_FETCH])
        self.assertEqual((), client.metadata_by_uid["8"][2])

    def test_unrelated_thread_member_never_receives_full_body_fetch(self) -> None:
        client = FakeClient()
        client.thread_searches = [b"7 8 9", b"7 8 9", b"7 8 9"]
        client.raw_by_uid["9"] = message(message_id="<newsletter@gmail.com>", sender="newsletter@example.com", recipient=ACCOUNT, body="newsletter body must not be fetched\n")
        client.metadata_by_uid["9"] = ("102", "200", ())
        result = self.lookup(client)
        self.assertIsInstance(result, ReplyEvidence)
        full_body_uids = [args[0] for command, args in client.calls if command == "fetch" and args[1] == MESSAGE_FETCH]
        self.assertEqual(["8", "8"], full_body_uids)
        self.assertNotIn(("9", MESSAGE_FETCH), client.payload_n)

    def test_rejects_wrong_configured_account_or_human(self) -> None:
        for settings in (AgentMailSettings("other@gmail.com", "password", HUMAN), AgentMailSettings(ACCOUNT, "password", "other@gmail.com")):
            with self.subTest(settings=settings):
                result = lookup_reply(settings, Factory(FakeClient()))  # pyright: ignore[reportArgumentType]
                self.assertIsInstance(result, LookupFailure)

    def test_rejects_missing_or_ambiguous_parent_and_thread(self) -> None:
        for parent, thread, expected in ((b"", b"7 8", "parent"), (b"7 9", b"7 8", "parent"), (b"7", b"7", "thread"), (b"7", b"8 9", "thread")):
            with self.subTest(parent=parent, thread=thread):
                client = FakeClient()
                client.parent_searches[0] = parent
                client.thread_searches[0] = thread
                self.assert_failure(client, expected)

    def test_rejects_multiple_human_replies(self) -> None:
        client = FakeClient()
        client.thread_searches = [b"7 8 9", b"7 8 9", b"7 8 9"]
        client.raw_by_uid["9"] = message(message_id="<second@gmail.com>", sender=HUMAN, recipient=ACCOUNT, parent=PARENT_MESSAGE_ID, authenticated=True)
        client.metadata_by_uid["9"] = ("102", "200", ())
        self.assert_failure(client, "one and only one Human reply")

    def test_rejects_parent_message_or_thread_identity_drift(self) -> None:
        cases: tuple[tuple[Callable[[FakeClient], None], str], ...] = (
            (lambda client: client.parent_searches.__setitem__(1, b"9"), "membership drifted"),
            (lambda client: client.thread_searches.__setitem__(1, b"7 9"), "membership drifted"),
            (lambda client: client.metadata_by_uid.__setitem__("8", ("101", "201", ())), "thread"),
        )
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                client = FakeClient()
                mutate(client)
                self.assert_failure(client, expected)

    def test_rejects_uidvalidity_and_flags_drift(self) -> None:
        client = FakeClient()
        client.uidvalidities[1] = b"13"
        self.assert_failure(client, "UIDVALIDITY")

        class FlagsDriftClient(FakeClient):
            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "fetch" and args == ("8", METADATA_FETCH) and self.payload_n.get(("8", MESSAGE_FETCH)) == 2:
                    self.metadata_by_uid["8"] = ("101", "200", (r"\Seen",))
                return super().uid(command, *args)

        self.assert_failure(FlagsDriftClient(), "FLAGS")

        class UnrelatedFlagsDriftClient(FakeClient):
            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "fetch" and args == ("7", METADATA_FETCH) and self.payload_n.get(("8", MESSAGE_FETCH)) == 2:
                    self.metadata_by_uid["7"] = ("100", "200", (r"\Seen",))
                return super().uid(command, *args)

        self.assert_failure(UnrelatedFlagsDriftClient(), "FLAGS")

    def test_rejects_content_envelope_parent_and_authentication_drift(self) -> None:
        variants = (
            (message(message_id=PARENT_MESSAGE_ID, sender="other@gmail.com", recipient=HUMAN), REPLY_RAW, "parent"),
            (PARENT_RAW, message(message_id=REPLY_ID, sender=HUMAN, recipient="other@gmail.com", parent=PARENT_MESSAGE_ID, authenticated=True), "recipient"),
            (PARENT_RAW, message(message_id=REPLY_ID, sender=HUMAN, recipient=ACCOUNT, parent="<other@gmail.com>", authenticated=True), "direct reply"),
            (PARENT_RAW, message(message_id=REPLY_ID, sender=HUMAN, recipient=ACCOUNT, parent=PARENT_MESSAGE_ID), "transport-authenticated"),
        )
        for parent_raw, reply_raw, expected in variants:
            with self.subTest(expected=expected):
                client = FakeClient()
                client.raw_by_uid = {"7": parent_raw, "8": reply_raw}
                self.assert_failure(client, expected)

        class ContentDriftClient(FakeClient):
            @override
            def raw(self, uid: str, expression: str, n_fetch: int) -> bytes:
                if uid == "8" and expression == MESSAGE_FETCH and n_fetch == 1:
                    return message(message_id=REPLY_ID, sender=HUMAN, recipient=ACCOUNT, parent=PARENT_MESSAGE_ID, body="Keep summaries\n", authenticated=True)
                return super().raw(uid, expression, n_fetch)

        self.assert_failure(ContentDriftClient(), "content or envelope drifted")

    def test_rejects_malformed_or_ambiguous_search_and_fetch(self) -> None:
        client = FakeClient()
        client.parent_searches[0] = b"7 x"
        self.assert_failure(client, "malformed")

        class FetchFailureClient(FakeClient):
            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "fetch" and args == ("7", METADATA_FETCH):
                    return "NO", []
                return super().uid(command, *args)

        self.assert_failure(FetchFailureClient(), "fetch failed")

        class SearchFailureClient(FakeClient):
            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "search":
                    return "NO", []
                return super().uid(command, *args)

        self.assert_failure(SearchFailureClient(), "search failed")
        client = FakeClient()
        client.parent_searches[0] = b"7 7"
        self.assert_failure(client, "duplicate UIDs")

        class IdentityFailureClient(FakeClient):
            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                typ, data = super().uid(command, *args)
                if command == "fetch" and args == ("7", METADATA_FETCH) and data and isinstance(data[0], bytes):
                    data[0] = data[0].replace(b"UID 7", b"UID 9", 1)
                return typ, data

        self.assert_failure(IdentityFailureClient(), "identity is malformed or drifted")

    def test_rejects_parent_header_and_full_reply_observation_drift(self) -> None:
        class ParentMetadataDriftClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.parent_metadata_n: int = 0

            @override
            def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if command == "fetch" and args == ("7", METADATA_FETCH):
                    self.parent_metadata_n += 1
                    if self.parent_metadata_n == 2:
                        self.metadata_by_uid["7"] = ("999", "200", ())
                return super().uid(command, *args)

        self.assert_failure(ParentMetadataDriftClient(), "parent Gmail identity drifted")

        class HeaderDriftClient(FakeClient):
            @override
            def raw(self, uid: str, expression: str, n_fetch: int) -> bytes:
                if uid == "7" and expression == HEADER_FETCH and n_fetch == 1:
                    return edit_message(PARENT_RAW, replace=(("To", "other@gmail.com"),))
                return super().raw(uid, expression, n_fetch)

        self.assert_failure(HeaderDriftClient(), "header or envelope drifted")

        class HeaderToFullDriftClient(FakeClient):
            @override
            def raw(self, uid: str, expression: str, n_fetch: int) -> bytes:
                if uid == "8" and expression == MESSAGE_FETCH:
                    return edit_message(REPLY_RAW, add=(("Reply-To", HUMAN),))
                return super().raw(uid, expression, n_fetch)

        self.assert_failure(HeaderToFullDriftClient(), "reply content or envelope drifted")

    def test_rejects_connection_capability_select_and_uidvalidity_boundaries(self) -> None:
        class ConnectionFailureFactory:
            def __call__(self, _host: str, *, timeout: float) -> FakeClient:
                raise OSError(f"connection failed after {timeout:g}s")

        self.assertIsInstance(lookup_reply(self.settings, ConnectionFailureFactory()), LookupFailure)  # pyright: ignore[reportArgumentType]

        class LoginFailureClient(FakeClient):
            @override
            def login(self, address: str, password: str) -> tuple[str, list[bytes]]:
                _ = (address, password)
                return "NO", []

        self.assert_failure(LoginFailureClient(), "login")

        class CapabilityFailureClient(FakeClient):
            @override
            def capability(self) -> tuple[str, list[bytes]]:
                return "NO", []

        self.assert_failure(CapabilityFailureClient(), "Gmail identity support")

        class SelectFailureClient(FakeClient):
            @override
            def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                _ = (mailbox, readonly)
                return "NO", []

        self.assert_failure(SelectFailureClient(), "select failed")
        for value in (b"", b"abc"):
            with self.subTest(uidvalidity=value):
                client = FakeClient()
                client.uidvalidities[0] = value
                self.assert_failure(client, "UIDVALIDITY")

    def test_rejects_every_transport_authentication_boundary(self) -> None:
        variants = (
            edit_message(REPLY_RAW, delete=("Return-Path",)),
            edit_message(REPLY_RAW, replace=(("Return-Path", "<other@gmail.com>"),)),
            duplicate_header(REPLY_RAW, "Return-Path", f"<{HUMAN}>"),
            edit_message(REPLY_RAW, delete=("Authentication-Results",)),
            edit_message(REPLY_RAW, replace=(("Authentication-Results", f"other.example; spf=pass smtp.mailfrom={HUMAN}"),)),
            edit_message(REPLY_RAW, replace=(("Authentication-Results", f"mx.google.com; spf=fail smtp.mailfrom={HUMAN}"),)),
            edit_message(REPLY_RAW, replace=(("Authentication-Results", "mx.google.com; spf=pass smtp.mailfrom=other@gmail.com"),)),
            duplicate_header(REPLY_RAW, "Authentication-Results", f"mx.google.com; spf=pass smtp.mailfrom={HUMAN}"),
            edit_message(REPLY_RAW, add=(("Sender", "other@gmail.com"),)),
            duplicate_header(edit_message(REPLY_RAW, add=(("Sender", HUMAN),)), "Sender", HUMAN),
            duplicate_header(REPLY_RAW, "From", "other@gmail.com"),
        )
        for index, reply in enumerate(variants):
            with self.subTest(case=index):
                self.assert_message_failure("transport-authenticated", reply=reply)

    def test_rejects_every_direct_reply_envelope_boundary(self) -> None:
        variants = (
            (edit_message(REPLY_RAW, replace=(("To", "other@gmail.com"),)), "recipient envelope"),
            (duplicate_header(REPLY_RAW, "To", ACCOUNT), "recipient envelope"),
            (edit_message(REPLY_RAW, add=(("Cc", "other@gmail.com"),)), "direct Human-to-account"),
            (edit_message(REPLY_RAW, add=(("Bcc", "other@gmail.com"),)), "direct Human-to-account"),
            (edit_message(REPLY_RAW, add=(("Resent-From", HUMAN),)), "direct Human-to-account"),
            (edit_message(REPLY_RAW, add=(("Resent-Sender", HUMAN),)), "direct Human-to-account"),
            (edit_message(REPLY_RAW, add=(("Resent-To", ACCOUNT),)), "direct Human-to-account"),
            (edit_message(REPLY_RAW, add=(("Reply-To", "other@gmail.com"),)), "reply address"),
            (duplicate_header(edit_message(REPLY_RAW, add=(("Reply-To", HUMAN),)), "Reply-To", HUMAN), "reply address"),
        )
        for reply, expected in variants:
            with self.subTest(expected=expected):
                self.assert_message_failure(expected, reply=reply)

    def test_rejects_every_direct_parent_envelope_boundary(self) -> None:
        variants = (
            edit_message(PARENT_RAW, add=(("Sender", ACCOUNT),)),
            edit_message(PARENT_RAW, add=(("Cc", HUMAN),)),
            edit_message(PARENT_RAW, add=(("Bcc", HUMAN),)),
            edit_message(PARENT_RAW, add=(("Resent-From", ACCOUNT),)),
            edit_message(PARENT_RAW, add=(("Resent-Sender", ACCOUNT),)),
            edit_message(PARENT_RAW, add=(("Resent-To", HUMAN),)),
            edit_message(PARENT_RAW, add=(("Reply-To", "other@gmail.com"),)),
            duplicate_header(PARENT_RAW, "From", ACCOUNT),
            duplicate_header(PARENT_RAW, "To", HUMAN),
        )
        for index, parent in enumerate(variants):
            with self.subTest(case=index):
                self.assert_message_failure("parent", parent=parent)

    def test_rejects_every_reply_thread_header_boundary(self) -> None:
        variants = (
            (edit_message(REPLY_RAW, delete=("In-Reply-To",)), "In-Reply-To"),
            (duplicate_header(REPLY_RAW, "In-Reply-To", PARENT_MESSAGE_ID), "In-Reply-To"),
            (edit_message(REPLY_RAW, replace=(("In-Reply-To", "<other@gmail.com>"),)), "direct reply"),
            (edit_message(REPLY_RAW, delete=("References",)), "References"),
            (duplicate_header(REPLY_RAW, "References", PARENT_MESSAGE_ID), "References"),
            (edit_message(REPLY_RAW, replace=(("References", "not-a-message-id"),)), "References"),
            (edit_message(REPLY_RAW, replace=(("References", f"{PARENT_MESSAGE_ID} <other@gmail.com>"),)), "References"),
            (edit_message(REPLY_RAW, delete=("Message-ID",)), "Message-ID"),
            (edit_message(REPLY_RAW, replace=(("Message-ID", "invalid"),)), "invalid Message-ID"),
            (duplicate_header(REPLY_RAW, "Message-ID", REPLY_ID), "Message-ID"),
        )
        for reply, expected in variants:
            with self.subTest(expected=expected):
                self.assert_message_failure(expected, reply=reply)

    def test_rejects_missing_human_and_duplicate_provider_or_rfc_identity(self) -> None:
        self.assert_message_failure("one and only one Human reply", reply=message(message_id=REPLY_ID, sender="other@gmail.com", recipient=ACCOUNT, parent=PARENT_MESSAGE_ID))
        client = FakeClient()
        client.metadata_by_uid["8"] = ("100", "200", ())
        self.assert_failure(client, "provider message identities")
        client = FakeClient()
        client.raw_by_uid["8"] = edit_message(REPLY_RAW, replace=(("Message-ID", PARENT_MESSAGE_ID),))
        self.assert_failure(client, "ambiguous RFC Message-IDs")

    def test_rejects_empty_ambiguous_unavailable_and_invalid_body(self) -> None:
        self.assert_message_failure("empty", reply=message(message_id=REPLY_ID, sender=HUMAN, recipient=ACCOUNT, parent=PARENT_MESSAGE_ID, body=" \n", authenticated=True))
        multipart = EmailMessage()
        multipart["Message-ID"] = REPLY_ID
        multipart["From"] = HUMAN
        multipart["To"] = ACCOUNT
        multipart["In-Reply-To"] = PARENT_MESSAGE_ID
        multipart["References"] = PARENT_MESSAGE_ID
        multipart["Return-Path"] = f"<{HUMAN}>"
        multipart["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={HUMAN}"
        multipart.set_content("Pause summaries")
        multipart.add_alternative("Keep summaries", subtype="plain")
        self.assert_message_failure("unambiguous", reply=multipart.as_bytes())
        invalid_charset = REPLY_RAW.replace(b'charset="utf-8"', b'charset="x-invalid"')
        self.assert_message_failure("encoding", reply=invalid_charset)
        unavailable = EmailMessage()
        unavailable.set_type("text/plain")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            _ = _body(unavailable)

    def test_rejects_low_level_flags_and_fetch_shapes(self) -> None:
        for value, expected in ((None, "malformed"), ("bad", "malformed"), (r"(\Seen \Seen)", "ambiguous")):
            with self.subTest(flags=value):
                with self.assertRaisesRegex(RuntimeError, expected):
                    _ = _flags(value)
        raw = b"Message-ID: <x@gmail.com>\n\n"
        valid_prefix = b"1 (UID 1 FLAGS () X-GM-MSGID 10 X-GM-THRID 20 BODY[] {27}"
        cases: tuple[tuple[list[bytes | tuple[bytes, bytes]], str | None, str], ...] = (
            ([], "BODY[]", "ambiguous"),
            ([(valid_prefix, raw), (valid_prefix, raw), b")"], "BODY[]", "ambiguous"),
            ([(valid_prefix, raw)], "BODY[]", "unassociated"),
            ([(valid_prefix, raw), b"extra"], "BODY[]", "unassociated"),
            ([(b"1 (UID 1 FLAGS () X-GM-MSGID 10 X-GM-THRID 20 BODY[] {1}", raw), b")"], "BODY[]", "length"),
            ([(valid_prefix, raw), b")"], HEADER_RESPONSE, "length"),
            ([], None, "ambiguous"),
            ([(b"1 (UID 1 FLAGS () X-GM-MSGID 10 X-GM-THRID 20)", raw)], None, "ambiguous"),
            ([b"1 (UID 1 FLAGS () X-GM-MSGID 10)"], None, "incomplete"),
            ([b"1 (UID 1 UID 1 FLAGS () X-GM-MSGID 10 X-GM-THRID 20)"], None, "incomplete"),
        )
        for data, response_section, expected in cases:
            with self.subTest(expected=expected, response_section=response_section):
                with self.assertRaisesRegex(RuntimeError, expected):
                    _ = _attributes(data, response_section=response_section)


if __name__ == "__main__":
    _ = unittest.main()
