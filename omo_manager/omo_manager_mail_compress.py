#!/usr/bin/env python3
"""Snapshot/export unread manager mail and mark an explicit UID set read."""
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
from email.utils import parseaddr
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.email_idle_watcher import CONFIG_PATH, message_text, parse_env_config

MANAGER_SUBJECT_TOKEN = "[omo_manager]"
HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"
FULL_FETCH = "(BODY.PEEK[])"


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
        sender=str(msg.get("From", "")).replace("\n", " "),
        to=str(msg.get("To", "")).replace("\n", " "),
        subject=str(msg.get("Subject", "")).replace("\n", " "),
        msgid_sha256=msgid_digest(msg),
        body=body.replace("\r\n", "\n"),
    )


def is_manager_record(record: MailRecord, self_email: str) -> bool:
    return parseaddr(record.sender)[1].lower() == self_email.lower() and MANAGER_SUBJECT_TOKEN.lower() in record.subject.lower()


def load_config() -> dict[str, str]:
    config = parse_env_config(CONFIG_PATH)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        raise RuntimeError(f"missing email config keys in {CONFIG_PATH}: {sorted(missing)}")
    return config


def manager_unread_uids(client: imaplib.IMAP4_SSL, self_email: str) -> list[str]:
    typ, data = client.uid("search", None, "UNSEEN", "FROM", f'"{self_email}"', "SUBJECT", f'"{MANAGER_SUBJECT_TOKEN}"')
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def unread_subset(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[str]:
    if not uids:
        return []
    typ, data = client.uid("search", None, "UNSEEN", "UID", ",".join(uids))
    if typ != "OK":
        raise RuntimeError(f"IMAP unread UID search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


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


def accepted_manager_headers(client: imaplib.IMAP4_SSL, uids: list[str], self_email: str) -> tuple[list[MailRecord], list[str]]:
    records = fetch_records(client, uids, with_body=False)
    accepted = [record for record in records if is_manager_record(record, self_email)]
    skipped = [record.uid for record in records if not is_manager_record(record, self_email)]
    return accepted, skipped


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
        uids = manager_unread_uids(client, config["user"])
        records, skipped = accepted_manager_headers(client, uids, config["user"])
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
        header_records, skipped = accepted_manager_headers(client, manager_unread_uids(client, config["user"]), config["user"])
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
    if not args.yes:
        print("refusing to mark mail read without --yes", file=sys.stderr)
        return 2
    try:
        requested = parse_uids(args.uids, args.uid_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=False)
    try:
        still_unread = unread_subset(client, requested)
        records = fetch_records(client, still_unread, with_body=False)
        bad = [record.uid for record in records if not is_manager_record(record, config["user"])]
        if bad:
            print(f"refusing boundary mismatch uids={','.join(bad)}", file=sys.stderr)
            return 1
        if still_unread:
            typ, _data = client.uid("store", ",".join(still_unread), "+FLAGS", r"(\Seen)")
            if typ != "OK":
                print(f"IMAP STORE failed: {typ}", file=sys.stderr)
                return 1
        remaining = unread_subset(client, requested)
    finally:
        client.logout()
    print(f"mark_seen: requested={len(requested)} marked={len(still_unread)} already_not_unread={len(requested) - len(still_unread)} verify_remaining={len(remaining)}")
    if remaining:
        print(f"remaining_unread_uids={','.join(remaining)}")
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
    mark_seen = sub.add_parser("mark-seen", help="Mark an explicit unread manager UID set read after replacement summaries are sent.")
    mark_seen.add_argument("--uids", default="", help="Comma or whitespace separated UID list.")
    mark_seen.add_argument("--uid-file", type=Path)
    mark_seen.add_argument("--yes", action="store_true")
    mark_seen.set_defaults(func=cmd_mark_seen)
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
