#!/usr/bin/env python3
"""Read one exact authenticated Source-1300 Human reply without changing flags."""

from __future__ import annotations

import hashlib
import imaplib
import re
import sys
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Literal, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_email_config import GMAIL_IMAP_HOST, AgentMailSettings, configured_agent_mail
from omo_manager.omo_manager_mail_compress import imap_fetch_attributes, imap_quoted, imap_response_text, imap_uid, logout_mailbox, select_mailbox, selected_uidvalidity

ACCOUNT = "sichangheagent@gmail.com"
HUMAN = "stevensichanghe@gmail.com"
MAILBOX = "[Gmail]/All Mail"
PARENT_MESSAGE_ID = "<178825768376.3726979.9266916304748627918@gmail.com>"
METADATA_FETCH = "(UID FLAGS X-GM-MSGID X-GM-THRID)"
HEADER_FETCH = "(UID FLAGS X-GM-MSGID X-GM-THRID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM SENDER RETURN-PATH AUTHENTICATION-RESULTS REPLY-TO TO CC BCC RESENT-FROM RESENT-SENDER RESENT-TO)])"
HEADER_RESPONSE = "BODY[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM SENDER RETURN-PATH AUTHENTICATION-RESULTS REPLY-TO TO CC BCC RESENT-FROM RESENT-SENDER RESENT-TO)]"
# 🧑 "Should not happen. ... leave all the other ones as is"
MESSAGE_FETCH = "(UID FLAGS X-GM-MSGID X-GM-THRID BODY.PEEK[])"
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class ReplyIdentity:
    account: str
    mailbox: str
    uidvalidity: str
    uid: str
    gmail_message_id: str
    gmail_thread_id: str
    rfc_message_id: str


@dataclass(frozen=True)
class ReplyEvidence:
    identity: ReplyIdentity
    body: str


@dataclass(frozen=True)
class LookupFailure:
    reason: str


@dataclass(frozen=True)
class _Record:
    uid: str
    gmail_message_id: str
    gmail_thread_id: str
    flags: tuple[str, ...]
    raw: bytes | None = None


class _ClientFactory(Protocol):
    def __call__(self, host: str, *, timeout: float) -> imaplib.IMAP4_SSL: ...


class _Rejected(RuntimeError):
    pass


def _addresses(message: Message, name: str) -> tuple[str, ...]:
    headers = message.get_all(name, [])
    if len(headers) != 1:
        return ()
    values = [address.casefold() for _display, address in getaddresses([str(headers[0])]) if address]
    return tuple(values)


def _mentions_address(message: Message, name: str, address: str) -> bool:
    expected = address.casefold()
    return any(candidate.casefold() == expected for _display, candidate in getaddresses([str(value) for value in message.get_all(name, [])]) if candidate)


def _authenticated_human_sender(message: Message, human_address: str) -> bool:
    human = human_address.casefold()
    if _addresses(message, "From") != (human,):
        return False
    sender_headers = message.get_all("Sender", [])
    if len(sender_headers) > 1 or (sender_headers and _addresses(message, "Sender") != (human,)):
        return False
    if _addresses(message, "Return-Path") != (human,):
        return False
    authentication = message.get_all("Authentication-Results", [])
    if len(authentication) != 1:
        return False
    segments = [" ".join(segment.casefold().split()) for segment in str(authentication[0]).split(";")]
    escaped_human = re.escape(human)
    return segments[0] == "mx.google.com" and any(
        re.search(r"(?:^|\s)spf=pass(?:\s|$)", segment) is not None and re.search(rf"(?:^|\s)smtp\.mailfrom={escaped_human}(?:\s|$)", segment) is not None for segment in segments[1:]
    )


def _exact_header(message: Message, name: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1:
        raise _Rejected(f"message requires one exact {name} header")
    return " ".join(str(values[0]).split())


def _message_id(message: Message) -> str:
    value = _exact_header(message, "Message-ID")
    if MESSAGE_ID_RE.fullmatch(value) is None:
        raise _Rejected("message has an invalid Message-ID")
    return value


def _flags(value: str | None) -> tuple[str, ...]:
    if value is None or not value.startswith("(") or not value.endswith(")"):
        raise _Rejected("Gmail FLAGS metadata is missing or malformed")
    values = tuple(value[1:-1].split())
    if len(values) != len(set(values)):
        raise _Rejected("Gmail FLAGS metadata is ambiguous")
    return values


def _attributes(data: list[bytes | tuple[bytes, bytes]], *, response_section: str | None) -> tuple[dict[str, str], bytes | None]:
    if response_section is not None:
        records = [item for item in data if isinstance(item, tuple)]
        if len(records) != 1 or len(records[0]) != 2:
            raise _Rejected("IMAP BODY.PEEK returned an ambiguous record")
        trailers = [item for item in data if isinstance(item, bytes)]
        if len(data) != 2 or trailers != [b")"]:
            raise _Rejected("IMAP BODY.PEEK returned an unassociated record")
        raw = records[0][1]
        response = records[0][0].decode("ascii", errors="strict")
        section = response.find(f" {response_section} ")
        size = re.search(r"\{(\d+)\}\Z", response)
        if section < 0 or size is None or response[section + 1 : size.start()].strip() != response_section or int(size.group(1)) != len(raw):
            raise _Rejected("IMAP BODY.PEEK length does not match its exact message")
        text = f"{response[:section]})"
    else:
        if len(data) != 1 or not isinstance(data[0], bytes):
            raise _Rejected("IMAP metadata fetch returned an ambiguous record")
        raw = None
        text = data[0].decode("ascii", errors="strict")
    attributes = imap_fetch_attributes(text, reject_duplicate_keys=frozenset({"UID", "FLAGS", "X-GM-MSGID", "X-GM-THRID"}))
    if set(("UID", "FLAGS", "X-GM-MSGID", "X-GM-THRID")) - attributes.keys():
        raise _Rejected("Gmail identity metadata is incomplete or ambiguous")
    return attributes, raw


def _fetch(client: imaplib.IMAP4_SSL, uid: str, mode: Literal["metadata", "header", "message"]) -> _Record:
    expression = {"metadata": METADATA_FETCH, "header": HEADER_FETCH, "message": MESSAGE_FETCH}[mode]
    typ, data = imap_uid(client, f"source-1300 fetch uid={uid}", "fetch", uid, expression)
    if typ != "OK":
        raise _Rejected(f"IMAP fetch failed for UID {uid}")
    response_section = {"metadata": None, "header": HEADER_RESPONSE, "message": "BODY[]"}[mode]
    attributes, raw = _attributes(data, response_section=response_section)
    observed_uid = attributes["UID"]
    gmail_message_id = attributes["X-GM-MSGID"]
    gmail_thread_id = attributes["X-GM-THRID"]
    if observed_uid != uid or not all(value.isdecimal() for value in (observed_uid, gmail_message_id, gmail_thread_id)):
        raise _Rejected("Gmail object identity is malformed or drifted")
    return _Record(uid, gmail_message_id, gmail_thread_id, _flags(attributes["FLAGS"]), raw)


def _search(client: imaplib.IMAP4_SSL, *criteria: str) -> tuple[str, ...]:
    typ, data = imap_uid(client, "source-1300 search", "search", None, *criteria)
    if typ != "OK" or len(data) != 1 or not isinstance(data[0], bytes):
        raise _Rejected("IMAP search failed or returned an ambiguous response")
    values = tuple(value.decode("ascii") for value in data[0].split())
    if any(not value.isdecimal() for value in values) or len(values) != len(set(values)):
        raise _Rejected("IMAP search returned malformed or duplicate UIDs")
    return values


def _body(message: Message) -> str:
    parts = [part for part in message.walk() if not part.is_multipart() and not part.get_filename() and part.get_content_type() == "text/plain"]
    if len(parts) != 1:
        raise _Rejected("Human reply requires one unambiguous text/plain body")
    payload = parts[0].get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise _Rejected("Human reply body is unavailable")
    try:
        text = payload.decode(parts[0].get_content_charset() or "utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (LookupError, UnicodeDecodeError) as exc:
        raise _Rejected("Human reply body encoding is invalid") from exc
    if not text.strip():
        raise _Rejected("Human reply body is empty")
    return text


def _validate_parent(message: Message, settings: AgentMailSettings) -> None:
    reply_to = message.get_all("Reply-To", [])
    if (
        _message_id(message) != PARENT_MESSAGE_ID
        or _addresses(message, "From") != (settings.agent_address.casefold(),)
        or _addresses(message, "To") != (settings.human_address.casefold(),)
        or message.get_all("Sender", [])
        or message.get_all("Cc", [])
        or message.get_all("Bcc", [])
        or any(message.get_all(name, []) for name in ("Resent-From", "Resent-Sender", "Resent-To"))
        or (reply_to and _addresses(message, "Reply-To") != (settings.agent_address.casefold(),))
    ):
        raise _Rejected("parent message identity or direct envelope drifted")


def _validate_reply(message: Message, settings: AgentMailSettings) -> tuple[str, str]:
    return _reply_headers(message, settings), _body(message)


def _reply_headers(message: Message, settings: AgentMailSettings) -> str:
    if not _authenticated_human_sender(message, settings.human_address):
        raise _Rejected("reply is not transport-authenticated as the exact Human sender")
    if _addresses(message, "To") != (settings.agent_address.casefold(),):
        raise _Rejected("reply recipient envelope does not match the authenticated account")
    if message.get_all("Cc", []) or message.get_all("Bcc", []) or any(message.get_all(name, []) for name in ("Resent-From", "Resent-Sender", "Resent-To")):
        raise _Rejected("reply is not a direct Human-to-account envelope")
    reply_to = message.get_all("Reply-To", [])
    if reply_to and _addresses(message, "Reply-To") != (settings.human_address.casefold(),):
        raise _Rejected("reply address escapes the exact Human sender envelope")
    if _exact_header(message, "In-Reply-To") != PARENT_MESSAGE_ID:
        raise _Rejected("Human message is not a direct reply to the exact parent")
    references = _exact_header(message, "References").split()
    if not references or references[-1] != PARENT_MESSAGE_ID or any(MESSAGE_ID_RE.fullmatch(value) is None for value in references):
        raise _Rejected("reply References does not bind the exact parent")
    return _message_id(message)


def _header_binding(message: Message) -> tuple[tuple[str, tuple[str, ...]], ...]:
    names = ("Message-ID", "In-Reply-To", "References", "From", "Sender", "Return-Path", "Authentication-Results", "Reply-To", "To", "Cc", "Bcc", "Resent-From", "Resent-Sender", "Resent-To")
    return tuple((name, tuple(" ".join(str(value).split()) for value in message.get_all(name, []))) for name in names)


def _lookup(client: imaplib.IMAP4_SSL, settings: AgentMailSettings) -> ReplyEvidence:
    select_mailbox(client, MAILBOX, readonly=True)
    uidvalidity_before = selected_uidvalidity(client)
    parent_uids_before = _search(client, "HEADER", "Message-ID", imap_quoted(PARENT_MESSAGE_ID))
    if len(parent_uids_before) != 1:
        raise _Rejected("exact parent Message-ID is missing or ambiguous")
    parent_metadata = _fetch(client, parent_uids_before[0], "metadata")
    thread_uids_before = _search(client, "X-GM-THRID", parent_metadata.gmail_thread_id)
    if parent_metadata.uid not in thread_uids_before or len(thread_uids_before) < 2:
        raise _Rejected("Gmail thread is missing or does not contain a reply")
    metadata_before = tuple(_fetch(client, uid, "metadata") for uid in thread_uids_before)
    if parent_metadata != next((record for record in metadata_before if record.uid == parent_metadata.uid), None):
        raise _Rejected("exact parent Gmail identity drifted before BODY.PEEK")
    first_headers = tuple(_fetch(client, uid, "header") for uid in thread_uids_before)
    second_headers = tuple(_fetch(client, uid, "header") for uid in thread_uids_before)
    metadata_after_headers = tuple(_fetch(client, uid, "metadata") for uid in thread_uids_before)
    parent_uids_after_headers = _search(client, "HEADER", "Message-ID", imap_quoted(PARENT_MESSAGE_ID))
    thread_uids_after_headers = _search(client, "X-GM-THRID", parent_metadata.gmail_thread_id)
    uidvalidity_after_headers = selected_uidvalidity(client)
    identities_before = tuple((record.uid, record.gmail_message_id, record.gmail_thread_id, record.flags) for record in metadata_before)
    identities_first_headers = tuple((record.uid, record.gmail_message_id, record.gmail_thread_id, record.flags) for record in first_headers)
    identities_second_headers = tuple((record.uid, record.gmail_message_id, record.gmail_thread_id, record.flags) for record in second_headers)
    identities_after_headers = tuple((record.uid, record.gmail_message_id, record.gmail_thread_id, record.flags) for record in metadata_after_headers)
    if uidvalidity_before != uidvalidity_after_headers:
        raise _Rejected("Gmail UIDVALIDITY drifted during header lookup")
    if parent_uids_before != parent_uids_after_headers or thread_uids_before != thread_uids_after_headers:
        raise _Rejected("parent or Gmail thread membership drifted during header lookup")
    if not identities_before == identities_first_headers == identities_second_headers == identities_after_headers:
        raise _Rejected("Gmail message, thread, or FLAGS identity drifted during header BODY.PEEK")
    if any(record.gmail_thread_id != parent_metadata.gmail_thread_id for record in second_headers):
        raise _Rejected("fetched message escaped the exact Gmail thread")
    if len({record.gmail_message_id for record in second_headers}) != len(second_headers):
        raise _Rejected("Gmail thread contains ambiguous provider message identities")
    if tuple(hashlib.sha256(record.raw or b"").digest() for record in first_headers) != tuple(hashlib.sha256(record.raw or b"").digest() for record in second_headers):
        raise _Rejected("message header or envelope drifted during lookup")
    header_messages = [BytesParser(policy=policy.default).parsebytes(record.raw or b"") for record in second_headers]
    message_ids = tuple(_message_id(message) for message in header_messages)
    if len(message_ids) != len(set(message_ids)):
        raise _Rejected("Gmail thread contains ambiguous RFC Message-IDs")
    parents = [(record, message) for record, message in zip(second_headers, header_messages, strict=True) if _message_id(message) == PARENT_MESSAGE_ID]
    if len(parents) != 1 or parents[0][0].uid != parent_metadata.uid:
        raise _Rejected("exact parent message identity drifted inside the Gmail thread")
    _validate_parent(parents[0][1], settings)
    human_headers = [(record, message) for record, message in zip(second_headers, header_messages, strict=True) if _mentions_address(message, "From", settings.human_address)]
    if len(human_headers) != 1:
        raise _Rejected("exact Gmail thread requires one and only one Human reply")
    reply_header_record, reply_header_message = human_headers[0]
    reply_message_id = _reply_headers(reply_header_message, settings)
    reply_metadata_before = _fetch(client, reply_header_record.uid, "metadata")
    first_reply = _fetch(client, reply_header_record.uid, "message")
    second_reply = _fetch(client, reply_header_record.uid, "message")
    metadata_after_reply = tuple(_fetch(client, uid, "metadata") for uid in thread_uids_before)
    parent_uids_after_reply = _search(client, "HEADER", "Message-ID", imap_quoted(PARENT_MESSAGE_ID))
    thread_uids_after_reply = _search(client, "X-GM-THRID", parent_metadata.gmail_thread_id)
    uidvalidity_after_reply = selected_uidvalidity(client)
    reply_identity = (reply_header_record.uid, reply_header_record.gmail_message_id, reply_header_record.gmail_thread_id, reply_header_record.flags)
    reply_metadata_before_identity = (reply_metadata_before.uid, reply_metadata_before.gmail_message_id, reply_metadata_before.gmail_thread_id, reply_metadata_before.flags)
    first_reply_identity = (first_reply.uid, first_reply.gmail_message_id, first_reply.gmail_thread_id, first_reply.flags)
    second_reply_identity = (second_reply.uid, second_reply.gmail_message_id, second_reply.gmail_thread_id, second_reply.flags)
    reply_metadata_after = next((record for record in metadata_after_reply if record.uid == reply_header_record.uid), None)
    if reply_metadata_after is None:
        raise _Rejected("reply Gmail identity disappeared after BODY.PEEK")
    reply_metadata_after_identity = (reply_metadata_after.uid, reply_metadata_after.gmail_message_id, reply_metadata_after.gmail_thread_id, reply_metadata_after.flags)
    identities_after_reply = tuple((record.uid, record.gmail_message_id, record.gmail_thread_id, record.flags) for record in metadata_after_reply)
    if uidvalidity_before != uidvalidity_after_reply:
        raise _Rejected("Gmail UIDVALIDITY drifted during reply lookup")
    if parent_uids_before != parent_uids_after_reply or thread_uids_before != thread_uids_after_reply:
        raise _Rejected("parent or Gmail thread membership drifted during reply lookup")
    if identities_before != identities_after_reply or not reply_identity == reply_metadata_before_identity == first_reply_identity == second_reply_identity == reply_metadata_after_identity:
        raise _Rejected("Gmail message, thread, or FLAGS identity drifted during reply BODY.PEEK")
    first_reply_message = BytesParser(policy=policy.default).parsebytes(first_reply.raw or b"")
    second_reply_message = BytesParser(policy=policy.default).parsebytes(second_reply.raw or b"")
    if (
        first_reply.raw != second_reply.raw
        or _header_binding(first_reply_message) != _header_binding(reply_header_message)
        or _header_binding(second_reply_message) != _header_binding(reply_header_message)
    ):
        raise _Rejected("reply content or envelope drifted during lookup")
    observed_message_id, body = _validate_reply(second_reply_message, settings)
    if observed_message_id != reply_message_id:
        raise _Rejected("reply Message-ID drifted during lookup")
    return ReplyEvidence(
        ReplyIdentity(settings.agent_address, MAILBOX, uidvalidity_before, reply_header_record.uid, reply_header_record.gmail_message_id, reply_header_record.gmail_thread_id, reply_message_id),
        body,
    )


def lookup_reply(settings: AgentMailSettings, client_factory: _ClientFactory = imaplib.IMAP4_SSL) -> ReplyEvidence | LookupFailure:
    """Return stable reply evidence or one fail-closed reason."""
    if settings.agent_address.casefold() != ACCOUNT or settings.human_address.casefold() != HUMAN:
        return LookupFailure("configured account or Human identity does not match Source-1300")
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = client_factory(GMAIL_IMAP_HOST, timeout=30)
        typ, _data = client.login(settings.agent_address, settings.app_password)
        if typ != "OK":
            raise _Rejected("authenticated Gmail login failed")
        typ, data = client.capability()
        capabilities = imap_response_text(data).upper().split() if typ == "OK" else []
        if "X-GM-EXT-1" not in capabilities:
            raise _Rejected("authenticated mailbox does not advertise Gmail identity support")
        return _lookup(client, settings)
    except (_Rejected, OSError, RuntimeError, UnicodeDecodeError, imaplib.IMAP4.error) as exc:
        return LookupFailure(str(exc))
    finally:
        if client is not None:
            try:
                logout_mailbox(client)
            except (OSError, RuntimeError, imaplib.IMAP4.error):
                pass


def main() -> int:
    try:
        settings = configured_agent_mail()
    except ValueError as exc:
        print(f"omo_source1300_reply_peek.py: {exc}", file=sys.stderr)
        return 2
    if settings is None:
        print("omo_source1300_reply_peek.py: dedicated agent Gmail is not configured", file=sys.stderr)
        return 2
    result = lookup_reply(settings)
    if isinstance(result, LookupFailure):
        print(f"omo_source1300_reply_peek.py: {result.reason}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
