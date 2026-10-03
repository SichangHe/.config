"""Email between agents and the human.

Agents send from the agent Gmail account to the human's address. The human's replies arrive in the agent account's inbox.
Whether the human has read an agent's email is only visible in the human's own mailbox, which is read through the himalaya config.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from amh.config import Config

IMAP_HOST = "imap.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'
TRASH = '"[Gmail]/Trash"'
HEADERS = "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID REFERENCES DATE)])"


@dataclass(frozen=True)
class Header:
    uid: str
    subject: str
    message_id: str
    references: str
    date: str


@contextmanager
def agent_box(config: Config) -> Iterator[imaplib.IMAP4_SSL]:
    """Log in to the agent account, which sends agent mail and receives the human's replies."""
    box = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
    _ = box.login(config.get("OMO_AGENT_GMAIL_ADDRESS"), config.get("OMO_AGENT_GMAIL_APP_PASSWORD"))
    try:
        yield box
    finally:
        box.shutdown()


@contextmanager
def human_box(config: Config) -> Iterator[imaplib.IMAP4_SSL]:
    """Log in to the human's mailbox, where agent mail is unread until the human opens it."""
    path = Path(config.get("OMO_EMAIL_CONFIG_PATH", str(Path.home() / ".config/himalaya/config.toml")))
    host, login, command = (re.search(rf'^{key} = "(.*)"$', path.read_text(encoding="utf-8"), re.MULTILINE)[1] for key in ("host", "login", "cmd"))
    password = subprocess.run(command, shell=True, capture_output=True, text=True, check=True).stdout.strip()
    box = imaplib.IMAP4_SSL(host, timeout=60)
    _ = box.login(login, password)
    try:
        yield box
    finally:
        box.shutdown()


def search(box: imaplib.IMAP4_SSL, folder: str, readonly: bool, *criteria: str) -> list[Header]:
    """Return the headers of matching mail in `folder`, oldest first."""
    _ = box.select(folder, readonly=readonly)
    uids = box.uid("search", *criteria)[1][0].split()
    if not uids:
        return []
    found = []
    for part in box.uid("fetch", b",".join(uids), HEADERS)[1]:
        if not isinstance(part, tuple):
            continue
        parsed = message_from_bytes(part[1], policy=policy.default)
        uid = re.search(rb"UID (\d+)", part[0])
        found.append(Header(uid[1].decode() if uid else "", str(parsed["Subject"] or ""), str(parsed["Message-ID"] or "").strip(), " ".join(str(parsed["References"] or "").split()), str(parsed["Date"] or "")))
    return found


def raw(query: str) -> tuple[str, str]:
    """Build a Gmail search criterion."""
    return "X-GM-RAW", '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bare(subject: str) -> str:
    """Strip reply prefixes and `[tag]`s from the front of a subject."""
    return re.sub(r"^(\s*(re|fwd?):\s*|\s*\[[^\]]*\]\s*)+", "", subject, flags=re.IGNORECASE).strip()


def send(config: Config, tag: str, subject: str, body: str, sender: str) -> str:
    """Email the human on the tag's thread and return the new Message-ID.

    An empty `subject` reuses the subject of the newest email carrying the tag.
    """
    # 🧑 "Let email_me.py prefer the task file name in the subject tag. I find it easier"
    # 🧑 "pending item created/deleted messages should reuse subject of the agent’s previous email"
    title = bare(subject)
    if len(title) >= 60:
        raise SystemExit("amh: the subject must be under 60 characters")
    with agent_box(config) as box:
        thread = [h for h in search(box, ALL_MAIL, True, *raw(f'subject:"[{tag}]"')) if tag_of(h.subject) == tag and (not title or bare(h.subject) == title)]
    title = title or (bare(thread[-1].subject) if thread else tag)
    message = EmailMessage()
    message["From"] = config.get("OMO_AGENT_GMAIL_ADDRESS")
    message["To"] = config.get("OMO_HUMAN_EMAIL_ADDRESS")
    message["Subject"] = f"{'Re: ' if thread else ''}[{tag}] {title}"
    message["Message-ID"] = make_msgid(domain="gmail.com")
    message["X-AMH-From"] = sender
    if thread:
        message["In-Reply-To"] = thread[-1].message_id
        message["References"] = f"{thread[-1].references} {thread[-1].message_id}".strip()
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        _ = smtp.login(config.get("OMO_AGENT_GMAIL_ADDRESS"), config.get("OMO_AGENT_GMAIL_APP_PASSWORD"))
        _ = smtp.send_message(message)
    return str(message["Message-ID"])


def unread(config: Config, tag: str, trash_ids: list[str]) -> list[Header]:
    """List the tag's emails the human has not opened; first move those named in `trash_ids` to the trash."""
    # 🧑 “Trash the emails that I no longer need to read that are not read yet.”
    with human_box(config) as box:
        found = search(box, "INBOX", not trash_ids, "UNSEEN", "FROM", f'"{config.get("OMO_AGENT_GMAIL_ADDRESS")}"', "SUBJECT", f'"[{tag}]"')
        doomed = [h for h in found if h.message_id in trash_ids]
        if doomed:
            _ = box.uid("move", ",".join(h.uid for h in doomed), TRASH)
        return [h for h in found if h not in doomed]


@dataclass(frozen=True)
class Incoming:
    uid: str
    subject: str
    body: str


def fetch_new(box: imaplib.IMAP4_SSL, config: Config) -> list[Incoming]:
    """Return the human's unread emails in the agent inbox, without marking them read."""
    _ = box.select("INBOX")
    uids = box.uid("search", "UNSEEN", "FROM", f'"{config.get("OMO_HUMAN_EMAIL_ADDRESS")}"')[1][0].split()
    mails = []
    for uid in uids:
        parsed = message_from_bytes(box.uid("fetch", uid, "(BODY.PEEK[])")[1][0][1], policy=policy.default)
        part = parsed.get_body(preferencelist=("plain", "html"))
        try:
            body = part.get_content() if part else ""
        except (LookupError, ValueError):
            body = (part.get_payload(decode=True) or b"").decode(errors="replace")
        mails.append(Incoming(uid.decode(), " ".join(str(parsed["Subject"] or "").split()), body))
    return mails


def mark_read(box: imaplib.IMAP4_SSL, uid: str) -> None:
    _ = box.uid("store", uid, "+FLAGS", "(\\Seen)")


def tag_of(subject: str) -> str | None:
    """Return the first `[tag]` at the front of a subject, ignoring reply prefixes."""
    match = re.match(r"^(?:\s*(?:re|fwd?):\s*)*\[([A-Za-z0-9_-]+)\]", subject, re.IGNORECASE)
    return match[1] if match else None
