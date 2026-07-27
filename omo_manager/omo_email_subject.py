#!/usr/bin/env python3
"""Prepare manager-human email subjects."""
from __future__ import annotations

import argparse
import imaplib
import os
import re
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

try:
    from .omo_email_config import GMAIL_IMAP_HOST, configured_agent_mail, human_config_path
except ImportError:
    from omo_email_config import GMAIL_IMAP_HOST, configured_agent_mail, human_config_path

SHORT_MANAGER_TAG = "[a]"
CONFIG_PATH = human_config_path()
SUBJECT_TAG_RE = re.compile(r"^\s*\[(?:a|omo_manager|omo)\]\s*", re.IGNORECASE)
MANAGER_TAG_RE = re.compile(r"^\s*(?:\[a\]|\[omo_manager\])\s*", re.IGNORECASE)
RESERVED_AGENT_TAG_RE = re.compile(r"^(?:re:\s*)*\[omo\]\s*", re.IGNORECASE)
RE_PREFIX_RE = re.compile(r"^\s*re:\s*", re.IGNORECASE)
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
TMUX_SUBJECT_TAG_RE = re.compile(r"^\s*(?:\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]|[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?:\s+|$)")
PLACEHOLDER_RE = re.compile(r"subject\W*", re.IGNORECASE)
DEFAULT_THREAD_LOOKUP_WINDOW_S = 3 * 24 * 60 * 60
DEFAULT_THREAD_LOOKUP_DEADLINE_S = 5.0


class SubjectInputError(ValueError):
    pass


class SubjectLookupTimeout(Exception):
    pass


@dataclass(frozen=True)
class RecentHeader:
    sender: str
    subject: str
    date: datetime | None
    message_id: str = ""
    references: str = ""


def parse_env_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    section = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        value = raw_value.strip().strip('"').strip("'")
        key = key.strip()
        if section == "accounts.gmail.backend" and key in {"host", "login"}:
            values["host" if key == "host" else "user"] = value
        if section == "accounts.gmail.backend.auth" and key == "cmd" and "echo" in value:
            parts = value.split("'")
            if len(parts) >= 2:
                values["password"] = parts[1]
    return values


def strip_re_prefixes(subject: str) -> str:
    text = subject.strip()
    while True:
        next_text = RE_PREFIX_RE.sub("", text, count=1)
        if next_text == text:
            return text
        text = next_text.strip()


def normalized_subject_key(subject: str) -> str:
    text = subject.strip()
    while True:
        before = text
        text = RE_PREFIX_RE.sub("", text, count=1).strip()
        text = SUBJECT_TAG_RE.sub("", text, count=1).strip()
        text = strip_leading_tmux_tags(text)
        if text == before:
            return " ".join(text.split()).casefold()


def subject_base(subject: str) -> str:
    text = subject.strip()
    while True:
        before = text
        text = RE_PREFIX_RE.sub("", text, count=1).strip()
        text = SUBJECT_TAG_RE.sub("", text, count=1).strip()
        text = strip_leading_tmux_tags(text)
        if text == before:
            return text


def canonical_manager_subject(subject: str) -> str:
    base = subject_base(subject)
    if starts_w_re(subject):
        return manager_reply_subject(base)
    return manager_subject(base)


def has_manager_tag(subject: str) -> bool:
    text = subject.strip()
    while True:
        text = RE_PREFIX_RE.sub("", text, count=1).strip()
        if MANAGER_TAG_RE.match(text) is not None:
            return True
        next_text = MANAGER_TAG_RE.sub("", text, count=1).strip()
        if next_text == text:
            return False
        text = next_text


def validate_subject(subject: str) -> None:
    if PLACEHOLDER_RE.fullmatch(normalized_subject_key(subject)):
        raise SubjectInputError("subject must be a real subject, not the placeholder SUBJECT")
    if RESERVED_AGENT_TAG_RE.match(subject.strip()):
        raise SubjectInputError("agent email subject must use [a]; [omo] is deprecated")


def starts_w_re(subject: str) -> bool:
    return RE_PREFIX_RE.match(subject.strip()) is not None


def manager_reply_subject(base: str) -> str:
    return f"Re: {SHORT_MANAGER_TAG} {base.strip()}"


def manager_subject(base: str) -> str:
    return f"{SHORT_MANAGER_TAG} {base.strip()}"


def canonical_tmux_target(tmux_target: str) -> str:
    clean_target = tmux_target.strip()
    window_target, dot, pane = clean_target.rpartition(".")
    if dot and pane == "0" and ":" in window_target:
        return window_target
    return clean_target


def manager_subject_w_target(base: str, tmux_target: str = "", reply: bool = False) -> str:
    clean_base = strip_leading_tmux_tags(base.strip())
    clean_target = canonical_tmux_target(tmux_target)
    bracketed_target = f"[{clean_target}]"
    if clean_target and TMUX_TARGET_RE.fullmatch(clean_target) and not clean_base.startswith(f"{bracketed_target} "):
        clean_base = f"{bracketed_target} {clean_base}"
    return manager_reply_subject(clean_base) if reply else manager_subject(clean_base)


def strip_leading_tmux_tags(subject: str) -> str:
    text = subject.strip()
    while True:
        next_text = TMUX_SUBJECT_TAG_RE.sub("", text, count=1).strip()
        if next_text == text:
            return text
        text = next_text


def parsed_header_date(raw_date: str) -> datetime | None:
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def find_recent_thread(subject_key: str) -> RecentHeader | None:
    lookup_s = int(os.environ.get("OMO_MANAGER_EMAIL_THREAD_LOOKUP_S", str(DEFAULT_THREAD_LOOKUP_WINDOW_S)))
    if lookup_s <= 0:
        return None
    split_settings = configured_agent_mail()
    config = (
        {"host": GMAIL_IMAP_HOST, "user": split_settings.agent_address, "password": split_settings.app_password}
        if split_settings is not None
        else parse_env_config(Path(os.environ.get("OMO_EMAIL_CONFIG_PATH", CONFIG_PATH)))
    )
    if not {"host", "user", "password"} <= set(config):
        return None
    cutoff = datetime.now().astimezone() - timedelta(seconds=lookup_s)
    timeout_s = float(os.environ.get("OMO_MANAGER_EMAIL_THREAD_LOOKUP_TIMEOUT_S", "10"))
    client = imaplib.IMAP4_SSL(config["host"], timeout=timeout_s)
    timed_out = False
    try:
        client.login(config["user"], config["password"])
        mailbox_searches = (
            [
                ("[Gmail]/Sent Mail", split_settings.agent_address, split_settings.human_address),
                ("INBOX", split_settings.human_address, split_settings.agent_address),
            ]
            if split_settings is not None
            else [("INBOX", config["user"], "")]
        )
        best: RecentHeader | None = None
        for mailbox, sender, recipient in mailbox_searches:
            typ, _data = client.select(mailbox, readonly=True)
            if typ != "OK":
                continue
            criteria = ["SINCE", cutoff.strftime("%d-%b-%Y"), "FROM", f'"{sender}"']
            if recipient:
                criteria.extend(["TO", f'"{recipient}"'])
            typ, data = client.uid("search", None, *criteria)
            if typ != "OK" or not data or not data[0]:
                continue
            for raw_uid in data[0].split():
                uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
                header = fetch_recent_header(client, uid)
                if header is None or header.date is None or header.date < cutoff:
                    continue
                if parseaddr(header.sender)[1].casefold() != sender.casefold() or normalized_subject_key(header.subject) != subject_key:
                    continue
                if best is None or best.date is None or header.date > best.date:
                    best = header
        return best
    except SubjectLookupTimeout:
        timed_out = True
        raise
    finally:
        try:
            if timed_out:
                client.shutdown()
            else:
                client.logout()
        except (SubjectLookupTimeout, OSError, imaplib.IMAP4.error):
            pass


def fetch_recent_header(client: imaplib.IMAP4_SSL, uid: str) -> RecentHeader | None:
    typ, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT MESSAGE-ID REFERENCES)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    msg = BytesParser(policy=policy.default).parsebytes(data[0][1])
    return RecentHeader(str(msg.get("From", "")), str(msg.get("Subject", "")), parsed_header_date(str(msg.get("Date", ""))), str(msg.get("Message-ID", "")), str(msg.get("References", "")))


def recent_thread_exists(subject_key: str) -> bool:
    return find_recent_thread(subject_key) is not None


@contextmanager
def recent_thread_lookup_deadline(timeout_s: float) -> Iterator[None]:
    if timeout_s <= 0 or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return
    old_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(_signum: int, _frame: object) -> None:
        raise SubjectLookupTimeout(f"recent thread lookup exceeded {timeout_s:g}s")

    signal.signal(signal.SIGALRM, raise_timeout)
    old_timer = signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])


def has_recent_thread(subject_key: str) -> bool:
    try:
        deadline_s = float(os.environ.get("OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S", str(DEFAULT_THREAD_LOOKUP_DEADLINE_S)))
        with recent_thread_lookup_deadline(deadline_s):
            return recent_thread_exists(subject_key)
    except Exception as exc:
        print(f"omo_email_subject: recent thread lookup skipped: {exc}", file=sys.stderr)
        return False


def recent_thread_header(subject_key: str) -> RecentHeader | None:
    try:
        deadline_s = float(os.environ.get("OMO_MANAGER_EMAIL_THREAD_LOOKUP_DEADLINE_S", str(DEFAULT_THREAD_LOOKUP_DEADLINE_S)))
        with recent_thread_lookup_deadline(deadline_s):
            return find_recent_thread(subject_key)
    except Exception as exc:
        print(f"omo_email_subject: recent thread lookup skipped: {exc}", file=sys.stderr)
        return None


def reply_headers_for_subject(subject: str) -> dict[str, str]:
    header = recent_thread_header(normalized_subject_key(subject))
    return reply_headers_from_recent_header(header)


def reply_headers_from_recent_header(header: RecentHeader | None) -> dict[str, str]:
    if header is None or not header.message_id.strip():
        return {}
    message_id = header.message_id.strip()
    references = [item for item in header.references.split() if item]
    if message_id not in references:
        references.append(message_id)
    return {"In-Reply-To": message_id, "References": " ".join(references)}


def prepare_subject_and_headers(subject: str, tmux_target: str = "") -> tuple[str, dict[str, str]]:
    validate_subject(subject)
    stripped = subject.strip()
    base = subject_base(stripped)
    header = recent_thread_header(normalized_subject_key(stripped))
    if starts_w_re(stripped) and has_manager_tag(stripped):
        return manager_subject_w_target(subject_base(stripped), tmux_target, True), reply_headers_from_recent_header(header)
    if starts_w_re(stripped) or header is not None:
        return manager_subject_w_target(base, tmux_target, True), reply_headers_from_recent_header(header)
    return manager_subject_w_target(base, tmux_target), {}


def fallback_subject(subject: str, tmux_target: str = "") -> str:
    if has_manager_tag(subject):
        return manager_subject_w_target(subject_base(subject), tmux_target, starts_w_re(subject))
    if starts_w_re(subject):
        return manager_subject_w_target(subject_base(subject), tmux_target, True)
    return manager_subject_w_target(subject_base(subject), tmux_target)


def prepare_subject(subject: str, tmux_target: str = "") -> str:
    validate_subject(subject)
    stripped = subject.strip()
    if starts_w_re(stripped) and has_manager_tag(stripped):
        return manager_subject_w_target(subject_base(stripped), tmux_target, True)
    base = subject_base(stripped)
    if starts_w_re(stripped) or has_recent_thread(normalized_subject_key(stripped)):
        return manager_subject_w_target(base, tmux_target, True)
    return manager_subject_w_target(base, tmux_target)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("subject")
    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("subject")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "normalize":
        print(normalized_subject_key(args.subject))
        return 0
    try:
        print(prepare_subject(args.subject))
    except SubjectInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"omo_email_subject: subject preparation fell back: {exc}", file=sys.stderr)
        print(fallback_subject(args.subject))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
