#!/usr/bin/env python3
"""Snapshot/export unread manager mail and trash explicit superseded UIDs."""
from __future__ import annotations

import argparse
import hashlib
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

from omo_manager.email_idle_watcher import LEGACY_MANAGER_SUBJECT_TOKENS, message_text, parse_env_config
from omo_manager.omo_email_config import configured_agent_mail, human_config_path

HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"
FULL_FETCH = "(BODY.PEEK[])"
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


def record_from_msg(uid: str, msg: Message, body: str = "") -> MailRecord:
    return MailRecord(
        uid=uid,
        date=str(msg.get("Date", "")).replace("\n", " "),
        sender=", ".join(str(value).replace("\n", " ") for value in msg.get_all("From", [])),
        to=", ".join(str(value).replace("\n", " ") for value in msg.get_all("To", [])),
        subject=str(msg.get("Subject", "")).replace("\n", " "),
        msgid_sha256=msgid_digest(msg),
        body=body.replace("\r\n", "\n"),
    )


def is_manager_record(record: MailRecord, sender_email: str, recipient_email: str) -> bool:
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


def fetch_msg(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str) -> Message:
    typ, data = client.uid("fetch", uid, fetch_expr)
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"IMAP fetch failed: uid={uid} typ={typ}")
    return BytesParser(policy=policy.default).parsebytes(data[0][1])


def fetch_records(client: imaplib.IMAP4_SSL, uids: list[str], with_body: bool) -> list[MailRecord]:
    records: list[MailRecord] = []
    for uid in uids:
        msg = fetch_msg(client, uid, FULL_FETCH if with_body else HEADER_FETCH)
        records.append(record_from_msg(uid, msg, message_text(msg) if with_body else ""))
    return records


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


def export_body(record: MailRecord) -> str:
    return f"UID: {record.uid}\nDate: {record.date}\nSubject: {record.subject}\nMessage-ID-SHA256: {record.msgid_sha256}\n\n{record.body.rstrip()}\n"


def cmd_export(args: argparse.Namespace) -> int:
    out_dir = args.out_dir
    ensure_empty_private_dir(out_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        header_records, skipped = accepted_manager_headers(client, manager_unread_uids(client, sender_email), sender_email, recipient_email)
        records = fetch_records(client, [record.uid for record in header_records], with_body=True)
    finally:
        client.logout()
    manifest_lines = ["uid\tdate\tmsgid_sha256\tbody_bytes\tsubject"]
    for record in records:
        write_private(out_dir / f"{record.uid}.txt", export_body(record))
        manifest_lines.append(f"{record.uid}\t{record.date}\t{record.msgid_sha256}\t{record.body_bytes}\t{record.subject}")
    write_private(out_dir / "manifest.tsv", "\n".join(manifest_lines) + "\n")
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
    try:
        requested = parse_uids(args.uids, args.uid_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=False)
    try:
        sender_email, recipient_email = mail_boundary(config)
        if not mailbox_exists(client, TRASH_MAILBOX):
            print(f"refusing because mailbox is missing: {TRASH_MAILBOX}", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        records = fetch_records(client, still_in_inbox, with_body=False)
        bad = [record.uid for record in records if not is_manager_record(record, sender_email, recipient_email)]
        if bad:
            print(f"refusing boundary mismatch uids={','.join(bad)}", file=sys.stderr)
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
        print(f"remaining_inbox_uids={','.join(remaining)}")
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
