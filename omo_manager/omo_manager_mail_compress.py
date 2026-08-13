#!/usr/bin/env python3
"""Snapshot/export manager mail and trash explicit superseded UIDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import imaplib
import os
import re
import socket
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.email_idle_watcher import LEGACY_MANAGER_SUBJECT_TOKENS, is_mail_cleanup_excluded_subject, message_text, parse_env_config
from omo_manager.omo_email_config import configured_agent_mail, human_config_path

HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"
FULL_FETCH = "(BODY.PEEK[])"
GMAIL_METADATA_FETCH = "(FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS)"
TRASH_MAILBOX = "[Gmail]/Trash"
CONFIG_PATH = human_config_path()
DEFAULT_THREADS_PER_BATCH = 10
EXPORT_FULL_FETCH_ATTEMPTS = 2
IMAP_OPERATION_TIMEOUT_S = 45.0


class ImapOperationError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True)
class MailRecord:
    uid: str
    date: str
    sender: str
    to: str
    subject: str
    msgid_sha256: str
    body: str = ""
    gmail_msgid: str = ""
    gmail_thrid: str = ""
    flags: str = ""
    labels: str = ""
    raw_sha256: str = ""
    source_uidvalidity: str = ""

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode())


@dataclass(frozen=True)
class GmailMetadata:
    gmail_msgid: str
    gmail_thrid: str
    flags: str
    labels: str


@dataclass(frozen=True)
class PostMoveVerification:
    same_mailbox: bool = False
    verified_message_count: int = 0
    verified_thread_count: int = 0
    changed_thread_count: int = 0
    imap_failure_count: int = 0
    verified_records: tuple[MailRecord, ...] = ()

    @property
    def complete(self) -> bool:
        return self.same_mailbox and not (self.changed_thread_count or self.imap_failure_count)


def imap_operation[T](client: imaplib.IMAP4_SSL, stage: str, operation: Callable[[], T]) -> T:
    """Run one IMAP operation with an absolute deadline and abort its socket on expiry."""
    if getattr(client, "_omo_operation_timed_out", False):
        raise ImapOperationError(stage, f"IMAP client is unusable after timeout: stage={stage}")
    results: list[T] = []
    failures: list[BaseException] = []

    def abort() -> None:
        setattr(client, "_omo_operation_timed_out", True)
        sock = getattr(client, "sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def run() -> None:
        try:
            results.append(operation())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, name=f"imap:{stage}", daemon=True)
    worker.start()
    worker.join(IMAP_OPERATION_TIMEOUT_S)
    if worker.is_alive():
        abort()
        raise ImapOperationError(stage, f"IMAP operation timed out: stage={stage} timeout_s={IMAP_OPERATION_TIMEOUT_S:g}")
    if failures:
        exc = failures[0]
        if isinstance(exc, (EOFError, OSError, imaplib.IMAP4.error)):
            raise ImapOperationError(stage, f"IMAP operation failed: stage={stage} error={exc}") from exc
        raise exc
    return results[0]


def imap_uid(client: imaplib.IMAP4_SSL, stage: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
    return imap_operation(client, stage, lambda: client.uid(*args))  # pyright: ignore[reportArgumentType, reportReturnType]


def logout_mailbox(client: imaplib.IMAP4_SSL) -> None:
    if getattr(client, "_omo_operation_timed_out", False):
        return
    imap_operation(client, "logout", client.logout)


def connect_mailbox(host: str) -> imaplib.IMAP4_SSL:
    """Connect with an absolute deadline and close any client created after expiry."""
    clients: list[imaplib.IMAP4_SSL] = []
    failures: list[BaseException] = []
    completed = False
    expired = False
    state_changed = threading.Condition()

    def run() -> None:
        nonlocal completed, expired
        try:
            client = imaplib.IMAP4_SSL(host, timeout=IMAP_OPERATION_TIMEOUT_S)
        except BaseException as exc:
            with state_changed:
                failures.append(exc)
                completed = True
                state_changed.notify()
            return
        with state_changed:
            if not expired:
                clients.append(client)
                completed = True
                state_changed.notify()
                return
        try:
            client.shutdown()
        except (AttributeError, OSError, imaplib.IMAP4.error):
            pass

    worker = threading.Thread(target=run, name="imap:connect", daemon=True)
    worker.start()
    with state_changed:
        if not state_changed.wait_for(lambda: completed, timeout=IMAP_OPERATION_TIMEOUT_S):
            expired = True
            raise ImapOperationError("connect", f"IMAP operation timed out: stage=connect timeout_s={IMAP_OPERATION_TIMEOUT_S:g}")
    if failures:
        exc = failures[0]
        if isinstance(exc, (EOFError, OSError, imaplib.IMAP4.error)):
            raise ImapOperationError("connect", f"IMAP operation failed: stage=connect error={exc}") from exc
        raise exc
    return clients[0]


def parse_uid_text(text: str) -> list[str]:
    values = [value for value in re.split(r"[\s,]+", text.strip()) if value]
    seen: set[str] = set()
    uids: list[str] = []
    for value in values:
        if not value.isdecimal():
            raise ValueError(f"UID must be decimal: {value}")
        if value not in seen:
            seen.add(value)
            uids.append(value)
    return uids


def parse_uids(raw: str, uid_file: Path | None) -> list[str]:
    parts = [raw]
    if uid_file is not None:
        parts.append(uid_file.read_text(encoding="utf-8"))
    return parse_uid_text("\n".join(parts))


def msgid_digest(msg: Message) -> str:
    msgid = rfc_message_id(msg)
    return hashlib.sha256(msgid.encode()).hexdigest()[:12] if msgid else "no-msgid"


def rfc_message_id(msg: Message) -> str:
    values = msg.get_all("Message-ID", [])
    if len(values) != 1:
        return ""
    value = " ".join(str(values[0]).split())
    return value if re.fullmatch(r"<[^<>\s]+>", value) else ""


def record_from_msg(
    uid: str,
    msg: Message,
    body: str = "",
    gmail_msgid: str = "",
    gmail_thrid: str = "",
    flags: str = "",
    labels: str = "",
    raw_sha256: str = "",
) -> MailRecord:
    return MailRecord(
        uid=uid,
        date=str(msg.get("Date", "")).replace("\n", " "),
        sender=", ".join(str(value).replace("\n", " ") for value in msg.get_all("From", [])),
        to=", ".join(str(value).replace("\n", " ") for value in msg.get_all("To", [])),
        subject=str(msg.get("Subject", "")).replace("\n", " "),
        msgid_sha256=msgid_digest(msg),
        body=body.replace("\r\n", "\n"),
        gmail_msgid=gmail_msgid,
        gmail_thrid=gmail_thrid,
        flags=flags,
        labels=labels,
        raw_sha256=raw_sha256,
    )


def is_manager_record(record: MailRecord, sender_email: str, recipient_email: str) -> bool:
    if is_mail_cleanup_excluded_subject(record.subject):
        return False
    senders = [address.casefold() for _name, address in getaddresses([record.sender]) if address]
    sender_matches = senders == [sender_email.casefold()]
    recipients = [address.casefold() for _name, address in getaddresses([record.to]) if address]
    recipient_matches = recipients == [recipient_email.casefold()]
    if sender_email.casefold() != recipient_email.casefold():
        return sender_matches and recipient_matches
    subject_matches = any(token.lower() in record.subject.lower() for token in LEGACY_MANAGER_SUBJECT_TOKENS)
    return sender_matches and recipient_matches and subject_matches


def imap_quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', r"\"") + '"'


def load_config() -> dict[str, str]:
    config = parse_env_config(CONFIG_PATH)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        raise RuntimeError(f"missing email config keys in {CONFIG_PATH}: {sorted(missing)}")
    return config


def manager_candidate_uids(client: imaplib.IMAP4_SSL, self_email: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    typ, data = imap_uid(client, "fixed-start-search", "search", None, "ALL")
    if typ != "OK":
        raise RuntimeError(f"IMAP fixed-start search failed: {typ}")
    frozen = {raw.decode() for raw in data[0].split()} if data and data[0] else set()
    settings = configured_agent_mail()
    criteria = ["FROM", f'"{self_email}"']
    subject_tokens = ("",) if settings is not None else LEGACY_MANAGER_SUBJECT_TOKENS
    for token in subject_tokens:
        typ, data = imap_uid(client, "manager-candidate-search", "search", None, *(criteria + (["SUBJECT", f'"{token}"'] if token else [])))
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for uid in [raw.decode() for raw in data[0].split()] if data and data[0] else []:
            if uid in frozen and uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def inbox_subset(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[str]:
    if not uids:
        return []
    typ, data = imap_uid(client, "inbox-subset-search", "search", None, "UID", ",".join(uids))
    if typ != "OK":
        raise RuntimeError(f"IMAP UID search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def mailbox_exists(client: imaplib.IMAP4_SSL, mailbox: str) -> bool:
    typ, data = imap_operation(client, "mailbox-list", client.list)
    if typ != "OK":
        raise RuntimeError(f"IMAP mailbox list failed: {typ}")
    return any(mailbox.encode() in raw for raw in data if isinstance(raw, bytes))


def fetch_msg_bytes(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str, n_attempts: int = 1) -> bytes:
    if n_attempts < 1:
        raise ValueError("IMAP fetch attempts must be positive")
    for _attempt in range(n_attempts):
        typ, data = imap_uid(client, f"message-fetch uid={uid}", "fetch", uid, fetch_expr)
        if typ != "OK":
            raise RuntimeError(f"IMAP fetch failed: uid={uid} typ={typ}")
        if not data or not isinstance(data[0], tuple):
            continue
        payload = data[0][1]
        if isinstance(payload, bytes):
            return payload
    raise RuntimeError(f"IMAP fetch returned no usable record: uid={uid}")


def fetch_msg(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str, n_attempts: int = 1) -> tuple[Message, str]:
    payload = fetch_msg_bytes(client, uid, fetch_expr, n_attempts)
    return BytesParser(policy=policy.default).parsebytes(payload), hashlib.sha256(payload).hexdigest()


def imap_response_text(data: list[object]) -> str:
    chunks: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            chunks.append(item.decode("utf-8", errors="replace"))
        elif isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, tuple):
            for part in item:
                if isinstance(part, bytes):
                    chunks.append(part.decode("utf-8", errors="replace"))
                elif isinstance(part, str):
                    chunks.append(part)
    return " ".join(chunks)


def imap_fetch_attributes(text: str) -> dict[str, str]:
    """Return top-level FETCH attributes without treating nested labels as fields."""
    outer = text.find("(")
    if outer < 0:
        return {}
    attributes: dict[str, str] = {}
    index = outer + 1
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] == ")":
            return attributes
        key_start = index
        while index < len(text) and not text[index].isspace() and text[index] not in "()":
            index += 1
        key = text[key_start:index]
        while index < len(text) and text[index].isspace():
            index += 1
        if not key or index >= len(text):
            return attributes
        value_start = index
        if text[index] == "(":
            depth = 0
            quoted = False
            escaped = False
            while index < len(text):
                char = text[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            if depth != 0:
                return attributes
        elif text[index] == '"':
            index += 1
            escaped = False
            while index < len(text):
                char = text[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace() and text[index] != ")":
                index += 1
        attributes[key.upper()] = text[value_start:index]
    return attributes


def imap_list_value(value: str) -> str:
    return " ".join(value[1:-1].split()) if value.startswith("(") and value.endswith(")") else ""


def fetch_gmail_metadata_detail(client: imaplib.IMAP4_SSL, uid: str) -> GmailMetadata:
    typ, data = imap_uid(client, f"gmail-metadata-fetch uid={uid}", "fetch", uid, GMAIL_METADATA_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail metadata fetch failed: typ={typ}")
    attributes = imap_fetch_attributes(imap_response_text(data))
    gmail_msgid = attributes.get("X-GM-MSGID", "")
    gmail_thrid = attributes.get("X-GM-THRID", "")
    return GmailMetadata(
        gmail_msgid if gmail_msgid.isdecimal() else "",
        gmail_thrid if gmail_thrid.isdecimal() else "",
        imap_list_value(attributes.get("FLAGS", "")),
        imap_list_value(attributes.get("X-GM-LABELS", "")),
    )


def fetch_gmail_metadata(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str, str, str]:
    metadata = fetch_gmail_metadata_detail(client, uid)
    return metadata.gmail_msgid, metadata.gmail_thrid, metadata.flags, metadata.labels


def gmail_extension_advertised(client: imaplib.IMAP4_SSL) -> bool:
    typ, data = imap_operation(client, "capability", client.capability)
    if typ != "OK":
        raise RuntimeError(f"IMAP capability query failed: {typ}")
    return "X-GM-EXT-1" in imap_response_text(data).upper().split()


def fetch_record(client: imaplib.IMAP4_SSL, uid: str, with_body: bool, with_metadata: bool, n_fetch_attempts: int = 1) -> MailRecord:
    msg, raw_sha256 = fetch_msg(client, uid, FULL_FETCH if with_body else HEADER_FETCH, n_fetch_attempts)
    gmail_msgid, gmail_thrid, flags, labels = fetch_gmail_metadata(client, uid) if with_metadata else ("", "", "", "")
    return record_from_msg(
        uid,
        msg,
        message_text(msg) if with_body else "",
        gmail_msgid,
        gmail_thrid,
        flags,
        labels,
        raw_sha256,
    )


def fetch_records(client: imaplib.IMAP4_SSL, uids: list[str], with_body: bool, with_metadata: bool = False, n_fetch_attempts: int = 1) -> list[MailRecord]:
    return [fetch_record(client, uid, with_body, with_metadata, n_fetch_attempts) for uid in uids]


def accepted_manager_headers(client: imaplib.IMAP4_SSL, uids: list[str], sender_email: str, recipient_email: str) -> tuple[list[MailRecord], list[str]]:
    records = fetch_records(client, uids, with_body=False)
    accepted = [record for record in records if is_manager_record(record, sender_email, recipient_email)]
    skipped = [record.uid for record in records if not is_manager_record(record, sender_email, recipient_email)]
    return accepted, skipped


def mail_boundary(config: dict[str, str]) -> tuple[str, str]:
    """Return the exact agent sender and human mailbox recipient boundary."""
    split_settings = configured_agent_mail()
    if split_settings is None:
        return config["user"], config["user"]
    if config["user"].casefold() != split_settings.human_address.casefold():
        raise RuntimeError("human cleanup mailbox does not match OMO_HUMAN_EMAIL_ADDRESS")
    return split_settings.agent_address, split_settings.human_address


def open_mailbox(readonly: bool) -> tuple[imaplib.IMAP4_SSL, dict[str, str]]:
    config = load_config()
    client = connect_mailbox(config["host"])
    try:
        imap_operation(client, "login", lambda: client.login(config["user"], config["password"]))
        typ, _data = imap_operation(client, "select mailbox=INBOX", lambda: client.select("INBOX", readonly=readonly))
        if typ != "OK":
            raise RuntimeError(f"IMAP select INBOX failed: {typ}")
    except Exception:
        try:
            logout_mailbox(client)
        except RuntimeError:
            pass
        raise
    return client, config


def selected_uidvalidity(client: imaplib.IMAP4_SSL) -> str:
    _name, data = imap_operation(client, "selected-uidvalidity", lambda: client.response("UIDVALIDITY"))
    values = [value.decode() for value in data or [] if isinstance(value, bytes)]
    if len(values) != 1 or not values[0].isdecimal():
        raise RuntimeError("selected mailbox omitted UIDVALIDITY")
    return values[0]


def select_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, readonly: bool) -> None:
    typ, _data = imap_operation(client, f"select mailbox={mailbox}", lambda: client.select(imap_quoted(mailbox), readonly=readonly))
    if typ != "OK":
        raise RuntimeError(f"IMAP select failed: mailbox={mailbox} typ={typ}")


def imap_mailbox_name(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def special_use_mailboxes(client: imaplib.IMAP4_SSL) -> dict[str, str]:
    typ, data = imap_operation(client, "special-use-mailbox-list", client.list)
    if typ != "OK":
        raise RuntimeError(f"IMAP mailbox list failed: {typ}")
    mailboxes: dict[str, str] = {}
    for raw in data:
        if not isinstance(raw, bytes):
            continue
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("IMAP special-use mailbox name was not ASCII modified UTF-7") from exc
        match = re.fullmatch(r"\((?P<attributes>[^)]*)\)\s+(?:\"[^\"]*\"|NIL)\s+(?P<mailbox>.+)", line)
        if match is None:
            continue
        attributes = set(match.group("attributes").split())
        mailbox = imap_mailbox_name(match.group("mailbox"))
        for special_use in (r"\All", r"\Sent"):
            if special_use in attributes:
                mailboxes[special_use] = mailbox
    return mailboxes


def gmail_thread_uids(client: imaplib.IMAP4_SSL, gmail_thrid: str) -> list[str]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity was missing or malformed")
    typ, data = imap_uid(client, f"gmail-thread-search thread={gmail_thrid}", "search", None, "X-GM-THRID", gmail_thrid)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail thread search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def gmail_message_uids(client: imaplib.IMAP4_SSL, gmail_msgid: str) -> list[str]:
    if not gmail_msgid.isdecimal():
        raise RuntimeError("Gmail message identity was missing or malformed")
    typ, data = imap_uid(client, f"gmail-message-search message={gmail_msgid}", "search", None, "X-GM-MSGID", gmail_msgid)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail message search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def require_gmail_identities(records: list[MailRecord]) -> None:
    missing = [record.uid for record in records if not record.gmail_msgid or not record.gmail_thrid or not record.raw_sha256]
    if missing:
        raise RuntimeError(f"Gmail identity metadata missing for {len(missing)} source messages")


def fetch_imap_thread_contexts(
    client: imaplib.IMAP4_SSL,
    records: list[MailRecord],
) -> tuple[dict[str, str], dict[str, list[MailRecord]]]:
    """Fetch complete Gmail thread context through the configured IMAP session."""
    require_gmail_identities(records)
    if len({record.gmail_msgid for record in records}) != len(records):
        raise RuntimeError("configured IMAP mailbox returned duplicate Gmail message identities")
    special_use = special_use_mailboxes(client)
    all_mailbox = special_use.get(r"\All", "")
    if not all_mailbox:
        raise RuntimeError("Gmail All Mail special-use mailbox was not discovered")
    if not special_use.get(r"\Sent"):
        raise RuntimeError("Gmail Sent special-use mailbox was not discovered")
    source_ids_by_thread: dict[str, set[str]] = {}
    for record in records:
        source_ids_by_thread.setdefault(record.gmail_thrid, set()).add(record.gmail_msgid)
    select_mailbox(client, all_mailbox, readonly=True)
    records_by_thread: dict[str, list[MailRecord]] = {}
    for gmail_thrid, source_ids in sorted(source_ids_by_thread.items()):
        context_records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
        require_gmail_identities(context_records)
        context_ids = [record.gmail_msgid for record in context_records]
        if not context_records or len(context_ids) != len(set(context_ids)) or any(record.gmail_thrid != gmail_thrid for record in context_records) or not source_ids.issubset(context_ids):
            raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
        records_by_thread[gmail_thrid] = context_records
    return special_use, records_by_thread


def thread_context_digest(records: list[MailRecord]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda value: value.gmail_msgid):
        digest.update(
            "\t".join(
                (
                    record.gmail_msgid,
                    record.gmail_thrid,
                    record.msgid_sha256,
                    record.raw_sha256,
                    record.flags,
                    record.labels,
                )
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def tsv_value(value: str) -> str:
    return " ".join(value.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


def write_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)


def write_thread_context(out_dir: Path, records_by_thread: dict[str, list[MailRecord]], sender_email: str, recipient_email: str) -> dict[str, str]:
    context_dir = out_dir / "threads"
    write_private_dir(context_dir)
    rows = ["gmail_thrid\tgmail_msgid\tmsgid_sha256\traw_sha256\tflags\tlabels\tscope\tsender\trecipient\tall_mailbox_uid\tbody_bytes"]
    digests: dict[str, str] = {}
    for gmail_thrid, records in sorted(records_by_thread.items()):
        digests[gmail_thrid] = thread_context_digest(records)
        for record in records:
            scope = "manager-to-human" if is_manager_record(record, sender_email, recipient_email) else "other"
            rows.append(
                "\t".join(
                    (
                        gmail_thrid,
                        record.gmail_msgid,
                        record.msgid_sha256,
                        record.raw_sha256,
                        tsv_value(record.flags),
                        tsv_value(record.labels),
                        scope,
                        tsv_value(record.sender),
                        tsv_value(record.to),
                        record.uid,
                        str(record.body_bytes),
                    )
                )
            )
            write_private(context_dir / f"{gmail_thrid}-{record.gmail_msgid}.txt", export_body(record, include_addresses=True))
    write_private(out_dir / "thread-context.tsv", "\n".join(rows) + "\n")
    write_private(
        out_dir / "thread-digests.tsv",
        "gmail_thrid\tthread_context_sha256\n" + "\n".join(f"{thread}\t{digest}" for thread, digest in sorted(digests.items())) + "\n",
    )
    return digests


def print_records(records: list[MailRecord]) -> None:
    print("uid\tdate\tmsgid_sha256\tsubject")
    for record in records:
        print(f"{record.uid}\t{record.date}\t{record.msgid_sha256}\t{record.subject}")


def cmd_identity_preflight(_args: argparse.Namespace) -> int:
    """Print aggregate IMAP Gmail identity evidence without identifiers."""
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        headers, skipped = accepted_manager_headers(client, manager_candidate_uids(client, sender_email), sender_email, recipient_email)
        uidvalidity = ""
        source_records: list[MailRecord] = []
        records_by_thread: dict[str, list[MailRecord]] = {}
        gmail_extension = 0
        imap_failure = 0
        try:
            gmail_extension = int(gmail_extension_advertised(client))
            if not gmail_extension:
                raise RuntimeError("configured IMAP mailbox does not advertise Gmail identity support")
            uidvalidity = selected_uidvalidity(client)
            source_records = [replace(record, source_uidvalidity=uidvalidity) for record in fetch_records(client, [header.uid for header in headers], with_body=True, with_metadata=True)]
            require_gmail_identities(source_records)
            if len({record.gmail_msgid for record in source_records}) != len(source_records):
                raise RuntimeError("configured IMAP mailbox returned duplicate Gmail message identities")
            _special_use, records_by_thread = fetch_imap_thread_contexts(client, source_records)
        except (imaplib.IMAP4.error, RuntimeError) as exc:
            imap_failure = 1
            stage = exc.stage if isinstance(exc, ImapOperationError) else "identity-evidence"
            print(f"identity_preflight blocked failed_stage={stage}", file=sys.stderr)
        expected_threads = len({record.gmail_thrid for record in source_records})
        gate = "pass" if uidvalidity and gmail_extension and not imap_failure and len(source_records) == len(headers) and len(records_by_thread) == expected_threads else "block"
        print(
            "identity_preflight"
            f" accepted={len(headers)}"
            f" skipped_boundary_mismatch={len(skipped)}"
            f" source_uidvalidity_present={int(bool(uidvalidity))}"
            f" gmail_imap_extension={gmail_extension}"
            f" imap_evidence_failure_count={imap_failure}"
            f" unique_identity_count={len(source_records) if not imap_failure else 0}"
            f" complete_thread_count={len(records_by_thread)}"
            f" gate={gate}"
        )
    finally:
        logout_mailbox(client)
    return 0 if gate == "pass" else 1


def cmd_snapshot(_args: argparse.Namespace) -> int:
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        uids = manager_candidate_uids(client, sender_email)
        records, skipped = accepted_manager_headers(client, uids, sender_email, recipient_email)
        print(f"manager_candidate_count={len(records)}")
        if skipped:
            print(f"skipped_boundary_mismatch={len(skipped)}")
        print_records(records)
    finally:
        logout_mailbox(client)
    return 0


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def write_private_exclusive(path: Path, text: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"private evidence already exists: {path.name}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def ensure_empty_private_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise RuntimeError(f"export directory must be empty: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def export_body(record: MailRecord, include_addresses: bool = False) -> str:
    addresses = f"From: {record.sender}\nTo: {record.to}\n" if include_addresses else ""
    return (
        f"UID: {record.uid}\n"
        f"Date: {record.date}\n"
        f"Subject: {record.subject}\n"
        f"{addresses}"
        f"Message-ID-SHA256: {record.msgid_sha256}\n"
        f"Gmail-Message-ID: {record.gmail_msgid}\n"
        f"Gmail-Thread-ID: {record.gmail_thrid}\n"
        f"Source-UIDVALIDITY: {record.source_uidvalidity}\n"
        f"Flags: {record.flags}\n"
        f"Labels: {record.labels}\n"
        f"Raw-SHA256: {record.raw_sha256}\n\n"
        f"{record.body.rstrip()}\n"
    )


def export_manifest(records: list[MailRecord], thread_digests: dict[str, str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "uid",
            "source_mailbox",
            "uidvalidity",
            "date",
            "gmail_msgid",
            "gmail_thrid",
            "msgid_sha256",
            "raw_sha256",
            "flags",
            "labels",
            "thread_context_sha256",
            "body_bytes",
            "subject",
        ),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                "uid": record.uid,
                "source_mailbox": "INBOX",
                "uidvalidity": record.source_uidvalidity,
                "date": tsv_value(record.date),
                "gmail_msgid": record.gmail_msgid,
                "gmail_thrid": record.gmail_thrid,
                "msgid_sha256": record.msgid_sha256,
                "raw_sha256": record.raw_sha256,
                "flags": tsv_value(record.flags),
                "labels": tsv_value(record.labels),
                "thread_context_sha256": thread_digests[record.gmail_thrid],
                "body_bytes": str(record.body_bytes),
                "subject": tsv_value(record.subject),
            }
        )
    return output.getvalue()


def export_batches(records: list[MailRecord], threads_per_batch: int) -> str:
    if threads_per_batch < 1:
        raise ValueError("threads per batch must be positive")
    threads = sorted({record.gmail_thrid for record in records})
    batch_by_thread = {thread: f"batch-{index // threads_per_batch + 1:04d}" for index, thread in enumerate(threads)}
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=("batch_id", "gmail_thrid", "uid", "gmail_msgid", "subject", "body_file"),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for record in sorted(records, key=lambda value: (value.gmail_thrid, value.gmail_msgid)):
        writer.writerow(
            {
                "batch_id": batch_by_thread[record.gmail_thrid],
                "gmail_thrid": record.gmail_thrid,
                "uid": record.uid,
                "gmail_msgid": record.gmail_msgid,
                "subject": tsv_value(record.subject),
                "body_file": f"{record.uid}.txt",
            }
        )
    return output.getvalue()


def read_tsv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
                raise RuntimeError(f"private source map is missing required fields: {path.name}")
            return [{key: value or "" for key, value in row.items()} for row in reader]
    except OSError as exc:
        raise RuntimeError(f"could not read private source map: {path.name}") from exc


def read_literal_tsv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    """Read historical tab-joined evidence without interpreting quote characters."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"could not read private source map: {path.name}") from exc
    if not lines:
        raise RuntimeError(f"private source map is missing required fields: {path.name}")
    fieldnames = lines[0].split("\t")
    if len(fieldnames) != len(set(fieldnames)) or not required_fields.issubset(fieldnames):
        raise RuntimeError(f"private source map is missing required fields: {path.name}")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = line.split("\t")
        if len(values) != len(fieldnames):
            raise RuntimeError(f"private source map has malformed row: {path.name}")
        rows.append(dict(zip(fieldnames, values, strict=True)))
    return rows


def read_tsv_text(text: str, required_fields: set[str], evidence_name: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
        raise RuntimeError(f"private source map is missing required fields: {evidence_name}")
    return [{key: value or "" for key, value in row.items()} for row in reader]


def export_source_map(out_dir: Path, requested: list[str]) -> dict[str, dict[str, str]]:
    rows = read_tsv(
        out_dir / "manifest.tsv",
        {
            "uid",
            "source_mailbox",
            "uidvalidity",
            "gmail_msgid",
            "gmail_thrid",
            "msgid_sha256",
            "raw_sha256",
            "flags",
            "labels",
            "thread_context_sha256",
        },
    )
    source_map = {row["uid"]: row for row in rows if row["uid"]}
    gmail_identities = [(row["gmail_thrid"], row["gmail_msgid"]) for row in rows]
    if len(source_map) != len(rows) or len(set(gmail_identities)) != len(rows) or any(uid not in source_map for uid in requested):
        raise RuntimeError("requested sources were absent or ambiguous in the private source map")
    return {uid: source_map[uid] for uid in requested}


def export_mailboxes(out_dir: Path) -> dict[str, str]:
    rows = read_tsv(out_dir / "mailboxes.tsv", {"role", "mailbox"})
    mailboxes = {row["role"]: row["mailbox"] for row in rows if row["role"] and row["mailbox"]}
    if r"\All" not in mailboxes or r"\Sent" not in mailboxes:
        raise RuntimeError("private source map lacks required special-use mailboxes")
    return mailboxes


def batch_rows(source_dir: Path) -> list[dict[str, str]]:
    rows = read_tsv(source_dir / "batches.tsv", {"batch_id", "gmail_thrid", "uid", "gmail_msgid", "subject", "body_file"})
    uids = [row["uid"] for row in rows]
    identities = [(row["gmail_thrid"], row["gmail_msgid"]) for row in rows]
    thread_batches: dict[str, set[str]] = {}
    for row in rows:
        thread_batches.setdefault(row["gmail_thrid"], set()).add(row["batch_id"])
    if (
        any(not re.fullmatch(r"batch-[0-9]{4}", row["batch_id"]) or not row["gmail_thrid"].isdecimal() or not row["uid"].isdecimal() for row in rows)
        or len(uids) != len(set(uids))
        or len(identities) != len(set(identities))
        or any(len(batches) != 1 for batches in thread_batches.values())
    ):
        raise RuntimeError("private batch map is malformed or assigns a source more than once")
    return rows


def validate_owner(owner: str) -> str:
    if not owner or tsv_value(owner) != owner:
        raise RuntimeError("batch owner must be one nonempty line")
    return owner


def claim_batch(source_dir: Path, batch_id: str, owner: str) -> None:
    owner = validate_owner(owner)
    if batch_id not in {row["batch_id"] for row in batch_rows(source_dir)}:
        raise RuntimeError(f"unknown batch: {batch_id}")
    claim_path = source_dir / "claims" / f"{batch_id}.tsv"
    content = f"batch_id\towner\n{batch_id}\t{owner}\n"
    try:
        write_private_exclusive(claim_path, content)
    except RuntimeError:
        try:
            existing = claim_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read batch claim: {batch_id}") from exc
        if existing != content:
            raise RuntimeError(f"batch already belongs to another owner: {batch_id}")


def require_batch_owner(source_dir: Path, batch_id: str, owner: str) -> list[dict[str, str]]:
    owner = validate_owner(owner)
    rows = [row for row in batch_rows(source_dir) if row["batch_id"] == batch_id]
    if not rows:
        raise RuntimeError(f"unknown batch: {batch_id}")
    expected = f"batch_id\towner\n{batch_id}\t{owner}\n"
    try:
        actual = (source_dir / "claims" / f"{batch_id}.tsv").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"batch is not claimed: {batch_id}") from exc
    if actual != expected:
        raise RuntimeError(f"batch is not owned by {owner}: {batch_id}")
    return rows


def evidence_digest(path: Path, kind: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read {kind} evidence") from exc
    if not data.strip():
        raise RuntimeError(f"{kind} evidence must not be empty")
    return hashlib.sha256(data).hexdigest()


def thread_batch_rows(source_dir: Path, batch_id: str, owner: str, gmail_thrid: str) -> list[dict[str, str]]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity must be decimal")
    rows = [row for row in require_batch_owner(source_dir, batch_id, owner) if row["gmail_thrid"] == gmail_thrid]
    if not rows:
        raise RuntimeError("thread is not assigned to the claimed batch")
    return rows


def disposition_text(
    rows: list[dict[str, str]],
    batch_id: str,
    owner: str,
    moved_uids: set[str],
    reason_sha256: str,
    task_evidence_sha256: str,
    replacement: str,
) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=("batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement"),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "batch_id": batch_id,
                "owner": owner,
                "gmail_thrid": row["gmail_thrid"],
                "uid": row["uid"],
                "disposition": "trashed" if row["uid"] in moved_uids else "retained",
                "reason_sha256": reason_sha256,
                "task_evidence_sha256": task_evidence_sha256,
                "replacement": replacement,
            }
        )
    return output.getvalue()


def prepare_thread_disposition(
    source_dir: Path,
    batch_id: str,
    owner: str,
    gmail_thrid: str,
    moved_uids: set[str],
    reason_file: Path,
    task_evidence_file: Path,
    replacement: str,
) -> tuple[list[dict[str, str]], str, bool]:
    rows = thread_batch_rows(source_dir, batch_id, owner, gmail_thrid)
    thread_uids = {row["uid"] for row in rows}
    thread_sources = export_source_map(source_dir, sorted(thread_uids))
    if {source["gmail_thrid"] for source in thread_sources.values()} != {gmail_thrid}:
        raise RuntimeError("batch thread does not match the fixed-start source map")
    if not moved_uids.issubset(thread_uids):
        raise RuntimeError("requested source is outside the claimed thread batch")
    evidence = disposition_text(
        rows,
        batch_id,
        owner,
        moved_uids,
        evidence_digest(reason_file, "reason"),
        evidence_digest(task_evidence_file, "task"),
        replacement,
    )
    outcome_path = source_dir / "outcomes" / f"{gmail_thrid}.tsv"
    if outcome_path.exists():
        raise RuntimeError("thread already has a final disposition")
    intent_path = source_dir / "intents" / f"{gmail_thrid}.tsv"
    if not intent_path.exists():
        write_private_exclusive(intent_path, evidence)
        return rows, evidence, False
    try:
        existing = intent_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("could not read existing thread intent") from exc
    if existing == evidence:
        return rows, evidence, False
    if moved_uids:
        raise RuntimeError("thread already has a different mutation intent")
    return rows, evidence, True


def cmd_claim_batch(args: argparse.Namespace) -> int:
    claim_batch(args.source_dir, args.batch_id, args.owner)
    print(f"claimed batch={args.batch_id} owner={args.owner}")
    return 0


def cmd_retain_thread(args: argparse.Namespace) -> int:
    rows, evidence, recovery_needed = prepare_thread_disposition(
        args.source_dir,
        args.batch_id,
        args.owner,
        args.gmail_thrid,
        set(),
        args.reason_file,
        args.task_evidence_file,
        "not-required-retained",
    )
    if recovery_needed:
        source_map = export_source_map(args.source_dir, [row["uid"] for row in rows])
        client, _config = open_mailbox(readonly=True)
        try:
            for source in source_map.values():
                uids = gmail_message_uids(client, source["gmail_msgid"])
                if len(uids) != 1:
                    raise RuntimeError("cannot recover intent because a source is absent or ambiguous in INBOX")
                record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
                if record.gmail_msgid != source["gmail_msgid"] or record.gmail_thrid != source["gmail_thrid"]:
                    raise RuntimeError("cannot recover intent because source identity drifted")
        finally:
            logout_mailbox(client)
        write_private_exclusive(args.source_dir / "recoveries" / f"{args.gmail_thrid}.tsv", evidence)
    write_private_exclusive(args.source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", evidence)
    print(f"retained_thread={args.gmail_thrid} source_count={len(rows)}")
    return 0


def intent_reconciliation_evidence(source_dir: Path, gmail_thrid: str) -> tuple[str, list[dict[str, str]], dict[str, dict[str, str]]]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity must be decimal")
    outcome_path = source_dir / "outcomes" / f"{gmail_thrid}.tsv"
    if outcome_path.exists():
        raise RuntimeError("thread already has a final disposition")
    intent_path = source_dir / "intents" / f"{gmail_thrid}.tsv"
    try:
        evidence = intent_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("thread has no readable immutable intent") from exc
    fields = {"batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement"}
    rows = read_tsv_text(evidence, fields, intent_path.name)
    if not rows or any(
        row["gmail_thrid"] != gmail_thrid
        or row["disposition"] not in {"retained", "trashed"}
        or not re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["task_evidence_sha256"])
        or not row["replacement"]
        for row in rows
    ):
        raise RuntimeError("immutable intent is malformed or names a different thread")
    identities = {(row["batch_id"], row["owner"]) for row in rows}
    if len(identities) != 1:
        raise RuntimeError("immutable intent has ambiguous batch ownership")
    batch_id, owner = identities.pop()
    assigned = thread_batch_rows(source_dir, batch_id, owner, gmail_thrid)
    intended_uids = [row["uid"] for row in rows]
    if len(intended_uids) != len(set(intended_uids)) or set(intended_uids) != {row["uid"] for row in assigned}:
        raise RuntimeError("immutable intent does not cover the claimed fixed-start thread exactly once")
    source_map = export_source_map(source_dir, intended_uids)
    return evidence, rows, source_map


def record_matches_reconciliation_location(record: MailRecord, source: dict[str, str], _location: str) -> bool:
    return (
        record.gmail_msgid == source["gmail_msgid"]
        and record.gmail_thrid == source["gmail_thrid"]
        and record.msgid_sha256 == source["msgid_sha256"]
        and record.raw_sha256 == source["raw_sha256"]
    )


def observe_reconciliation_locations(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
) -> dict[str, dict[str, MailRecord]]:
    observed: dict[str, dict[str, MailRecord]] = {"INBOX": {}, "Trash": {}}
    for location, mailbox in (("INBOX", "INBOX"), ("Trash", TRASH_MAILBOX)):
        select_mailbox(client, mailbox, readonly=True)
        for uid, source in source_map.items():
            matches = gmail_message_uids(client, source["gmail_msgid"])
            if len(matches) > 1:
                raise RuntimeError(f"source is ambiguous in {location}")
            if matches:
                observed[location][uid] = fetch_record(client, matches[0], with_body=True, with_metadata=True)
    return observed


def require_reconciliation_locations(
    rows: list[dict[str, str]],
    source_map: dict[str, dict[str, str]],
    observed: dict[str, dict[str, MailRecord]],
    sender_email: str,
    recipient_email: str,
) -> None:
    for row in rows:
        uid = row["uid"]
        expected = "Trash" if row["disposition"] == "trashed" else "INBOX"
        other = "INBOX" if expected == "Trash" else "Trash"
        if uid not in observed[expected] or uid in observed[other]:
            raise RuntimeError("source is in both, neither, or the wrong reconciliation location")
        record = observed[expected][uid]
        if not is_manager_record(record, sender_email, recipient_email) or not record_matches_reconciliation_location(record, source_map[uid], expected):
            raise RuntimeError("source identity or content changed")


def frozen_thread_context(source_dir: Path, gmail_thrid: str) -> dict[str, dict[str, str]]:
    expected_rows = [
        row
        for row in read_literal_tsv(
            source_dir / "thread-context.tsv",
            {"gmail_thrid", "gmail_msgid", "msgid_sha256", "raw_sha256", "flags", "labels"},
        )
        if row["gmail_thrid"] == gmail_thrid
    ]
    expected = {row["gmail_msgid"]: row for row in expected_rows}
    if not expected_rows or len(expected) != len(expected_rows):
        raise RuntimeError("frozen thread evidence is missing or ambiguous")
    exact_records: list[MailRecord] = []
    required_headers = {
        "Gmail-Message-ID",
        "Gmail-Thread-ID",
        "Message-ID-SHA256",
        "Raw-SHA256",
        "Flags",
        "Labels",
    }
    for gmail_msgid, row in expected.items():
        context_path = source_dir / "threads" / f"{gmail_thrid}-{gmail_msgid}.txt"
        header_text = context_path.read_text(encoding="utf-8").split("\n\n", 1)[0]
        headers: dict[str, str] = {}
        for line in header_text.splitlines():
            name, separator, value = line.partition(": ")
            if name not in required_headers:
                continue
            if not separator or name in headers:
                raise RuntimeError("frozen thread evidence has malformed exported headers")
            headers[name] = value
        if set(headers) != required_headers:
            raise RuntimeError("frozen thread evidence has incomplete exported headers")
        if (
            headers["Gmail-Message-ID"] != gmail_msgid
            or headers["Gmail-Thread-ID"] != gmail_thrid
            or headers["Message-ID-SHA256"] != row["msgid_sha256"]
            or headers["Raw-SHA256"] != row["raw_sha256"]
            or tsv_value(headers["Flags"]) != row["flags"]
            or tsv_value(headers["Labels"]) != row["labels"]
        ):
            raise RuntimeError("frozen thread evidence files disagree")
        exact_records.append(
            MailRecord(
                uid="",
                date="",
                sender="",
                to="",
                subject="",
                msgid_sha256=headers["Message-ID-SHA256"],
                raw_sha256=headers["Raw-SHA256"],
                gmail_msgid=headers["Gmail-Message-ID"],
                gmail_thrid=headers["Gmail-Thread-ID"],
                flags=headers["Flags"],
                labels=headers["Labels"],
            )
        )
    frozen_digest = thread_context_digest(exact_records)
    digest_rows = [row for row in read_tsv(source_dir / "thread-digests.tsv", {"gmail_thrid", "thread_context_sha256"}) if row["gmail_thrid"] == gmail_thrid]
    manifest_digests = {
        row["thread_context_sha256"]
        for row in read_tsv(source_dir / "manifest.tsv", {"gmail_thrid", "thread_context_sha256"})
        if row["gmail_thrid"] == gmail_thrid
    }
    if len(digest_rows) != 1 or digest_rows[0]["thread_context_sha256"] != frozen_digest or manifest_digests != {frozen_digest}:
        raise RuntimeError("frozen thread evidence digest binding failed")
    return expected


def reconciliation_thread_unchanged(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    source_dir: Path,
    gmail_thrid: str,
    trash_records: list[MailRecord],
) -> bool:
    expected = frozen_thread_context(source_dir, gmail_thrid)
    if not expected:
        return False
    select_mailbox(client, all_mailbox, readonly=True)
    records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
    require_gmail_identities(records)
    all_msgids = {record.gmail_msgid for record in records}
    if any(record.gmail_msgid in all_msgids for record in trash_records):
        return False
    records.extend(trash_records)
    actual = {record.gmail_msgid: record for record in records}
    if len(actual) != len(records) or not set(expected).issubset(actual) or any(record.gmail_thrid != gmail_thrid for record in records):
        return False
    for gmail_msgid, row in expected.items():
        record = actual[gmail_msgid]
        if (
            record.gmail_thrid != gmail_thrid
            or record.msgid_sha256 != row["msgid_sha256"]
            or record.raw_sha256 != row["raw_sha256"]
        ):
            return False
    return True


def additive_recovery_thread_intact(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    source_dir: Path,
    gmail_thrid: str,
    trash_records: list[MailRecord],
) -> bool:
    expected = frozen_thread_context(source_dir, gmail_thrid)
    trashed_msgids = {record.gmail_msgid for record in trash_records}
    if not expected or len(trashed_msgids) != len(trash_records):
        return False
    select_mailbox(client, all_mailbox, readonly=True)
    records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
    require_gmail_identities(records)
    all_msgids = {record.gmail_msgid for record in records}
    if len(all_msgids) != len(records) or any(record.gmail_msgid in all_msgids for record in trash_records):
        return False
    records.extend(trash_records)
    actual = {record.gmail_msgid: record for record in records}
    if len(actual) != len(records) or not set(expected).issubset(actual) or not set(actual) - set(expected):
        return False
    for gmail_msgid, row in expected.items():
        record = actual[gmail_msgid]
        if (
            record.gmail_thrid != gmail_thrid
            or record.msgid_sha256 != row["msgid_sha256"]
            or record.raw_sha256 != row["raw_sha256"]
        ):
            return False
    return all(record.gmail_thrid == gmail_thrid for record in records)


def terminal_recovery_text(intent_rows: list[dict[str, str]]) -> str:
    fields = ("batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "terminal_recovery")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in intent_rows:
        writer.writerow({**{field: row[field] for field in fields[:-1]}, "terminal_recovery": "skipped_already_trashed"})
    return output.getvalue()


def cmd_reconcile_intent(args: argparse.Namespace) -> int:
    evidence, rows, source_map = intent_reconciliation_evidence(args.source_dir, args.gmail_thrid)
    expected_mailboxes = export_mailboxes(args.source_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        select_mailbox(client, "INBOX", readonly=True)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal() or selected_uidvalidity(client) not in expected_uidvalidities:
            raise RuntimeError("INBOX UIDVALIDITY changed from the frozen source map")
        if not mailbox_exists(client, TRASH_MAILBOX):
            raise RuntimeError(f"mailbox is missing: {TRASH_MAILBOX}")
        if special_use_mailboxes(client).get(r"\All") != expected_mailboxes[r"\All"]:
            raise RuntimeError("All Mail mailbox identity changed from the frozen source map")
        observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, observed, sender_email, recipient_email)
        final_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, final_observed, sender_email, recipient_email)
        final_trash_records = [final_observed["Trash"][row["uid"]] for row in rows if row["disposition"] == "trashed"]
        if not reconciliation_thread_unchanged(client, expected_mailboxes[r"\All"], args.source_dir, args.gmail_thrid, final_trash_records):
            raise RuntimeError("complete Gmail thread context changed")
    finally:
        logout_mailbox(client)
    write_private_exclusive(args.source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", evidence)
    trashed = sum(row["disposition"] == "trashed" for row in rows)
    print(f"reconciled_thread={args.gmail_thrid} sources={len(rows)} retained={len(rows) - trashed} trashed={trashed} mailbox_mutations=0 permanent_deleted=0")
    return 0


def cmd_recover_already_trashed(args: argparse.Namespace) -> int:
    _evidence, rows, source_map = intent_reconciliation_evidence(args.source_dir, args.gmail_thrid)
    if any(row["disposition"] != "trashed" for row in rows):
        raise RuntimeError("terminal Trash recovery requires an all-trashed immutable intent")
    expected_mailboxes = export_mailboxes(args.source_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        select_mailbox(client, "INBOX", readonly=True)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal() or selected_uidvalidity(client) not in expected_uidvalidities:
            raise RuntimeError("INBOX UIDVALIDITY changed from the frozen source map")
        if not mailbox_exists(client, TRASH_MAILBOX):
            raise RuntimeError(f"mailbox is missing: {TRASH_MAILBOX}")
        if special_use_mailboxes(client).get(r"\All") != expected_mailboxes[r"\All"]:
            raise RuntimeError("All Mail mailbox identity changed from the frozen source map")
        observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, observed, sender_email, recipient_email)
        final_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, final_observed, sender_email, recipient_email)
        trash_records = [final_observed["Trash"][row["uid"]] for row in rows]
        if not additive_recovery_thread_intact(client, expected_mailboxes[r"\All"], args.source_dir, args.gmail_thrid, trash_records):
            raise RuntimeError("frozen Gmail thread context changed or disappeared")
        receipt_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, receipt_observed, sender_email, recipient_email)
    finally:
        logout_mailbox(client)
    receipt_path = args.source_dir / "recoveries" / f"{args.gmail_thrid}.skipped-already-trashed.tsv"
    write_private_exclusive(receipt_path, terminal_recovery_text(rows))
    print(f"recovered_thread={args.gmail_thrid} skipped_already_trashed={len(rows)} mailbox_mutations=0 permanent_deleted=0")
    return 0


def cmd_verify_run(args: argparse.Namespace) -> int:
    run_rows = read_tsv(args.source_dir / "run.tsv", {"fixed_start_utc", "source_count", "thread_count", "threads_per_batch"})
    if len(run_rows) != 1:
        raise RuntimeError("fixed-start run evidence must contain exactly one row")
    manifest = read_tsv(args.source_dir / "manifest.tsv", {"uid", "gmail_thrid", "gmail_msgid"})
    batches = batch_rows(args.source_dir)
    manifest_uids = [row["uid"] for row in manifest]
    batch_uids = [row["uid"] for row in batches]
    if len(manifest_uids) != len(set(manifest_uids)) or set(manifest_uids) != set(batch_uids):
        raise RuntimeError("fixed-start manifest and batch map do not match")
    threads = {row["gmail_thrid"] for row in manifest}
    run = run_rows[0]
    if run["source_count"] != str(len(manifest)) or run["thread_count"] != str(len(threads)) or not run["fixed_start_utc"]:
        raise RuntimeError("fixed-start run counts do not match the immutable manifest")
    missing_terminal_evidence = [
        gmail_thrid
        for gmail_thrid in sorted(threads)
        if not (args.source_dir / "outcomes" / f"{gmail_thrid}.tsv").exists()
        and not (args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv").exists()
    ]
    if missing_terminal_evidence:
        raise RuntimeError(
            f"fixed-start threads lack outcome or terminal recovery: count={len(missing_terminal_evidence)} threads={','.join(missing_terminal_evidence)}"
        )
    dispositions: list[dict[str, str]] = []
    skipped_already_trashed = 0
    skipped_already_trashed_threads = 0
    for gmail_thrid in sorted(threads):
        outcome_path = args.source_dir / "outcomes" / f"{gmail_thrid}.tsv"
        intent_path = args.source_dir / "intents" / f"{gmail_thrid}.tsv"
        try:
            intent = intent_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"fixed-start thread lacks immutable intent: {gmail_thrid}") from exc
        if outcome_path.exists():
            if (args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv").exists():
                raise RuntimeError(f"thread has both normal outcome and terminal recovery evidence: {gmail_thrid}")
            rows = read_tsv(
                outcome_path,
                {"batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement"},
            )
            outcome = outcome_path.read_text(encoding="utf-8")
            if intent != outcome:
                try:
                    recovery = (args.source_dir / "recoveries" / f"{gmail_thrid}.tsv").read_text(encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(f"changed intent lacks recovery evidence for thread: {gmail_thrid}") from exc
                if recovery != outcome:
                    raise RuntimeError(f"recovery evidence does not match outcome for thread: {gmail_thrid}")
        else:
            recovery_path = args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv"
            try:
                recovery_text = recovery_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"fixed-start thread lacks outcome or terminal recovery: {gmail_thrid}") from exc
            rows = read_tsv_text(
                recovery_text,
                {"batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "terminal_recovery"},
                recovery_path.name,
            )
            intent_rows = read_tsv_text(intent, {"batch_id", "owner", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement"}, intent_path.name)
            if recovery_text != terminal_recovery_text(intent_rows) or any(row["terminal_recovery"] != "skipped_already_trashed" or row["disposition"] != "trashed" for row in rows):
                raise RuntimeError(f"invalid terminal recovery evidence for thread: {gmail_thrid}")
            skipped_already_trashed += len(rows)
            skipped_already_trashed_threads += 1
        if any(row["gmail_thrid"] != gmail_thrid or row["disposition"] not in {"retained", "trashed"} for row in rows):
            raise RuntimeError(f"invalid disposition outcome for thread: {gmail_thrid}")
        dispositions.extend(rows)
    disposition_uids = [row["uid"] for row in dispositions]
    if len(disposition_uids) != len(set(disposition_uids)) or set(disposition_uids) != set(manifest_uids):
        raise RuntimeError("fixed-start sources are not each classified exactly once")
    expected_batch = {row["uid"]: (row["batch_id"], row["gmail_thrid"]) for row in batches}
    for row in dispositions:
        if expected_batch[row["uid"]] != (row["batch_id"], row["gmail_thrid"]):
            raise RuntimeError("disposition attempted cross-batch mutation")
        if not re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"]) or not re.fullmatch(r"[0-9a-f]{64}", row["task_evidence_sha256"]):
            raise RuntimeError("disposition lacks bound reason or task evidence")
        if not row["replacement"]:
            raise RuntimeError("disposition lacks replacement decision evidence")
        require_batch_owner(args.source_dir, row["batch_id"], row["owner"])
    retained = sum(row["disposition"] == "retained" for row in dispositions)
    trashed = sum(row["disposition"] == "trashed" for row in dispositions)
    print(f"fixed_start_verified=1 sources={len(manifest)} threads={len(threads)} retained={retained} trashed={trashed - skipped_already_trashed} skipped_already_trashed={skipped_already_trashed} skipped_already_trashed_threads={skipped_already_trashed_threads} later_arrivals_included=0 live_full_scan=0 permanent_deleted=0")
    return 0


def record_matches_source_map(record: MailRecord, source: dict[str, str]) -> bool:
    return (
        source["source_mailbox"] == "INBOX"
        and record.source_uidvalidity == source["uidvalidity"]
        and record.gmail_msgid == source["gmail_msgid"]
        and record.gmail_thrid == source["gmail_thrid"]
        and record.msgid_sha256 == source["msgid_sha256"]
        and record.raw_sha256 == source["raw_sha256"]
    )


def replacement_exists(
    client: imaplib.IMAP4_SSL,
    mailbox: str,
    replacement_id: str,
    sender_email: str,
    recipient_email: str,
) -> bool:
    if not re.fullmatch(r"<[^<>\s]+>", replacement_id):
        return False
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-message-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        if typ != "OK":
            return False
        uids = [raw.decode() for raw in data[0].split()] if data and data[0] else []
        if len(uids) != 1:
            return False
        msg, _digest = fetch_msg(client, uids[0], HEADER_FETCH)
        senders = [address.casefold() for _name, address in getaddresses(msg.get_all("From", [])) if address]
        recipients = [address.casefold() for _name, address in getaddresses(msg.get_all("To", [])) if address]
        return rfc_message_id(msg) == replacement_id and senders == [sender_email.casefold()] and recipient_email.casefold() in recipients
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def cmd_export(args: argparse.Namespace) -> int:
    out_dir = args.out_dir
    ensure_empty_private_dir(out_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        header_records, skipped = accepted_manager_headers(client, manager_candidate_uids(client, sender_email), sender_email, recipient_email)
        uidvalidity = selected_uidvalidity(client)
        source_records = [
            replace(record, source_uidvalidity=uidvalidity)
            for record in fetch_records(
                client,
                [record.uid for record in header_records],
                with_body=True,
                with_metadata=True,
                n_fetch_attempts=EXPORT_FULL_FETCH_ATTEMPTS,
            )
        ]
        special_use, records_by_thread = fetch_imap_thread_contexts(client, source_records)
        all_mailbox = special_use.get(r"\All", "")
        sent_mailbox = special_use.get(r"\Sent", "")
        records = source_records
    finally:
        logout_mailbox(client)
    thread_digests = write_thread_context(out_dir, records_by_thread, sender_email, recipient_email)
    for record in records:
        write_private(out_dir / f"{record.uid}.txt", export_body(record))
    write_private(out_dir / "mailboxes.tsv", f"role\tmailbox\nINBOX\tINBOX\n\\All\t{tsv_value(all_mailbox)}\n\\Sent\t{tsv_value(sent_mailbox)}\n")
    write_private(out_dir / "manifest.tsv", export_manifest(records, thread_digests))
    write_private(out_dir / "batches.tsv", export_batches(records, args.threads_per_batch))
    write_private(
        out_dir / "run.tsv",
        f"fixed_start_utc\tsource_count\tthread_count\tthreads_per_batch\n{datetime.now(timezone.utc).isoformat()}\t{len(records)}\t{len(records_by_thread)}\t{args.threads_per_batch}\n",
    )
    write_private(out_dir / "uids.txt", "\n".join(record.uid for record in records) + ("\n" if records else ""))
    write_private_dir(out_dir / "claims")
    write_private_dir(out_dir / "intents")
    write_private_dir(out_dir / "outcomes")
    write_private_dir(out_dir / "recoveries")
    suffix = f" skipped_boundary_mismatch={len(skipped)}" if skipped else ""
    print(f"exported={len(records)}{suffix} out_dir={out_dir}")
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    print("mark-seen is retired for manager mail compression; use trash-superseded with an explicit superseded UID file", file=sys.stderr)
    return 2


def verify_post_move_imap(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> PostMoveVerification:
    """Verify each moved fixed-start source through the same IMAP session.

    Exact lookup from the selected Trash mailbox proves Trash membership because
    Gmail may omit its ``\\Trash`` system label from ``X-GM-LABELS``. Message-ID
    lookup excludes arrivals that were not in the frozen source set.
    """
    sources_by_thread: dict[str, dict[str, dict[str, str]]] = {}
    for source in source_map.values():
        sources_by_thread.setdefault(source["gmail_thrid"], {})[source["gmail_msgid"]] = source
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    verified_messages = 0
    verified_threads = 0
    changed_threads = 0
    failures = 0
    verified_records: list[MailRecord] = []
    for gmail_thrid, sources_by_id in sources_by_thread.items():
        thread_verified = 0
        for gmail_msgid, source in sources_by_id.items():
            try:
                uids = gmail_message_uids(client, gmail_msgid)
                if len(uids) != 1:
                    raise RuntimeError("moved source was absent or ambiguous in Trash")
                record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
                require_gmail_identities([record])
            except (imaplib.IMAP4.error, RuntimeError):
                failures += 1
                break
            if (
                not record_matches_reconciliation_location(record, source, "Trash")
                or not is_manager_record(record, sender_email, recipient_email)
            ):
                break
            thread_verified += 1
            verified_records.append(record)
        else:
            verified_messages += thread_verified
            verified_threads += 1
            continue
        changed_threads += 1
    return PostMoveVerification(
        same_mailbox=True,
        verified_message_count=verified_messages,
        verified_thread_count=verified_threads,
        changed_thread_count=changed_threads,
        imap_failure_count=failures,
        verified_records=tuple(verified_records),
    )


def verified_existing_trash_records(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> list[MailRecord]:
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    records: list[MailRecord] = []
    try:
        for source in source_map.values():
            uids = gmail_message_uids(client, source["gmail_msgid"])
            if len(uids) != 1:
                raise RuntimeError("interrupted source was absent or ambiguous in Trash")
            record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
            if not is_manager_record(record, sender_email, recipient_email) or not record_matches_reconciliation_location(record, source, "Trash"):
                raise RuntimeError("interrupted Trash source identity or content changed")
            records.append(record)
        return records
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def cmd_trash_superseded(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to move superseded mail to Trash without --yes", file=sys.stderr)
        return 2
    if args.uid_file is None:
        print("refusing without a private superseded UID file beside its source map", file=sys.stderr)
        return 2
    if args.uids.strip():
        print("refusing inline UIDs; trash-superseded uses only the reviewed private --uid-file", file=sys.stderr)
        return 2
    try:
        requested = parse_uids(args.uids, args.uid_file)
        if not requested:
            raise RuntimeError("superseded source list must not be empty")
        source_dir = args.source_dir
        if args.uid_file.parent.resolve() != source_dir.resolve():
            raise RuntimeError("superseded UID file must be inside the fixed-start source directory")
        source_map = export_source_map(source_dir, requested)
        if {source["gmail_thrid"] for source in source_map.values()} != {args.gmail_thrid}:
            raise RuntimeError("one Trash operation must contain sources from exactly one claimed thread")
        expected_mailboxes = export_mailboxes(source_dir)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal():
            raise RuntimeError("private source map has ambiguous UIDVALIDITY")
        expected_uidvalidity = next(iter(expected_uidvalidities))
        if bool(args.replacement_id) == bool(args.replacement_not_required):
            raise RuntimeError("record exactly one replacement identity or --replacement-not-required")
        replacement = args.replacement_id if args.replacement_id else "not-required"
        if tsv_value(replacement) != replacement:
            raise RuntimeError("replacement identity must be one line")
        _thread_rows, outcome_evidence, recovery_needed = prepare_thread_disposition(
            source_dir,
            args.batch_id,
            args.owner,
            args.gmail_thrid,
            set(requested),
            args.reason_file,
            args.task_evidence_file,
            replacement,
        )
        if recovery_needed:
            raise RuntimeError("Trash disposition cannot replace a different existing intent")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=False)
    try:
        sender_email, recipient_email = mail_boundary(config)
        if selected_uidvalidity(client) != expected_uidvalidity:
            print("refusing because INBOX UIDVALIDITY changed", file=sys.stderr)
            return 1
        if not mailbox_exists(client, TRASH_MAILBOX):
            print(f"refusing because mailbox is missing: {TRASH_MAILBOX}", file=sys.stderr)
            return 1
        current_mailboxes = special_use_mailboxes(client)
        if current_mailboxes.get(r"\All") != expected_mailboxes[r"\All"] or current_mailboxes.get(r"\Sent") != expected_mailboxes[r"\Sent"]:
            print("refusing because special-use mailbox identity changed", file=sys.stderr)
            return 1
        if args.replacement_id and not replacement_exists(
            client,
            expected_mailboxes[r"\All"],
            args.replacement_id,
            sender_email,
            recipient_email,
        ):
            print("refusing because the recorded replacement was not found in the recipient mailbox", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        already_trashed = set(requested) - set(still_in_inbox)
        try:
            existing_trash_records = verified_existing_trash_records(
                client,
                {uid: source_map[uid] for uid in requested if uid in already_trashed},
                sender_email,
                recipient_email,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        records = [replace(record, source_uidvalidity=expected_uidvalidity) for record in fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)]
        require_gmail_identities(records)
        if any(not is_manager_record(record, sender_email, recipient_email) for record in records):
            print("refusing boundary mismatch", file=sys.stderr)
            return 1
        if any(not record_matches_source_map(record, source_map[record.uid]) for record in records):
            print("refusing because source identity or content changed", file=sys.stderr)
            return 1
        thread_unchanged = reconciliation_thread_unchanged(
            client,
            expected_mailboxes[r"\All"],
            source_dir,
            args.gmail_thrid,
            existing_trash_records,
        )
        select_mailbox(client, "INBOX", readonly=False)
        if not thread_unchanged:
            print("refusing because complete Gmail thread context changed", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested) - already_trashed:
            print("refusing because a planned source changed during revalidation", file=sys.stderr)
            return 1
        final_records = [replace(record, source_uidvalidity=expected_uidvalidity) for record in fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)]
        require_gmail_identities(final_records)
        if any(not is_manager_record(record, sender_email, recipient_email) or not record_matches_source_map(record, source_map[record.uid]) for record in final_records):
            print("refusing because a planned source changed immediately before move", file=sys.stderr)
            return 1
        if existing_trash_records:
            try:
                existing_trash_records = verified_existing_trash_records(
                    client,
                    {uid: source_map[uid] for uid in requested if uid in already_trashed},
                    sender_email,
                    recipient_email,
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        thread_unchanged = reconciliation_thread_unchanged(
            client,
            expected_mailboxes[r"\All"],
            source_dir,
            args.gmail_thrid,
            existing_trash_records,
        )
        select_mailbox(client, "INBOX", readonly=False)
        if not thread_unchanged:
            print("refusing because complete Gmail thread context changed immediately before move", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested) - already_trashed:
            print("refusing because a planned source left INBOX immediately before move", file=sys.stderr)
            return 1
        if still_in_inbox:
            typ, _data = imap_uid(client, "move-reviewed-sources-to-trash", "MOVE", ",".join(still_in_inbox), imap_quoted(TRASH_MAILBOX))
            if typ != "OK":
                print(f"IMAP MOVE failed: {typ}", file=sys.stderr)
                return 1
        remaining = inbox_subset(client, requested)
        post_move = verify_post_move_imap(
            client,
            source_map,
            sender_email,
            recipient_email,
        )
        if remaining or post_move.verified_message_count != len(requested) or not post_move.complete:
            post_thread_unchanged = False
        else:
            post_thread_unchanged = reconciliation_thread_unchanged(
                client,
                expected_mailboxes[r"\All"],
                source_dir,
                args.gmail_thrid,
                list(post_move.verified_records),
            )
    finally:
        logout_mailbox(client)
    if not remaining and post_move.verified_message_count == len(requested) and post_move.complete and post_thread_unchanged:
        write_private_exclusive(source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", outcome_evidence)
    print(
        f"trash_superseded: requested={len(requested)} moved={len(still_in_inbox)}"
        f" already_not_in_inbox={len(requested) - len(still_in_inbox)} verify_remaining={len(remaining)}"
        f" verify_trash_count={post_move.verified_message_count}"
        f" verify_thread_count={post_move.verified_thread_count}"
        f" verify_changed_thread_count={post_move.changed_thread_count}"
        f" verify_imap_failure_count={post_move.imap_failure_count}"
        f" same_mailbox_after_move={int(post_move.same_mailbox)} permanent_deleted=0"
    )
    if remaining or post_move.verified_message_count != len(requested) or not post_move.complete or not post_thread_unchanged:
        print(f"remaining_inbox_count={len(remaining)}")
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    sub = arg_parser.add_subparsers(dest="cmd", required=True)
    identity_preflight = sub.add_parser("identity-preflight", help="Read-only aggregate Gmail identity preflight for manager mail.")
    identity_preflight.set_defaults(func=cmd_identity_preflight)
    snapshot = sub.add_parser("snapshot", help="Print manager mail headers and UIDs.")
    snapshot.set_defaults(func=cmd_snapshot)
    export = sub.add_parser("export", help="Export manager mail bodies into a private local directory.")
    export.add_argument("--out-dir", type=Path, required=True)
    export.add_argument("--threads-per-batch", type=int, default=DEFAULT_THREADS_PER_BATCH)
    export.set_defaults(func=cmd_export)
    claim = sub.add_parser("claim-batch", help="Atomically claim one fixed-start review batch.")
    claim.add_argument("--source-dir", type=Path, required=True)
    claim.add_argument("--batch-id", required=True)
    claim.add_argument("--owner", required=True)
    claim.set_defaults(func=cmd_claim_batch)
    retain = sub.add_parser("retain-thread", help="Record one claimed thread as retained without mailbox mutation.")
    retain.add_argument("--source-dir", type=Path, required=True)
    retain.add_argument("--batch-id", required=True)
    retain.add_argument("--owner", required=True)
    retain.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    retain.add_argument("--reason-file", type=Path, required=True)
    retain.add_argument("--task-evidence-file", type=Path, required=True)
    retain.set_defaults(func=cmd_retain_thread)
    reconcile = sub.add_parser("reconcile-intent", help="Read-only reconciliation of one interrupted immutable intent in INBOX or Trash.")
    reconcile.add_argument("--source-dir", type=Path, required=True)
    reconcile.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    reconcile.set_defaults(func=cmd_reconcile_intent)
    recover_trashed = sub.add_parser("recover-already-trashed", help="Read-only terminal recovery when every frozen intent source is already exact in Trash and only later thread context was added.")
    recover_trashed.add_argument("--source-dir", type=Path, required=True)
    recover_trashed.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    recover_trashed.set_defaults(func=cmd_recover_already_trashed)
    verify = sub.add_parser("verify-run", help="Reconcile final outcomes against only the immutable fixed-start set.")
    verify.add_argument("--source-dir", type=Path, required=True)
    verify.set_defaults(func=cmd_verify_run)
    mark_seen = sub.add_parser("mark-seen", help="Retired; use trash-superseded for manager mail compression.")
    mark_seen.add_argument("--uids", default="", help="Comma or whitespace separated UID list.")
    mark_seen.add_argument("--uid-file", type=Path)
    mark_seen.add_argument("--yes", action="store_true")
    mark_seen.set_defaults(func=cmd_mark_seen)
    trash = sub.add_parser("trash-superseded", help="Move an explicit superseded manager UID set from INBOX to Trash after replacement summaries are sent.")
    trash.add_argument("--uids", default="", help="Retired for this command; inline UIDs are refused and --uid-file is authoritative.")
    trash.add_argument("--uid-file", type=Path)
    trash.add_argument("--source-dir", type=Path, required=True)
    trash.add_argument("--batch-id", required=True)
    trash.add_argument("--owner", required=True)
    trash.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    trash.add_argument("--reason-file", type=Path, required=True)
    trash.add_argument("--task-evidence-file", type=Path, required=True)
    trash.add_argument("--yes", action="store_true")
    replacement = trash.add_mutually_exclusive_group(required=True)
    replacement.add_argument("--replacement-message-id", dest="replacement_id", default="")
    replacement.add_argument("--replacement-not-required", action="store_true")
    trash.set_defaults(func=cmd_trash_superseded)
    return arg_parser


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
