#!/usr/bin/env python3
"""Adopt one already-delivered Gmail Sent message without sending mail."""
from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.email_idle_watcher import fetch_gmail_metadata
from omo_manager.omo_email_config import GMAIL_IMAP_HOST, AgentMailSettings, configured_agent_mail
from omo_manager.omo_manager_mail_compress import FULL_FETCH, fetch_msg_bytes, imap_mailbox_name, imap_quoted, imap_uid, logout_mailbox, select_mailbox, selected_uidvalidity
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata

SHA256_RE = re.compile(r"[0-9a-f]{64}")
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
SCHEMA = "omo-completion-mail-adoption/v1"
TASK_NAME = "transcription_sw.md"
TASK_TARGET = "wl:32"
TASK_MANAGER = "wl:1"
TASK_BLOCKER = "owner-authenticated reconciliation of already delivered completion email; no resend"
MESSAGE_ID = "<178815460436.2815805.14149274743602497510@gmail.com>"
THREAD_ROOT_MESSAGE_ID = "<178815432253.2784108.9648480549229137852@gmail.com>"
OUTCOME = "task done"


@dataclass(frozen=True)
class ProviderBinding:
    all_mail_uid: str
    all_mail_uidvalidity: str
    sent_mail_uid: str
    sent_mail_uidvalidity: str
    gmail_message_id: str
    gmail_thread_id: str
    internaldate_unix_ms: str
    raw_sha256: str
    body_sha256: str
    thread_message_ids: tuple[str, ...]


@dataclass(frozen=True)
class AdoptionBinding:
    task_sha256: str
    gmail_message_id: str
    gmail_thread_id: str
    internaldate_unix_ms: str
    raw_sha256: str
    body_sha256: str
    subject: str
    thread_message_ids: tuple[str, ...]
    all_mail_uidvalidity: str
    sent_mail_uidvalidity: str


@dataclass(frozen=True)
class Args:
    root: Path
    task_sha256: str
    binding: AdoptionBinding
    items: tuple[str, ...]
    output: Path


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def unique_message_uids(client: imaplib.IMAP4_SSL, mailbox: str, message_id: str) -> tuple[str, ...]:
    select_mailbox(client, mailbox, readonly=True)
    typ, data = imap_uid(client, f"completion-adoption search mailbox={mailbox}", "search", None, "HEADER", "Message-ID", imap_quoted(message_id))
    if typ != "OK":
        raise OSError(f"Gmail Message-ID search failed in {mailbox}")
    values = uid_values(data)
    if any(not value.isdecimal() for value in values) or len(values) != len(set(values)):
        raise OSError(f"Gmail Message-ID search was malformed in {mailbox}")
    return values


def uid_values(data: list[bytes | tuple[bytes, bytes]]) -> tuple[str, ...]:
    if len(data) != 1 or not isinstance(data[0], bytes):
        raise OSError("IMAP search returned an ambiguous response shape")
    return tuple(raw.decode("ascii") for raw in data[0].split())


def exact_special_use_mailboxes(client: imaplib.IMAP4_SSL) -> tuple[str, str]:
    typ, data = client.list()
    if typ != "OK":
        raise OSError("Gmail special-use mailbox LIST failed")
    matches: dict[str, list[str]] = {r"\All": [], r"\Sent": []}
    for raw in data:
        if not isinstance(raw, bytes):
            raise OSError("Gmail special-use mailbox LIST returned a non-bytes entry")
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise OSError("Gmail special-use mailbox LIST was not ASCII modified UTF-7") from exc
        match = re.fullmatch(r"\((?P<attributes>[^)]*)\)\s+(?:\"[^\"]*\"|NIL)\s+(?P<mailbox>.+)", line)
        if match is None:
            continue
        attributes = set(match.group("attributes").split())
        mailbox = imap_mailbox_name(match.group("mailbox"))
        for special_use in matches:
            if special_use in attributes:
                matches[special_use].append(mailbox)
    if any(len(values) != 1 for values in matches.values()) or matches[r"\All"][0] == matches[r"\Sent"][0]:
        raise OSError("Gmail requires exactly one distinct All Mail and Sent special-use mailbox")
    return matches[r"\All"][0], matches[r"\Sent"][0]


def exact_header(message: Message, name: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1:
        raise OSError(f"delivered message requires one exact {name} header")
    return " ".join(str(values[0]).split())


def exact_addresses(message: Message, name: str) -> tuple[str, ...]:
    values = [address.casefold() for _display, address in getaddresses([str(value) for value in message.get_all(name, [])]) if address]
    return tuple(values)


def message_text_bytes(message: Message) -> bytes:
    if message.is_multipart():
        matches = [part for part in message.walk() if part.get_content_type() == "text/plain" and not part.get_filename()]
        if len(matches) != 1:
            raise OSError("delivered message requires one unambiguous text/plain body")
        payload = matches[0].get_payload(decode=True)
    else:
        payload = message.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise OSError("delivered message body is unavailable")
    charset = message.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset).replace("\r\n", "\n").replace("\r", "\n").encode()
    except (LookupError, UnicodeDecodeError) as exc:
        raise OSError("delivered message body encoding is invalid") from exc


def metadata(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str, str]:
    observed = fetch_gmail_metadata(client, uid)
    if observed is None or observed[1] is None or observed[2] is None:
        raise OSError("Gmail provider identity is incomplete")
    gmail_message_id, gmail_thread_id, internaldate_unix_ms = observed
    assert gmail_thread_id is not None and internaldate_unix_ms is not None
    if not all(value.isdecimal() for value in (gmail_message_id, gmail_thread_id, internaldate_unix_ms)):
        raise OSError("Gmail provider identity is malformed")
    return gmail_message_id, gmail_thread_id, internaldate_unix_ms


def observe_provider_delivery(client: imaplib.IMAP4_SSL, settings: AgentMailSettings, binding: AdoptionBinding) -> ProviderBinding:
    all_mail, sent_mail = exact_special_use_mailboxes(client)
    all_uids = unique_message_uids(client, all_mail, MESSAGE_ID)
    all_mail_uidvalidity = selected_uidvalidity(client)
    sent_uids = unique_message_uids(client, sent_mail, MESSAGE_ID)
    sent_mail_uidvalidity = selected_uidvalidity(client)
    if len(all_uids) != 1 or len(sent_uids) != 1:
        raise OSError("delivered Message-ID is missing or ambiguous in Gmail All Mail or Sent")
    select_mailbox(client, all_mail, readonly=True)
    raw = fetch_msg_bytes(client, all_uids[0], FULL_FETCH)
    provider = metadata(client, all_uids[0])
    select_mailbox(client, sent_mail, readonly=True)
    sent_raw = fetch_msg_bytes(client, sent_uids[0], FULL_FETCH)
    sent_provider = metadata(client, sent_uids[0])
    if sent_raw != raw or sent_provider != provider:
        raise OSError("Gmail All Mail and Sent do not identify the same delivered message")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if (
        exact_header(message, "Message-ID") != MESSAGE_ID
        or exact_header(message, "In-Reply-To") != THREAD_ROOT_MESSAGE_ID
        or exact_header(message, "References") != THREAD_ROOT_MESSAGE_ID
        or exact_header(message, "Subject") != binding.subject
        or exact_addresses(message, "From") != (settings.agent_address.casefold(),)
        or exact_addresses(message, "To") != (settings.human_address.casefold(),)
    ):
        raise OSError("delivered message headers do not match the exact Human thread and mailbox boundary")
    select_mailbox(client, all_mail, readonly=True)
    typ, data = imap_uid(client, "completion-adoption thread search", "search", None, "X-GM-THRID", provider[1])
    thread_uids = uid_values(data) if typ == "OK" else ()
    if any(not uid.isdecimal() for uid in thread_uids) or len(thread_uids) != len(set(thread_uids)):
        raise OSError("Gmail thread search was missing, malformed, or ambiguous")
    thread: list[tuple[int, str]] = []
    for uid in thread_uids:
        thread_raw = fetch_msg_bytes(client, uid, FULL_FETCH)
        thread_message = BytesParser(policy=policy.default).parsebytes(thread_raw)
        thread_provider = metadata(client, uid)
        if thread_provider[1] != provider[1]:
            raise OSError("Gmail thread member changed provider thread identity")
        thread.append((int(thread_provider[2]), exact_header(thread_message, "Message-ID")))
    thread_message_ids = tuple(message_id for _date, message_id in sorted(thread))
    observed = ProviderBinding(
        all_uids[0],
        all_mail_uidvalidity,
        sent_uids[0],
        sent_mail_uidvalidity,
        provider[0],
        provider[1],
        provider[2],
        sha256(raw),
        sha256(message_text_bytes(message)),
        thread_message_ids,
    )
    expected = ProviderBinding(
        all_uids[0],
        binding.all_mail_uidvalidity,
        sent_uids[0],
        binding.sent_mail_uidvalidity,
        binding.gmail_message_id,
        binding.gmail_thread_id,
        binding.internaldate_unix_ms,
        binding.raw_sha256,
        binding.body_sha256,
        binding.thread_message_ids,
    )
    if observed != expected:
        raise OSError("authenticated Gmail Sent evidence drifted from the complete binding")
    return observed


# 🧑 Human source `manager_mail/85c5dff58359-1270.txt:1-7`: "Find me a good transcription software ... supporting Mac OS and Linux."
def validate_task(root: Path, task_sha256: str, items: tuple[str, ...]) -> tuple[Path, bytes]:
    task = root / TASK_NAME
    payload = task.read_bytes()
    if sha256(payload) != task_sha256:
        raise OSError("transcription task bytes do not match --task-sha256")
    try:
        metadata_value = parse_task_metadata(payload.decode(), root)
    except (TaskFrontmatterError, UnicodeDecodeError) as exc:
        raise OSError("transcription task frontmatter is invalid") from exc
    if (
        metadata_value is None
        or metadata_value.version != "v1.0.0"
        or metadata_value.status != "blocked"
        or metadata_value.blocked_on != TASK_BLOCKER
        or metadata_value.runat != TASK_TARGET
        or metadata_value.managerat != TASK_MANAGER
        or metadata_value.is_manager
        or metadata_value.pending_task_items != items
        or not items
    ):
        raise OSError("transcription task type, status, target, or complete ordered queue drifted")
    return task, payload


def receipt(args: Args, provider: ProviderBinding) -> bytes:
    value = {
        "schema": SCHEMA,
        "root": str(args.root),
        "task": TASK_NAME,
        "task_sha256": args.task_sha256,
        "owner": TASK_TARGET,
        "outcome": OUTCOME,
        "pending_task_items": list(args.items),
        "mail_policy": "already-delivered-no-resend",
        "message_id": MESSAGE_ID,
        "thread_root_message_id": THREAD_ROOT_MESSAGE_ID,
        "subject": args.binding.subject,
        "provider": "gmail-agent-sent",
        **asdict(provider),
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_private_exclusive(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise OSError("adoption receipt output must be an absolute file")
    parent = path.parent
    state = parent.stat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o077:
        raise OSError("adoption receipt directory must be owner-private")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def adopt(args: Args) -> None:
    task = args.root / TASK_NAME
    with task_file_lock(task):
        _task, before = validate_task(args.root, args.task_sha256, args.items)
        settings = configured_agent_mail()
        if settings is None:
            raise OSError("dedicated agent Gmail configuration is unavailable")
        client = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=30)
        try:
            client.login(settings.agent_address, settings.app_password)
            typ, _data = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise OSError("cannot open authenticated agent Gmail read-only")
            provider = observe_provider_delivery(client, settings, args.binding)
            if observe_provider_delivery(client, settings, args.binding) != provider:
                raise OSError("authenticated Gmail Sent evidence changed during adoption")
        finally:
            try:
                logout_mailbox(client)
            except RuntimeError:
                pass
        if task.read_bytes() != before:
            raise OSError("transcription task changed during Gmail Sent verification")
        write_private_exclusive(args.output, receipt(args, provider))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-sha256", required=True)
    _ = parser.add_argument("--gmail-message-id", required=True)
    _ = parser.add_argument("--gmail-thread-id", required=True)
    _ = parser.add_argument("--internaldate-unix-ms", required=True)
    _ = parser.add_argument("--raw-sha256", required=True)
    _ = parser.add_argument("--body-sha256", required=True)
    _ = parser.add_argument("--subject", required=True)
    _ = parser.add_argument("--all-mail-uidvalidity", required=True)
    _ = parser.add_argument("--sent-mail-uidvalidity", required=True)
    _ = parser.add_argument("--thread-message-id", action="append", default=[])
    _ = parser.add_argument("--item", action="append", default=[])
    _ = parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(argv)
    hashes = (parsed.task_sha256, parsed.raw_sha256, parsed.body_sha256)
    decimals = (parsed.gmail_message_id, parsed.gmail_thread_id, parsed.internaldate_unix_ms, parsed.all_mail_uidvalidity, parsed.sent_mail_uidvalidity)
    thread_ids = tuple(parsed.thread_message_id)
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        parser.error("task, raw MIME, and body SHA-256 bindings must be lowercase hexadecimal")
    if any(not value.isdecimal() for value in decimals):
        parser.error("Gmail message, thread, and internal-date bindings must be decimal")
    if not thread_ids or any(MESSAGE_ID_RE.fullmatch(value) is None for value in thread_ids) or len(thread_ids) != len(set(thread_ids)):
        parser.error("thread Message-ID bindings must be nonempty, exact, and unique")
    if THREAD_ROOT_MESSAGE_ID not in thread_ids or MESSAGE_ID not in thread_ids:
        parser.error("thread bindings must contain the exact root and delivered Message-ID")
    if not parsed.item or any(not item.strip() or item.strip() != item for item in parsed.item):
        parser.error("--item must repeat the complete nonempty ordered queue")
    return Args(
        parsed.root.resolve(),
        parsed.task_sha256,
        AdoptionBinding(
            parsed.task_sha256,
            parsed.gmail_message_id,
            parsed.gmail_thread_id,
            parsed.internaldate_unix_ms,
            parsed.raw_sha256,
            parsed.body_sha256,
            parsed.subject,
            thread_ids,
            parsed.all_mail_uidvalidity,
            parsed.sent_mail_uidvalidity,
        ),
        tuple(parsed.item),
        parsed.output,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        adopt(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, TaskFrontmatterError, UnicodeDecodeError, ValueError, imaplib.IMAP4.error) as exc:
        print(f"omo_completion_mail_adopt.py: {exc}", file=sys.stderr)
        return 2
    print("Adopted exact authenticated Gmail Sent delivery; no mail was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
