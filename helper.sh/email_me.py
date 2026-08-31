#!/usr/bin/env python3
"""Email the configured human from the agent Gmail account."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import imaplib
import os
import re
import secrets
import smtplib
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid
from html import escape
from pathlib import Path

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
ENV_FILE_PATH = Path.home() / ".config" / ".env"
MANAGER_DIR = Path(__file__).resolve().parents[1] / "omo_manager"
if MANAGER_DIR.is_dir():
    sys.path.insert(0, str(MANAGER_DIR))
from omo_email_config import (  # noqa: E402
    GMAIL_IMAP_HOST,
    GUEST_HEES_ADDRESS,
    GUEST_HEES_SESSION,
    AgentMailSettings,
    configured_agent_mail,
    fulfill_guest_hees_reply_obligation,
    guest_hees_mail,
    guest_hees_target,
    open_guest_hees_reply_message_ids,
    open_guest_hees_reply_source,
)
from omo_email_subject import (  # noqa: E402
    MailRouteProfile,
    SubjectInputError,
    canonical_tmux_target,
    normalized_subject_key,
    prepare_latest_thread_for_tmux_target,
    prepare_subject,
    prepare_subject_and_headers,
    reply_headers_for_subject,
    strip_leading_tmux_tags,
)
from omo_guest_images import GuestImageError, ValidatedImage, reply_attachments  # noqa: E402

PWD_FOOTER_RE = re.compile(r"(?:^|\n)(?:>\s*)?PWD: [^\n]+\n?\Z")
UNQUOTED_PWD_FOOTER_RE = re.compile(r"(?:^|\n)PWD: [^\n]+\n?\Z")
TMUX_WINDOW_RE = re.compile(r"[^:\n]+:\d+(?:\.\d+)?\Z")
AGENT_SESSION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)
TMUX_SUBJECT_TAG_RE = re.compile(r"^\s*(?:\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]|[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?:\s+|$)")
BRACKETED_TMUX_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]")
MANAGER_HUMAN_SUBJECT_RE = re.compile(r"^(?:Re:\s*)?\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\](?:\s+|$)", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+\s*)?$")
UNORDERED_LIST_RE = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")
ORDERED_LIST_RE = re.compile(r"^\s{0,3}\d+[.)]\s+(.+)$")
BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")


@dataclass(frozen=True)
class CliArgs:
    title: str | None
    content: str
    dry_run: bool
    add_pwd_footer: bool
    manager_human: bool
    tmux_target: str | None
    supersedes_message_ids: tuple[str, ...]
    guest_hees: bool
    guest_image_references: tuple[str, ...]


class ParsedArgs(argparse.Namespace):
    subject: str | None = None
    legacy_args: list[str]
    subject_file: Path | None = None
    message_file: Path | None = None
    dry_run: bool = False
    no_pwd_footer: bool = False
    manager_human: bool = False
    tmux_target: str | None = None
    sender_tmux_target: str | None = None
    supersedes_message_id: list[str]
    guest_hees: bool = False
    guest_image_reference: list[str]


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        usage="email_me.py [--dry-run] [--subject TEXT | --subject-file FILE] [--message-file FILE]",
        description=(
            "Email the human with manager-safe subject handling. The body accepts Markdown input, but plain text is preferred. "
            "Reads the email body from standard input by default; "
            "use --message-file for a saved body. Do not pass body text as a shell argument."
        ),
    )
    _ = parser.add_argument("legacy_args", nargs="*", help=argparse.SUPPRESS)
    _ = parser.add_argument("--subject", help="Email subject/title.")
    _ = parser.add_argument("--subject-file", type=Path, help="Read the email subject from this one-line file instead of an argument.")
    _ = parser.add_argument("--message-file", type=Path, help="Read the email body from this file instead of stdin.")
    _ = parser.add_argument("--dry-run", action="store_true", help="Validate without sending.")
    _ = parser.add_argument(
        "--no-pwd-footer",
        action="store_true",
        help="Send the body exactly as provided, without appending a PWD footer. Agents must not use this option unless explicitly told to.",
    )
    _ = parser.add_argument("--tmux-target", help="Normally omit: the helper infers producer identity from the exact current pane, then the launch environment. Override only to preserve a different verified producer identity; never pass a task owner or delivery target.")
    _ = parser.add_argument("--sender-tmux-target", dest="sender_tmux_target", help="Alias for --tmux-target; use only when forwarding or compressing mail while preserving a different verified producer identity.")
    _ = parser.add_argument("--supersedes-message-id", action="append", default=[], help="Exact Message-ID from agent-unread that this replacement supersedes; repeat for multiple messages.")
    _ = parser.add_argument("--manager-human", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--guest-hees", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--guest-image-reference", action="append", default=[], help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.legacy_args:
        parser.error("pass email subject with --subject or --subject-file; pass email body by standard input or --message-file.")
    if parsed.subject is not None and parsed.subject_file is not None:
        parser.error("pass email subject with --subject or --subject-file, not both.")
    if parsed.subject_file is not None:
        try:
            raw_title = parsed.subject_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"subject file not readable: {exc}")
        title_lines = raw_title.splitlines()
        if len(title_lines) != 1:
            parser.error("subject file must contain exactly one text line.")
        title = title_lines[0]
        if not title.strip():
            parser.error("subject file must not be empty.")
        if "\r" in title or "\0" in title:
            parser.error("subject file must contain exactly one text line.")
    else:
        title = parsed.subject
    if parsed.message_file is not None:
        try:
            content = parsed.message_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"message file not readable: {exc}")
    else:
        content = sys.stdin.read()
    for ch in title or "":
        codepoint = ord(ch)
        if codepoint < 32 or codepoint == 127:
            parser.error("`title` must not contain control characters.")
    if parsed.manager_human and not content.strip():
        parser.error("email body must not be empty.")
    if parsed.tmux_target is not None and parsed.sender_tmux_target is not None and parsed.tmux_target != parsed.sender_tmux_target:
        parser.error("pass --tmux-target or --sender-tmux-target, not both.")
    tmux_target = parsed.tmux_target or parsed.sender_tmux_target
    if tmux_target is not None and not valid_tmux_target(tmux_target):
        parser.error("--tmux-target must have shape session:window or session:window.pane, for example wl:4.")
    guest_hees = parsed.guest_hees or (parsed.manager_human and guest_hees_tmux_target(tmux_target))
    if guest_hees and not parsed.manager_human:
        parser.error("--guest-hees requires --manager-human.")
    if guest_hees and not guest_hees_tmux_target(tmux_target):
        parser.error("--guest-hees requires a --tmux-target in the guest_hees session.")
    if any(re.fullmatch(r"<[^<>\s]+>", value) is None for value in parsed.supersedes_message_id):
        parser.error("--supersedes-message-id must be an exact RFC Message-ID enclosed in angle brackets.")
    if len(set(parsed.supersedes_message_id)) != len(parsed.supersedes_message_id):
        parser.error("--supersedes-message-id values must be unique.")
    return CliArgs(
        title=title,
        content=content,
        dry_run=parsed.dry_run,
        add_pwd_footer=not parsed.no_pwd_footer,
        manager_human=parsed.manager_human,
        tmux_target=tmux_target,
        supersedes_message_ids=tuple(parsed.supersedes_message_id),
        guest_hees=guest_hees,
        guest_image_references=tuple(parsed.guest_image_reference),
    )


def normalize_subject(title: str, tmux_target: str = "") -> str:
    tmux_target = canonical_email_tmux_target(tmux_target)
    if prepare_subject is not None:
        try:
            return prepare_subject(title, tmux_target)
        except SubjectInputError as exc:
            raise ValueError(str(exc)) from exc
    stripped = title.strip()
    lowered = stripped.lower()
    normalized_placeholder = re.sub(r"\W+", "", stripped).casefold()
    if normalized_placeholder == "subject":
        raise ValueError("subject must be a real subject, not the placeholder SUBJECT")
    if re.match(r"^(?:re:\s*)*\[omo\]\s*", lowered):
        raise ValueError("agent email subject must not use deprecated [omo]")
    base = stripped
    reply = False
    while True:
        before = base
        if re.match(r"^\s*re:\s*", base, flags=re.IGNORECASE):
            reply = True
            base = re.sub(r"^\s*re:\s*", "", base, count=1, flags=re.IGNORECASE).strip()
        base = re.sub(r"^\s*(?:\[a\]|\[omo_manager\]|\[omo_manager_recover\])\s*", "", base, count=1, flags=re.IGNORECASE).strip()
        base = clean_subject_tmux_tags(base)
        if base == before:
            break
    if re.sub(r"\W+", "", base).casefold() == "subject":
        raise ValueError("subject must be a real subject, not the placeholder SUBJECT")
    clean_target = tmux_target.strip()
    base = clean_subject_tmux_tags(base)
    bracketed_target = f"[{clean_target}]"
    if clean_target and valid_tmux_target(clean_target) and not base.startswith(f"{bracketed_target} "):
        base = f"{bracketed_target} {base}"
    if lowered.startswith("re:"):
        return f"Re: {base}"
    if reply:
        return f"Re: {base}"
    return base


def current_pwd() -> str:
    pwd = os.environ.get("PWD", "")
    cwd = Path.cwd()
    if pwd:
        try:
            if Path(pwd).resolve() == cwd.resolve():
                return pwd
        except OSError:
            pass
    return str(cwd)


def current_tmux_window() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    command = ["tmux", "display-message", "-p"]
    if pane_id := os.environ.get("TMUX_PANE", "").strip():
        command.extend(("-t", pane_id))
    command.append("#S:#I.#P" if pane_id else "#S:#I")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    target = result.stdout.strip()
    if not TMUX_WINDOW_RE.fullmatch(target):
        return None
    return target


def valid_tmux_target(target: str) -> bool:
    return bool(TMUX_WINDOW_RE.fullmatch(target))


# 🧑 "The dedicated guest manager and its agents for hees ... send emails back to `46496337@qq.com`"
def guest_hees_tmux_target(target: str | None) -> bool:
    """Return whether `target` belongs to the dedicated guest session."""
    if target is None:
        return False
    canonical = canonical_email_tmux_target(target)
    return guest_hees_target(canonical) and canonical.partition(":")[0] == GUEST_HEES_SESSION


def substantive_guest_reply(content: str) -> bool:
    """Reject blank and canonical lifecycle-only guest mail."""
    lines = [line.strip() for line in markdown_links_to_plain(content).splitlines() if line.strip()]
    if not lines:
        return False
    labels = ("Task:", "Outcome:", "Items:", "Evidence:", "Completion record:")
    lifecycle_shape = any(line.startswith("Task:") for line in lines) and any(line.startswith("Outcome:") for line in lines)
    if lifecycle_shape and all(line.startswith(labels) or line.startswith(('- ', '* ')) for line in lines):
        return False
    normalized = " ".join(lines).casefold().rstrip(".! ")
    return normalized not in {"done", "task done", "completed", "task completed", "pending item removed"}


def verified_guest_reply_headers(reply_headers: dict[str, str]) -> bool:
    """Require one exact reply parent that also appears in References."""
    parent = reply_headers.get("In-Reply-To", "")
    references = reply_headers.get("References", "").split()
    return re.fullmatch(r"<[^<>\s]+>", parent) is not None and parent in references


def sent_plain_text(message: Message) -> str:
    if isinstance(message, EmailMessage):
        body = message.get_body(preferencelist=("plain",))
        return body.get_content() if body is not None else ""
    return ""


def sent_message_matches_guest_reply(candidate: Message, expected: EmailMessage, sender: str) -> bool:
    """Validate exact participants, thread, identity, and substantive Sent content."""
    recipients = [address for _name, address in getaddresses(candidate.get_all("To", []))]
    senders = [address for _name, address in getaddresses(candidate.get_all("From", []))]
    headers = {
        "In-Reply-To": str(candidate.get("In-Reply-To", "")),
        "References": str(candidate.get("References", "")),
    }
    return (
        senders == [sender]
        and recipients == [GUEST_HEES_ADDRESS]
        and str(candidate.get("Message-ID", "")) == str(expected.get("Message-ID", ""))
        and str(candidate.get("Subject", "")) == str(expected.get("Subject", ""))
        and headers["In-Reply-To"] == str(expected.get("In-Reply-To", ""))
        and headers["References"] == str(expected.get("References", ""))
        and verified_guest_reply_headers(headers)
        and substantive_guest_reply(sent_plain_text(candidate))
        and sent_plain_text(candidate) == sent_plain_text(expected)
    )


@dataclass(frozen=True)
class GuestSentEvidence:
    subject_sha256: str
    body_sha256: str


@dataclass
class GuestReplyClaim:
    fd: int

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def acquire_guest_reply_claim(state_dir: Path, source: str) -> GuestReplyClaim | None:
    """Acquire and validate one fail-closed process lease for an exact obligation."""
    if not source or any(character in source for character in "\r\n\0"):
        return None
    directory = state_dir / "guest-hees-reply-claims"
    path = directory / f"{hashlib.sha256(source.encode()).hexdigest()}.claim"
    expected = f"version=v1\nsource={source}\n".encode()
    fd = -1
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        if not path.exists():
            temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                offset = 0
                while offset < len(expected):
                    written = os.write(temporary_fd, expected[offset:])
                    if written <= 0:
                        raise OSError("guest reply claim write failed")
                    offset += written
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise OSError("unsafe guest reply claim")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.read(fd, len(expected) + 1) != expected:
            raise OSError("invalid guest reply claim")
        return GuestReplyClaim(fd)
    except OSError:
        if fd >= 0:
            os.close(fd)
        return None
    finally:
        temporary.unlink(missing_ok=True)


def guest_reply_attempt_path(state_dir: Path, source: str) -> Path:
    digest = hashlib.sha256(source.encode()).hexdigest()
    return state_dir / "guest-hees-reply-attempts" / f"{digest}.eml"


def store_guest_reply_attempt(state_dir: Path, source: str, message: EmailMessage) -> bool:
    """Persist the exact expected message before SMTP for safe retry reconciliation."""
    path = guest_reply_attempt_path(state_dir, source)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            _ = handle.write(message.as_bytes(policy=policy.default))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        return False
    return load_guest_reply_attempt(state_dir, source, str(message["In-Reply-To"]), str(message["From"])) is not None


def load_guest_reply_attempt(state_dir: Path, source: str, inbound_message_id: str, sender: str) -> EmailMessage | None:
    """Load only an owner-private exact-thread substantive prior attempt."""
    path = guest_reply_attempt_path(state_dir, source)
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or len(payload) > 50 * 1024 * 1024
    ):
        return None
    candidate = BytesParser(policy=policy.default).parsebytes(payload)
    if not isinstance(candidate, EmailMessage):
        return None
    if (
        str(candidate.get("In-Reply-To", "")) != inbound_message_id
        or re.fullmatch(r"<[^<>\s]+>", str(candidate.get("Message-ID", ""))) is None
        or not sent_message_matches_guest_reply(candidate, candidate, sender)
    ):
        return None
    return candidate


def verify_guest_reply_in_sent(settings: AgentMailSettings, expected: EmailMessage) -> GuestSentEvidence | None:
    """Find one exact immutable copy in Gmail Sent Mail before reporting success."""
    try:
        timeout_s = max(float(os.environ.get("OMO_GUEST_HEES_SENT_VERIFY_TIMEOUT_S", "20")), 0)
    except ValueError:
        timeout_s = 20
    deadline_s = time.monotonic() + timeout_s
    while True:
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=min(max(timeout_s, 1), 30))
            client.login(settings.agent_address, settings.app_password)
            typ, _data = client.select('"[Gmail]/Sent Mail"', readonly=True)
            if typ != "OK":
                raise imaplib.IMAP4.error("cannot select Sent Mail")
            typ, data = client.uid("search", None, "HEADER", "Message-ID", str(expected["Message-ID"]))
            uids = b" ".join(item for item in data or [] if isinstance(item, bytes)).split() if typ == "OK" else []
            if len(uids) == 1:
                typ, fetched = client.uid("fetch", uids[0].decode("ascii"), "(BODY.PEEK[])")
                payloads = [item[1] for item in fetched or [] if isinstance(item, tuple) and isinstance(item[1], bytes)]
                if typ == "OK" and len(payloads) == 1:
                    candidate = BytesParser(policy=policy.default).parsebytes(payloads[0])
                    if sent_message_matches_guest_reply(candidate, expected, settings.agent_address):
                        return GuestSentEvidence(
                            hashlib.sha256(str(candidate["Subject"]).encode()).hexdigest(),
                            hashlib.sha256(sent_plain_text(candidate).encode()).hexdigest(),
                        )
        except (OSError, ValueError, imaplib.IMAP4.error):
            pass
        finally:
            if client is not None:
                try:
                    client.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass
        if time.monotonic() >= deadline_s:
            return None
        time.sleep(min(0.5, max(0, deadline_s - time.monotonic())))


def validate_manager_route_identity(explicit_target: str | None, selected_target: str) -> None:
    if explicit_target is None:
        return
    inferred_target = inferred_tmux_target(True)
    if inferred_target is None:
        return
    if guest_hees_tmux_target(explicit_target) != guest_hees_tmux_target(inferred_target):
        raise ValueError(
            f"explicit tmux target {canonical_email_tmux_target(explicit_target)} conflicts with verified producer route {canonical_email_tmux_target(inferred_target)}"
        )
    if guest_hees_tmux_target(selected_target) != guest_hees_tmux_target(inferred_target):
        raise ValueError("selected email route conflicts with verified producer identity")


def canonical_email_tmux_target(target: str) -> str:
    if canonical_tmux_target is not None:
        return canonical_tmux_target(target)
    clean_target = target.strip()
    window_target, dot, pane = clean_target.rpartition(".")
    if dot and pane == "0" and ":" in window_target:
        return window_target
    return clean_target


def env_tmux_target() -> str | None:
    target = os.environ.get("OMO_AGENT_TMUX_TARGET", "").strip()
    if valid_tmux_target(target):
        return canonical_email_tmux_target(target)
    return None


def env_manager_tmux_target() -> str | None:
    target = os.environ.get("OMO_MANAGER_TMUX_TARGET", "").strip()
    if valid_tmux_target(target):
        return canonical_email_tmux_target(target)
    return None


def inferred_tmux_target(manager_human: bool) -> str | None:
    agent_target = env_tmux_target()
    has_pane_id = bool(os.environ.get("TMUX_PANE", "").strip())
    current_target = current_tmux_window() if has_pane_id else None
    if current_target is not None:
        current_target = canonical_email_tmux_target(current_target)
        return agent_target if agent_target == current_target else current_target
    fallback_target = agent_target or (env_manager_tmux_target() if manager_human else None)
    if fallback_target is not None:
        return fallback_target
    return None if has_pane_id else current_tmux_window()


def agent_session_id() -> str:
    value = (os.environ.get("CODEX_SESSION_ID", "").strip() or os.environ.get("CODEX_THREAD_ID", "").strip()).lower()
    return value if AGENT_SESSION_RE.fullmatch(value) else ""


def footer_tmux_target(explicit_tmux_target: str | None = None, manager_human: bool = False) -> str | None:
    if explicit_tmux_target is not None:
        if not valid_tmux_target(explicit_tmux_target):
            raise ValueError("tmux target must have shape session:window or session:window.pane.")
        return canonical_email_tmux_target(explicit_tmux_target)
    return inferred_tmux_target(manager_human)


def clean_subject_tmux_tags(subject: str) -> str:
    if strip_leading_tmux_tags is not None:
        return strip_leading_tmux_tags(subject)
    text = subject.strip()
    while True:
        next_text = TMUX_SUBJECT_TAG_RE.sub("", text, count=1).strip()
        if next_text == text:
            return text
        text = next_text


def validate_manager_human_subject(subject: str) -> None:
    stripped = subject.strip()
    if MANAGER_HUMAN_SUBJECT_RE.match(stripped) is None or len(BRACKETED_TMUX_TAG_RE.findall(stripped)) != 1:
        raise ValueError("manager-human subject must contain exactly one bracketed tmux tag.")


def append_pwd_footer(content: str, cwd: str | Path | None = None, tmux_target: str | None = None, require_unquoted_footer: bool = False) -> str:
    pwd_footer = UNQUOTED_PWD_FOOTER_RE if require_unquoted_footer else PWD_FOOTER_RE
    if pwd_footer.search(content):
        return content
    del tmux_target
    footer = f"PWD: {short_pwd(cwd or current_pwd())}"
    body = content.rstrip("\n")
    return f"{body}\n\n{footer}\n"


def short_pwd(cwd: str | Path) -> str:
    path = Path(cwd)
    return path.name or str(path)


def markdown_links_to_plain(text: str) -> str:
    return MARKDOWN_LINK_RE.sub(lambda match: f"{match.group(1).strip()}: {match.group(2).strip()}", text)


def render_inline_markdown(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in INLINE_CODE_RE.finditer(text):
        parts.append(render_inline_text(text[last_end:match.start()]))
        parts.append(f'<code style="font-family: monospace;">{escape(match.group(1))}</code>')
        last_end = match.end()
    parts.append(render_inline_text(text[last_end:]))
    return "".join(parts)


def render_inline_text(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in MARKDOWN_LINK_RE.finditer(text):
        parts.append(render_inline_styles(text[last_end:match.start()]))
        label = render_inline_styles(match.group(1).strip())
        url = escape(match.group(2).strip(), quote=True)
        parts.append(f'<a href="{url}" style="color: #1155cc;">{label}</a>')
        last_end = match.end()
    parts.append(render_inline_styles(text[last_end:]))
    return "".join(parts)


def render_inline_styles(text: str) -> str:
    html = escape(text)
    html = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![A-Za-z0-9])__(?=\S)(.+?)(?<=\S)__(?![A-Za-z0-9])", lambda match: f"<strong>{match.group(1) or match.group(2)}</strong>", html)
    return re.sub(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)|(?<![A-Za-z0-9])_(?!_)(?=\S)(.+?)(?<=\S)_(?![A-Za-z0-9])", lambda match: f"<em>{match.group(1) or match.group(2)}</em>", html)


def inline_lines_html(text: str) -> str:
    return "<br> ".join(render_inline_markdown(line) for line in text.splitlines())


def paragraph_html(lines: list[str]) -> str:
    text = "\n".join(lines)
    return f'<p style="margin: 0 0 12px 0;">{inline_lines_html(text)}</p>'


def list_html(kind: str, items: list[str]) -> str:
    tag = "ol" if kind == "ol" else "ul"
    rendered_items = "\n".join(f'<li style="margin: 0 0 4px 0;">{inline_lines_html(item)}</li>' for item in items)
    return f'<{tag} style="margin: 0 0 12px 24px; padding: 0;">\n{rendered_items}\n</{tag}>'


def blockquote_html(lines: list[str]) -> str:
    text = "\n".join(lines)
    inner = inline_lines_html(text)
    return f'<blockquote style="margin: 0 0 12px 0; padding-left: 12px; border-left: 4px solid #d0d7de; color: #57606a;">{inner}</blockquote>'


def match_list_item(line: str, in_list: bool) -> tuple[str, str] | None:
    if (unordered := UNORDERED_LIST_RE.match(line)) is not None:
        return ("ul", unordered.group(1))
    if (ordered := ORDERED_LIST_RE.match(line)) is not None:
        return ("ol", ordered.group(1))
    if not in_list:
        return None
    if (nested_unordered := re.match(r"^\s+[-*+]\s+(.+)$", line)) is not None:
        return ("ul", nested_unordered.group(1))
    if (nested_ordered := re.match(r"^\s+\d+[.)]\s+(.+)$", line)) is not None:
        return ("ol", nested_ordered.group(1))
    return None


def markdown_to_html(text: str) -> str:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_kind = ""
    list_items: list[str] = []
    quote_lines: list[str] = []
    lines = text.splitlines()
    idx = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(paragraph_html(paragraph))
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_items:
            blocks.append(list_html(list_kind, list_items))
            list_items.clear()
        list_kind = ""

    def flush_quote() -> None:
        if quote_lines:
            blocks.append(blockquote_html(quote_lines))
            quote_lines.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            flush_all()
            idx += 1
            continue
        if FENCE_RE.match(line):
            flush_all()
            fence = FENCE_RE.match(line)
            assert fence is not None
            marker = fence.group(1)
            idx += 1
            code_lines: list[str] = []
            while idx < len(lines) and not lines[idx].lstrip().startswith(marker):
                code_lines.append(lines[idx])
                idx += 1
            if idx < len(lines):
                idx += 1
            code = escape("\n".join(code_lines))
            blocks.append(f'<pre style="margin: 0 0 12px 0; padding: 10px; background: #f6f8fa; white-space: pre-wrap;"><code>{code}</code></pre>')
            continue
        quote = BLOCKQUOTE_RE.match(line)
        if quote is not None:
            flush_paragraph()
            flush_list()
            quote_lines.append(quote.group(1))
            idx += 1
            continue
        list_item = match_list_item(line, bool(list_items))
        if list_item is not None:
            flush_paragraph()
            flush_quote()
            kind, item_text = list_item
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_items.append(item_text)
            idx += 1
            continue
        if list_items and (line.startswith(" ") or line.startswith("\t")):
            list_items[-1] = f"{list_items[-1]}\n{line.strip()}"
            idx += 1
            continue
        flush_list()
        flush_quote()
        heading = HEADING_RE.match(line)
        if heading is not None:
            flush_paragraph()
            level = min(len(heading.group(1)), 6)
            blocks.append(f'<h{level} style="margin: 0 0 12px 0;">{render_inline_markdown(heading.group(2).strip())}</h{level}>')
        elif HR_RE.match(line):
            flush_paragraph()
            blocks.append('<hr style="border: 0; border-top: 1px solid #d0d7de; margin: 16px 0;">')
        else:
            paragraph.append(line)
        idx += 1
    flush_all()
    body = "\n".join(blocks)
    return f'<!doctype html><html><body style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.45;">{body}</body></html>\n'


def build_message(sender_email: str, title: str, content: str, add_pwd_footer: bool = True, prepared_subject: str | None = None, reply_headers: dict[str, str] | None = None, tmux_target: str | None = None, manager_human: bool = False, recipient_email: str | None = None, supersedes_message_ids: tuple[str, ...] = (), agent_session: str = "") -> EmailMessage:
    source_target = footer_tmux_target(tmux_target, manager_human)
    msg = EmailMessage()
    msg.add_header("Subject", prepared_subject or normalize_subject(title, source_target or ""))
    msg.add_header("From", sender_email)
    msg.add_header("To", recipient_email or sender_email)
    msg.add_header("Message-ID", make_msgid(domain=sender_email.partition("@")[2] or None))
    if agent_session:
        if AGENT_SESSION_RE.fullmatch(agent_session) is None:
            raise ValueError("agent session identity must be a UUID")
        msg.add_header("X-OMO-Agent-Session-ID", agent_session.lower())
    for message_id in supersedes_message_ids:
        msg.add_header("X-OMO-Supersedes", message_id)
    if reply_headers is not None:
        for name, value in reply_headers.items():
            msg.add_header(name, value)
    elif reply_headers_for_subject is not None:
        for name, value in reply_headers_for_subject(title).items():
            msg.add_header(name, value)
    body = append_pwd_footer(content, tmux_target=source_target, require_unquoted_footer=manager_human) if add_pwd_footer else content
    msg.set_content(markdown_links_to_plain(body))
    msg.add_alternative(markdown_to_html(body), subtype="html")
    return msg


def attach_guest_images(msg: EmailMessage, images: tuple[ValidatedImage, ...]) -> None:
    for image in images:
        maintype, subtype = image.mime_type.split("/", 1)
        msg.add_attachment(image.data, maintype=maintype, subtype=subtype, filename=image.filename)


def parse_env_file(file_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
            if "=" not in line:
                continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        env_value = value.strip()
        if env_value.startswith('"'):
            quote = env_value[0]
            end_idx = 1
            escaped = False
            while end_idx < len(env_value):
                ch = env_value[end_idx]
                if escaped:
                    escaped = False
                    end_idx += 1
                    continue
                if ch == "\\":
                    escaped = True
                    end_idx += 1
                    continue
                if ch == quote:
                    break
                end_idx += 1
            if end_idx < len(env_value) and env_value[end_idx] == quote:
                quoted = env_value[1:end_idx]
                env_value = bytes(quoted, "utf-8").decode("unicode_escape")
            else:
                env_value = env_value[1:]
        elif env_value.startswith("'"):
            quote = env_value[0]
            end_idx = env_value.find(quote, 1)
            if end_idx >= 0:
                env_value = env_value[1:end_idx]
            else:
                env_value = env_value[1:]
        else:
            hash_idx = env_value.find("#")
            if hash_idx >= 0 and hash_idx > 0 and env_value[hash_idx - 1].isspace():
                env_value = env_value[:hash_idx].rstrip()
        values[env_key] = env_value
    return values


def manager_state_dir() -> Path:
    return Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def should_send_manager_email(subject: str, content: str) -> bool:
    return should_send_manager_email_key(subject, subject, content)


def manager_email_dedupe_digest(subject: str, content: str) -> str:
    return hashlib.sha256(subject.encode() + b"\0" + content.encode()).hexdigest()


def should_send_manager_email_key(dedupe_subject: str, display_subject: str, content: str, state_scope: str = "human") -> bool:
    try:
        state_dir = manager_state_dir()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        dedupe_s = int(os.environ.get("OMO_MANAGER_EMAIL_DEDUPE_S", "300"))
        dedupe_file = state_dir / f"{state_scope}-email-dedupe.tsv"
        lock_file = state_dir / f"{state_scope}-email-dedupe.lock"
        digest = manager_email_dedupe_digest(dedupe_subject, content)
        now_s = int(time.time())
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            rows: list[tuple[int, str, str]] = []
            try:
                for line in dedupe_file.read_text(encoding="utf-8").splitlines():
                    raw_s, old_digest, old_subject = line.split("\t", 2)
                    sent_s = int(raw_s)
                    if now_s - sent_s <= max(dedupe_s, 0):
                        rows.append((sent_s, old_digest, old_subject))
            except (OSError, ValueError):
                pass
            if any(old_digest == digest for _, old_digest, _ in rows):
                return False
            rows.append((now_s, digest, display_subject.replace("\t", " ").replace("\n", " ")))
            tmp = dedupe_file.with_name(f".{dedupe_file.name}.tmp")
            tmp.write_text("".join(f"{sent_s}\t{old_digest}\t{old_subject}\n" for sent_s, old_digest, old_subject in rows), encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(dedupe_file)
    except (OSError, ValueError):
        return True
    return True


def log_manager_email(subject: str, state_scope: str = "human") -> None:
    try:
        state_dir = manager_state_dir()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        log_file = state_dir / f"{state_scope}-email-sent.tsv"
        safe_subject = subject.replace("\t", " ").replace("\n", " ")
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\t{safe_subject}\n")
    except OSError:
        pass


def fake_send_log_path() -> Path | None:
    value = os.environ.get("EMAIL_ME_FAKE_SEND_LOG", "")
    return Path(value) if value else None


def maybe_print_thread_reminder() -> None:
    if secrets.randbelow(8) == 0:
        print("Tip: omit --subject to continue this tmux window's latest email thread.")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    guest_reply_source = ""
    try:
        subject_tmux_target = footer_tmux_target(args.tmux_target, args.manager_human)
        if args.manager_human and subject_tmux_target is not None:
            validate_manager_route_identity(args.tmux_target, subject_tmux_target)
        if args.manager_human and guest_hees_tmux_target(subject_tmux_target) and not args.guest_hees:
            args = dataclass_replace(args, guest_hees=True)
        if args.manager_human and subject_tmux_target is None:
            raise ValueError("manager-human email requires a tmux target.")
        if args.guest_image_references and not args.guest_hees:
            raise ValueError("--guest-image-reference requires a guest_hees producer target.")
        try:
            split_settings = configured_agent_mail() if configured_agent_mail is not None else None
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if args.manager_human and split_settings is None:
            raise ValueError("manager-human email requires split email configuration")
        if args.guest_hees:
            if split_settings is None or guest_hees_mail is None:
                raise ValueError("guest-hees email requires split email configuration")
            split_settings = guest_hees_mail(split_settings)
            if split_settings.human_address != GUEST_HEES_ADDRESS:
                raise ValueError("guest-hees recipient configuration is not pinned")
        elif args.manager_human and split_settings is not None and split_settings.human_address.casefold() == GUEST_HEES_ADDRESS.casefold():
            raise ValueError("primary email route must not use the pinned guest recipient")
        route_profile = None
        if args.manager_human:
            if MailRouteProfile is None or split_settings is None:
                raise ValueError("manager-human route-profile validation is unavailable")
            route_profile = MailRouteProfile(
                agent_address=split_settings.agent_address,
                counterparty_address=split_settings.human_address,
                route_kind="guest-hees" if args.guest_hees else "primary",
                parent_message_ids=open_guest_hees_reply_message_ids(manager_state_dir()) if args.guest_hees else None,
            )
        if args.title is None:
            if subject_tmux_target is None:
                raise ValueError("email without a subject requires an inferred tmux target.")
            if prepare_latest_thread_for_tmux_target is None:
                raise ValueError("email thread lookup is unavailable; pass --subject or --subject-file.")
            if route_profile is None:
                subject, reply_headers = prepare_latest_thread_for_tmux_target(subject_tmux_target)
            else:
                subject, reply_headers = prepare_latest_thread_for_tmux_target(subject_tmux_target, route_profile=route_profile)
            title = subject
        elif prepare_subject_and_headers is not None:
            if route_profile is None:
                subject, reply_headers = prepare_subject_and_headers(args.title, subject_tmux_target or "")
            else:
                subject, reply_headers = prepare_subject_and_headers(args.title, subject_tmux_target or "", route_profile=route_profile)
            title = args.title
        else:
            subject, reply_headers = normalize_subject(args.title, subject_tmux_target or ""), {}
            title = args.title
        if args.manager_human:
            try:
                validate_manager_human_subject(subject)
            except ValueError:
                if BRACKETED_TMUX_TAG_RE.findall(subject):
                    raise
                # Some inbound human subjects are untagged.  Reply safely by
                # rebuilding the prepared subject with the known sender
                # target, rather than rejecting a valid acknowledgement.
                subject = normalize_subject(subject, subject_tmux_target or "")
                validate_manager_human_subject(subject)
        if args.guest_hees:
            if not substantive_guest_reply(args.content):
                raise ValueError("guest-hees reply must contain a substantive guest-facing answer")
            if not subject.casefold().startswith("re:") or not verified_guest_reply_headers(reply_headers):
                raise ValueError("guest-hees reply must continue one verified guest email thread")
            guest_reply_source = open_guest_hees_reply_source(manager_state_dir(), reply_headers["In-Reply-To"])
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.manager_human and route_profile is not None:
        if split_settings is None or (
            split_settings.agent_address.casefold() != route_profile.agent_address.casefold()
            or split_settings.human_address.casefold() != route_profile.counterparty_address.casefold()
        ):
            print("outbound email settings do not match the verified route profile", file=sys.stderr)
            return 2
    guest_images: tuple[ValidatedImage, ...] = ()
    if args.guest_image_references:
        if reply_attachments is None:
            print("guest image validation is unavailable", file=sys.stderr)
            return 2
        try:
            guest_images = reply_attachments(args.guest_image_references, recipient=GUEST_HEES_ADDRESS)
        except GuestImageError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    add_pwd_footer = False if split_settings is not None else args.add_pwd_footer or args.manager_human
    if args.dry_run:
        body = append_pwd_footer(args.content, tmux_target=subject_tmux_target, require_unquoted_footer=args.manager_human) if add_pwd_footer else args.content
        print(f"dry-run: email not sent; subject={subject}; body-bytes={len(body.encode())}")
        return 0
    dedupe_subject = normalized_subject_key(title) if args.manager_human and normalized_subject_key is not None else subject
    dedupe_content = args.content + "\0" + "\0".join(args.guest_image_references)
    state_scope = "guest-hees" if args.guest_hees else "human"
    if args.manager_human and not args.guest_hees and not should_send_manager_email_key(
        dedupe_subject, subject, dedupe_content, state_scope
    ):
        print("Skipped duplicate human email")
        return 0
    if fake_log := fake_send_log_path():
        if args.guest_hees:
            print("EMAIL_ME_FAKE_SEND_LOG cannot verify a guest reply", file=sys.stderr)
            return 2
        fake_log.parent.mkdir(parents=True, exist_ok=True)
        fake_log.write_text(f"{subject}\n{args.content}", encoding="utf-8")
        if args.manager_human:
            log_manager_email(subject, state_scope)
            print("Emailed the human")
        else:
            print("Email sent.")
        maybe_print_thread_reminder()
        return 0
    if split_settings is not None:
        sender_email = split_settings.agent_address
        recipient_email = split_settings.human_address
        app_password = split_settings.app_password
    else:
        env_values = parse_env_file(ENV_FILE_PATH)
        sender_email = env_values.get("EMAIL_ME_GMAIL_ADDRESS", "")
        recipient_email = sender_email
        app_password = env_values.get("EMAIL_ME_GMAIL_APP_PASSWORD", "")

    if not sender_email or not app_password:
        hint = "Set OMO_AGENT_GMAIL_ADDRESS, OMO_AGENT_GMAIL_APP_PASSWORD, and OMO_HUMAN_EMAIL_ADDRESS in ~/.config/omo_manager/local.env."
        print(
            hint,
            file=sys.stderr,
        )
        return 2
    if "@" not in sender_email or any(ch.isspace() for ch in sender_email):
        print("Invalid Gmail address format.", file=sys.stderr)
        return 2

    guest_claim = acquire_guest_reply_claim(manager_state_dir(), guest_reply_source) if args.guest_hees else None
    if args.guest_hees and guest_claim is None:
        print("Guest reply obligation claim is unavailable; retry after local state recovers", file=sys.stderr)
        return 1
    if args.guest_hees:
        prior_attempt = load_guest_reply_attempt(
            manager_state_dir(), guest_reply_source, reply_headers["In-Reply-To"], sender_email
        )
        if guest_reply_attempt_path(manager_state_dir(), guest_reply_source).exists() and prior_attempt is None:
            guest_claim.close()
            print("Guest reply prior attempt is invalid; refusing SMTP", file=sys.stderr)
            return 1
        prior_evidence = verify_guest_reply_in_sent(split_settings, prior_attempt) if prior_attempt is not None else None
        if prior_attempt is not None and prior_evidence is not None:
            try:
                source = fulfill_guest_hees_reply_obligation(
                    manager_state_dir(),
                    str(prior_attempt["In-Reply-To"]),
                    str(prior_attempt["Message-ID"]),
                    prior_evidence.subject_sha256,
                    prior_evidence.body_sha256,
                )
            except OSError as exc:
                guest_claim.close()
                print(f"Guest reply evidence could not be recorded: {exc}", file=sys.stderr)
                return 1
            guest_claim.close()
            print(f"Guest reply verified in Sent Mail for {source}")
            return 0

    smtp_uncertain = False
    try:
        msg = build_message(
            sender_email=sender_email,
            title=title,
            content=args.content,
            add_pwd_footer=add_pwd_footer,
            prepared_subject=subject,
            reply_headers=reply_headers,
            tmux_target=subject_tmux_target,
            manager_human=args.manager_human,
            recipient_email=recipient_email,
            supersedes_message_ids=args.supersedes_message_ids,
            agent_session=agent_session_id(),
        )
        attach_guest_images(msg, guest_images)
    except ValueError as exc:
        if args.guest_hees:
            guest_claim.close()
        print(str(exc), file=sys.stderr)
        return 2
    if args.guest_hees and not store_guest_reply_attempt(manager_state_dir(), guest_reply_source, msg):
        guest_claim.close()
        print("Guest reply attempt could not be recorded before SMTP", file=sys.stderr)
        return 1
    try:
        ssl_context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            host=SMTP_HOST,
            port=SMTP_PORT,
            timeout=30,
            context=ssl_context,
        ) as smtp:
            _ = smtp.login(sender_email, app_password)
            _ = smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        if args.guest_hees:
            guest_claim.close()
        print(
            "Authentication failed. Ensure Gmail 2-Step Verification is enabled and use a valid app password.",
            file=sys.stderr,
        )
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        print(f"Email send failed: {exc}", file=sys.stderr)
        print(f"Delivery-uncertain Message-ID: {msg['Message-ID']}", file=sys.stderr)
        smtp_uncertain = True

    if args.guest_hees:
        sent_evidence = verify_guest_reply_in_sent(split_settings, msg) if split_settings is not None else None
        if sent_evidence is None:
            guest_claim.close()
            print(f"Guest reply delivery is unverified; Message-ID: {msg['Message-ID']}", file=sys.stderr)
            return 1
        try:
            source = fulfill_guest_hees_reply_obligation(
                manager_state_dir(),
                str(msg["In-Reply-To"]),
                str(msg["Message-ID"]),
                sent_evidence.subject_sha256,
                sent_evidence.body_sha256,
            )
        except OSError as exc:
            guest_claim.close()
            print(f"Guest reply evidence could not be recorded: {exc}", file=sys.stderr)
            return 1
        guest_claim.close()
        print(f"Guest reply verified in Sent Mail for {source}")
    elif smtp_uncertain:
        return 1

    if args.manager_human:
        log_manager_email(subject, state_scope)
        print("Emailed the human")
    else:
        print("Email sent.")
    print(f"Message-ID: {msg['Message-ID']}")
    maybe_print_thread_reminder()
    return 0


if __name__ == "__main__":
    exit(main(sys.argv[1:]))
