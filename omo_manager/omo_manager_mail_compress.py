#!/usr/bin/env python3
"""Snapshot/export unread manager mail and trash explicit superseded UIDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import imaplib
import os
import re
import sys
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.email_idle_watcher import LEGACY_MANAGER_SUBJECT_TOKENS, is_mail_cleanup_excluded_subject, message_text, parse_env_config
from omo_manager.omo_email_config import configured_agent_mail, human_config_path

HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"
FULL_FETCH = "(BODY.PEEK[])"
GMAIL_METADATA_FETCH = "(FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS)"
TRASH_MAILBOX = "[Gmail]/Trash"
CONFIG_PATH = human_config_path()


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

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode())


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
    msgid = str(msg.get("Message-ID", ""))
    return hashlib.sha256(msgid.encode()).hexdigest()[:12] if msgid else "no-msgid"


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


def manager_unread_uids(client: imaplib.IMAP4_SSL, self_email: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    settings = configured_agent_mail()
    criteria = ["UNSEEN", "FROM", f'"{self_email}"']
    subject_tokens = ("",) if settings is not None else LEGACY_MANAGER_SUBJECT_TOKENS
    for token in subject_tokens:
        typ, data = client.uid("search", None, *(criteria + (["SUBJECT", f'"{token}"'] if token else [])))
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for uid in [raw.decode() for raw in data[0].split()] if data and data[0] else []:
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def unread_subset(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[str]:
    if not uids:
        return []
    typ, data = client.uid("search", None, "UNSEEN", "UID", ",".join(uids))
    if typ != "OK":
        raise RuntimeError(f"IMAP unread UID search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def inbox_subset(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[str]:
    if not uids:
        return []
    typ, data = client.uid("search", None, "UID", ",".join(uids))
    if typ != "OK":
        raise RuntimeError(f"IMAP UID search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def mailbox_exists(client: imaplib.IMAP4_SSL, mailbox: str) -> bool:
    typ, data = client.list()
    if typ != "OK":
        raise RuntimeError(f"IMAP mailbox list failed: {typ}")
    return any(mailbox.encode() in raw for raw in data if isinstance(raw, bytes))


def fetch_msg_bytes(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str) -> bytes:
    typ, data = client.uid("fetch", uid, fetch_expr)
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"IMAP fetch failed: uid={uid} typ={typ}")
    payload = data[0][1]
    if not isinstance(payload, bytes):
        raise RuntimeError(f"IMAP fetch payload was not bytes: uid={uid}")
    return payload


def fetch_msg(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str) -> tuple[Message, str]:
    payload = fetch_msg_bytes(client, uid, fetch_expr)
    return BytesParser(policy=policy.default).parsebytes(payload), hashlib.sha256(payload).hexdigest()


def imap_response_text(data: list[bytes | tuple[bytes, bytes]]) -> str:
    chunks: list[bytes] = []
    for item in data:
        if isinstance(item, bytes):
            chunks.append(item)
        elif isinstance(item, tuple):
            chunks.extend(part for part in item if isinstance(part, bytes))
    return b" ".join(chunks).decode("utf-8", errors="replace")


def imap_parenthesized_value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|[\s(]){re.escape(key)}\s+\(", text)
    if match is None:
        return ""
    index = match.end()
    start = index
    depth = 1
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
                return " ".join(text[start:index].split())
        index += 1
    return ""


def imap_numeric_value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}\s+([0-9]+)(?=\s|\))", text)
    return match.group(1) if match is not None else ""


def fetch_gmail_metadata(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str, str, str]:
    typ, data = client.uid("fetch", uid, GMAIL_METADATA_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail metadata fetch failed: uid={uid} typ={typ}")
    response = imap_response_text(data)
    return (
        imap_numeric_value(response, "X-GM-MSGID"),
        imap_numeric_value(response, "X-GM-THRID"),
        imap_parenthesized_value(response, "FLAGS"),
        imap_parenthesized_value(response, "X-GM-LABELS"),
    )


def fetch_record(client: imaplib.IMAP4_SSL, uid: str, with_body: bool, with_metadata: bool) -> MailRecord:
    msg, raw_sha256 = fetch_msg(client, uid, FULL_FETCH if with_body else HEADER_FETCH)
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


def fetch_records(client: imaplib.IMAP4_SSL, uids: list[str], with_body: bool, with_metadata: bool = False) -> list[MailRecord]:
    return [fetch_record(client, uid, with_body, with_metadata) for uid in uids]


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
    client = imaplib.IMAP4_SSL(config["host"], timeout=45)
    try:
        client.login(config["user"], config["password"])
        typ, _data = client.select("INBOX", readonly=readonly)
        if typ != "OK":
            raise RuntimeError(f"IMAP select INBOX failed: {typ}")
    except Exception:
        client.logout()
        raise
    return client, config


def select_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, readonly: bool) -> None:
    typ, _data = client.select(imap_quoted(mailbox), readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"IMAP select failed: mailbox={mailbox} typ={typ}")


def imap_mailbox_name(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def special_use_mailboxes(client: imaplib.IMAP4_SSL) -> dict[str, str]:
    typ, data = client.list()
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
    typ, data = client.uid("search", None, "X-GM-THRID", gmail_thrid)  # type: ignore[arg-type]
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail thread search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def require_gmail_identities(records: list[MailRecord]) -> None:
    missing = [record.uid for record in records if not record.gmail_msgid or not record.gmail_thrid or not record.raw_sha256]
    if missing:
        raise RuntimeError(f"Gmail identity metadata missing for {len(missing)} source messages")


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


def cmd_snapshot(_args: argparse.Namespace) -> int:
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        uids = manager_unread_uids(client, sender_email)
        records, skipped = accepted_manager_headers(client, uids, sender_email, recipient_email)
        print(f"unread_manager_count={len(records)}")
        if skipped:
            print(f"skipped_boundary_mismatch={len(skipped)}")
        print_records(records)
    finally:
        client.logout()
    return 0


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


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


def read_tsv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
                raise RuntimeError(f"private source map is missing required fields: {path.name}")
            return [{key: value or "" for key, value in row.items()} for row in reader]
    except OSError as exc:
        raise RuntimeError(f"could not read private source map: {path.name}") from exc


def export_source_map(out_dir: Path, requested: list[str]) -> dict[str, dict[str, str]]:
    rows = read_tsv(
        out_dir / "manifest.tsv",
        {
            "uid",
            "source_mailbox",
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
    if len(source_map) != len(rows) or any(uid not in source_map for uid in requested):
        raise RuntimeError("requested sources were absent or ambiguous in the private source map")
    return {uid: source_map[uid] for uid in requested}


def export_mailboxes(out_dir: Path) -> dict[str, str]:
    rows = read_tsv(out_dir / "mailboxes.tsv", {"role", "mailbox"})
    mailboxes = {row["role"]: row["mailbox"] for row in rows if row["role"] and row["mailbox"]}
    if r"\All" not in mailboxes or r"\Sent" not in mailboxes:
        raise RuntimeError("private source map lacks required special-use mailboxes")
    return mailboxes


def record_matches_source_map(record: MailRecord, source: dict[str, str]) -> bool:
    return (
        source["source_mailbox"] == "INBOX"
        and record.gmail_msgid == source["gmail_msgid"]
        and record.gmail_thrid == source["gmail_thrid"]
        and record.msgid_sha256 == source["msgid_sha256"]
        and record.raw_sha256 == source["raw_sha256"]
        and tsv_value(record.flags) == source["flags"]
        and tsv_value(record.labels) == source["labels"]
    )


def record_has_protected_intent(record: MailRecord) -> bool:
    flags = record.flags.casefold().split()
    labels = record.labels.casefold()
    return r"\flagged" in flags or any(token in labels for token in (r"\flagged", r"\starred", r"\important", "read later", "saved"))


def revalidate_thread_contexts(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    source_map: dict[str, dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> bool:
    source_ids_by_thread: dict[str, set[str]] = {}
    for source in source_map.values():
        source_ids_by_thread.setdefault(source["gmail_thrid"], set()).add(source["gmail_msgid"])
    select_mailbox(client, all_mailbox, readonly=True)
    try:
        for gmail_thrid, source_ids in source_ids_by_thread.items():
            records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
            require_gmail_identities(records)
            if (
                not records
                or any(record.gmail_thrid != gmail_thrid for record in records)
                or {record.gmail_msgid for record in records} != source_ids
                or any(not is_manager_record(record, sender_email, recipient_email) or record_has_protected_intent(record) for record in records)
            ):
                return False
            expected_digests = {source["thread_context_sha256"] for source in source_map.values() if source["gmail_thrid"] == gmail_thrid}
            if len(expected_digests) != 1:
                return False
            expected = expected_digests.pop()
            if thread_context_digest(records) != expected:
                return False
        return True
    finally:
        select_mailbox(client, "INBOX", readonly=False)


def cmd_export(args: argparse.Namespace) -> int:
    out_dir = args.out_dir
    ensure_empty_private_dir(out_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        header_records, skipped = accepted_manager_headers(client, manager_unread_uids(client, sender_email), sender_email, recipient_email)
        records = fetch_records(client, [record.uid for record in header_records], with_body=True, with_metadata=True)
        require_gmail_identities(records)
        special_use = special_use_mailboxes(client)
        all_mailbox = special_use.get(r"\All", "")
        sent_mailbox = special_use.get(r"\Sent", "")
        if not all_mailbox:
            raise RuntimeError("Gmail All Mail special-use mailbox was not discovered")
        if not sent_mailbox:
            raise RuntimeError("Gmail Sent special-use mailbox was not discovered")
        records_by_thread: dict[str, list[MailRecord]] = {}
        source_ids_by_thread: dict[str, set[str]] = {}
        for record in records:
            source_ids_by_thread.setdefault(record.gmail_thrid, set()).add(record.gmail_msgid)
        select_mailbox(client, all_mailbox, readonly=True)
        for gmail_thrid in sorted({record.gmail_thrid for record in records}):
            context_records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
            require_gmail_identities(context_records)
            if (
                not context_records
                or any(record.gmail_thrid != gmail_thrid for record in context_records)
                or not source_ids_by_thread[gmail_thrid].issubset({record.gmail_msgid for record in context_records})
            ):
                raise RuntimeError("Gmail thread context was incomplete or changed during export")
            records_by_thread[gmail_thrid] = context_records
    finally:
        client.logout()
    thread_digests = write_thread_context(out_dir, records_by_thread, sender_email, recipient_email)
    for record in records:
        write_private(out_dir / f"{record.uid}.txt", export_body(record))
    write_private(out_dir / "mailboxes.tsv", f"role\tmailbox\nINBOX\tINBOX\n\\All\t{tsv_value(all_mailbox)}\n\\Sent\t{tsv_value(sent_mailbox)}\n")
    write_private(out_dir / "manifest.tsv", export_manifest(records, thread_digests))
    write_private(out_dir / "uids.txt", "\n".join(record.uid for record in records) + ("\n" if records else ""))
    suffix = f" skipped_boundary_mismatch={len(skipped)}" if skipped else ""
    print(f"exported={len(records)}{suffix} out_dir={out_dir}")
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    print("mark-seen is retired for manager mail compression; use trash-superseded with an explicit superseded UID file", file=sys.stderr)
    return 2


def cmd_trash_superseded(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to move superseded mail to Trash without --yes", file=sys.stderr)
        return 2
    if args.uid_file is None:
        print("refusing without a private superseded UID file beside its source map", file=sys.stderr)
        return 2
    try:
        requested = parse_uids(args.uids, args.uid_file)
        source_dir = args.uid_file.parent
        source_map = export_source_map(source_dir, requested)
        expected_mailboxes = export_mailboxes(source_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=False)
    try:
        sender_email, recipient_email = mail_boundary(config)
        if not mailbox_exists(client, TRASH_MAILBOX):
            print(f"refusing because mailbox is missing: {TRASH_MAILBOX}", file=sys.stderr)
            return 1
        current_mailboxes = special_use_mailboxes(client)
        if current_mailboxes.get(r"\All") != expected_mailboxes[r"\All"] or current_mailboxes.get(r"\Sent") != expected_mailboxes[r"\Sent"]:
            print("refusing because special-use mailbox identity changed", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested):
            print("refusing because a planned source left INBOX", file=sys.stderr)
            return 1
        records = fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)
        if any(not is_manager_record(record, sender_email, recipient_email) for record in records):
            print("refusing boundary mismatch", file=sys.stderr)
            return 1
        if any(record_has_protected_intent(record) for record in records):
            print("refusing protected flagged, saved, or read-later source", file=sys.stderr)
            return 1
        if any(not record_matches_source_map(record, source_map[record.uid]) for record in records):
            print("refusing because source identity, flags, labels, or content changed", file=sys.stderr)
            return 1
        if not revalidate_thread_contexts(client, expected_mailboxes[r"\All"], source_map, sender_email, recipient_email):
            print("refusing because complete Gmail thread context changed", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested):
            print("refusing because a planned source changed during revalidation", file=sys.stderr)
            return 1
        final_records = fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)
        if any(not is_manager_record(record, sender_email, recipient_email) or not record_matches_source_map(record, source_map[record.uid]) for record in final_records):
            print("refusing because a planned source changed immediately before move", file=sys.stderr)
            return 1
        if not revalidate_thread_contexts(client, expected_mailboxes[r"\All"], source_map, sender_email, recipient_email):
            print("refusing because complete Gmail thread context changed immediately before move", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested):
            print("refusing because a planned source left INBOX immediately before move", file=sys.stderr)
            return 1
        if still_in_inbox:
            typ, _data = client.uid("MOVE", ",".join(still_in_inbox), imap_quoted(TRASH_MAILBOX))
            if typ != "OK":
                print(f"IMAP MOVE failed: {typ}", file=sys.stderr)
                return 1
        remaining = inbox_subset(client, requested)
    finally:
        client.logout()
    print(f"trash_superseded: requested={len(requested)} moved={len(still_in_inbox)} already_not_in_inbox={len(requested) - len(still_in_inbox)} verify_remaining={len(remaining)}")
    if remaining:
        print(f"remaining_inbox_count={len(remaining)}")
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    sub = arg_parser.add_subparsers(dest="cmd", required=True)
    snapshot = sub.add_parser("snapshot", help="Print unread manager mail headers and UIDs.")
    snapshot.set_defaults(func=cmd_snapshot)
    export = sub.add_parser("export", help="Export unread manager mail bodies into a private local directory.")
    export.add_argument("--out-dir", type=Path, required=True)
    export.set_defaults(func=cmd_export)
    mark_seen = sub.add_parser("mark-seen", help="Retired; use trash-superseded for manager mail compression.")
    mark_seen.add_argument("--uids", default="", help="Comma or whitespace separated UID list.")
    mark_seen.add_argument("--uid-file", type=Path)
    mark_seen.add_argument("--yes", action="store_true")
    mark_seen.set_defaults(func=cmd_mark_seen)
    trash = sub.add_parser("trash-superseded", help="Move an explicit superseded manager UID set from INBOX to Trash after replacement summaries are sent.")
    trash.add_argument("--uids", default="", help="Comma or whitespace separated UID list.")
    trash.add_argument("--uid-file", type=Path)
    trash.add_argument("--yes", action="store_true")
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
