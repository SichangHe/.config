#!/usr/bin/env python3
"""Email the human using Gmail SMTP.

Credentials are loaded from `~/.config/.env`:
- `EMAIL_ME_GMAIL_ADDRESS`
- `EMAIL_ME_GMAIL_APP_PASSWORD`
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
ENV_FILE_PATH = Path.home() / ".config" / ".env"
MANAGER_DIR = Path(__file__).resolve().parents[1] / "omo_manager"
if MANAGER_DIR.is_dir():
    sys.path.insert(0, str(MANAGER_DIR))
try:
    from omo_email_subject import SubjectInputError, canonical_tmux_target, normalized_subject_key, prepare_subject, prepare_subject_and_headers, reply_headers_for_subject, strip_leading_tmux_tags
except ImportError:
    SubjectInputError = ValueError
    canonical_tmux_target = None
    normalized_subject_key = None
    prepare_subject = None
    prepare_subject_and_headers = None
    reply_headers_for_subject = None
    strip_leading_tmux_tags = None

MANAGER_PREFIX = "[a]"
PWD_FOOTER_RE = re.compile(r"(?:^|\n)(?:>\s*)?PWD: [^\n]+\n?\Z")
TMUX_WINDOW_RE = re.compile(r"[^:\n]+:\d+(?:\.\d+)?\Z")
TMUX_SUBJECT_TAG_RE = re.compile(r"^\s*(?:\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]|[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?:\s+|$)")
BRACKETED_TMUX_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]")
MANAGER_HUMAN_SUBJECT_RE = re.compile(r"^(?:Re:\s*)?\[a\]\s+\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\](?:\s+|$)", re.IGNORECASE)
TMUX_FOOTER_RE = re.compile(r"(?:^|\n)tmux: [^:\n]+:\d+(?:\.\d+)?\r?\n?\Z", re.IGNORECASE)
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
    title: str
    content: str
    dry_run: bool
    add_pwd_footer: bool
    manager_human: bool
    tmux_target: str | None


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


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        usage="email_me.py [--dry-run] (--subject TEXT | --subject-file FILE) [--message-file FILE]",
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
    _ = parser.add_argument("--no-pwd-footer", action="store_true", help="Send the body exactly as provided, without appending a PWD footer.")
    _ = parser.add_argument("--tmux-target", help="Normally omit: the helper infers producer identity from the launch environment, then the current pane. Override only to preserve a different verified producer identity; never pass a task owner or delivery target.")
    _ = parser.add_argument("--sender-tmux-target", dest="sender_tmux_target", help="Alias for --tmux-target; use only when forwarding or compressing mail while preserving a different verified producer identity.")
    _ = parser.add_argument("--manager-human", action="store_true", help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.legacy_args:
        parser.error("pass email subject with --subject or --subject-file; pass email body by standard input or --message-file.")
    if parsed.subject is not None and parsed.subject_file is not None:
        parser.error("pass email subject with --subject or --subject-file, not both.")
    if parsed.subject is None and parsed.subject_file is None:
        parser.error("email subject required.")
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
        assert parsed.subject is not None
        title = parsed.subject
    if parsed.message_file is not None:
        try:
            content = parsed.message_file.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"message file not readable: {exc}")
    else:
        content = sys.stdin.read()
    for ch in title:
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
    return CliArgs(title=title, content=content, dry_run=parsed.dry_run, add_pwd_footer=not parsed.no_pwd_footer, manager_human=parsed.manager_human, tmux_target=tmux_target)


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
        raise ValueError("agent email subject must use [a]; [omo] is deprecated")
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
        return f"Re: {MANAGER_PREFIX} {base}"
    if reply:
        return f"Re: {MANAGER_PREFIX} {base}"
    return f"{MANAGER_PREFIX} {base}"


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
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S:#I"],
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


def footer_tmux_target(explicit_tmux_target: str | None = None, manager_human: bool = False) -> str | None:
    if explicit_tmux_target is not None:
        if not valid_tmux_target(explicit_tmux_target):
            raise ValueError("tmux target must have shape session:window or session:window.pane.")
        return canonical_email_tmux_target(explicit_tmux_target)
    if manager_human:
        # A worker can inherit its manager's target while it still has an
        # authoritative agent target.  Attribute direct worker mail to that
        # worker, not to the manager that launched it.
        return env_tmux_target() or env_manager_tmux_target() or current_tmux_window()
    return env_tmux_target() or current_tmux_window()


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


def append_pwd_footer(content: str, cwd: str | Path | None = None, tmux_target: str | None = None) -> str:
    if PWD_FOOTER_RE.search(content) or TMUX_FOOTER_RE.search(content):
        return content
    tmux_window = footer_tmux_target(tmux_target)
    if tmux_window is not None:
        footer = f"tmux: {tmux_window}"
    else:
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


def build_message(sender_email: str, title: str, content: str, add_pwd_footer: bool = True, prepared_subject: str | None = None, reply_headers: dict[str, str] | None = None, tmux_target: str | None = None) -> EmailMessage:
    source_target = footer_tmux_target(tmux_target)
    msg = EmailMessage()
    msg.add_header("Subject", prepared_subject or normalize_subject(title, source_target or ""))
    msg.add_header("From", sender_email)
    msg.add_header("To", sender_email)
    if reply_headers is not None:
        for name, value in reply_headers.items():
            msg.add_header(name, value)
    elif reply_headers_for_subject is not None:
        for name, value in reply_headers_for_subject(title).items():
            msg.add_header(name, value)
    body = append_pwd_footer(content, tmux_target=source_target) if add_pwd_footer else content
    msg.set_content(markdown_links_to_plain(body))
    msg.add_alternative(markdown_to_html(body), subtype="html")
    return msg


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


def should_send_manager_email_key(dedupe_subject: str, display_subject: str, content: str) -> bool:
    try:
        state_dir = manager_state_dir()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        dedupe_s = int(os.environ.get("OMO_MANAGER_EMAIL_DEDUPE_S", "300"))
        dedupe_file = state_dir / "human-email-dedupe.tsv"
        lock_file = state_dir / "human-email-dedupe.lock"
        digest = hashlib.sha256(dedupe_subject.encode() + b"\0" + content.encode()).hexdigest()
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


def log_manager_email(subject: str) -> None:
    try:
        state_dir = manager_state_dir()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        log_file = state_dir / "human-email-sent.tsv"
        safe_subject = subject.replace("\t", " ").replace("\n", " ")
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\t{safe_subject}\n")
    except OSError:
        pass


def fake_send_log_path() -> Path | None:
    value = os.environ.get("EMAIL_ME_FAKE_SEND_LOG", "")
    return Path(value) if value else None


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        subject_tmux_target = footer_tmux_target(args.tmux_target, args.manager_human)
        if args.manager_human and subject_tmux_target is None:
            raise ValueError("manager-human email requires a tmux target.")
        if prepare_subject_and_headers is not None:
            subject, reply_headers = prepare_subject_and_headers(args.title, subject_tmux_target or "")
        else:
            subject, reply_headers = normalize_subject(args.title, subject_tmux_target or ""), {}
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
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        body = append_pwd_footer(args.content, tmux_target=subject_tmux_target) if args.add_pwd_footer else args.content
        print(f"dry-run: email not sent; subject={subject}; body-bytes={len(body.encode())}")
        return 0
    dedupe_subject = normalized_subject_key(args.title) if args.manager_human and normalized_subject_key is not None else subject
    if args.manager_human and not should_send_manager_email_key(dedupe_subject, subject, args.content):
        print("Skipped duplicate human email")
        return 0
    if fake_log := fake_send_log_path():
        fake_log.parent.mkdir(parents=True, exist_ok=True)
        fake_log.write_text(f"{subject}\n{args.content}", encoding="utf-8")
        if args.manager_human:
            log_manager_email(subject)
            print("Emailed the human")
        else:
            print("Email sent.")
        return 0
    env_values = parse_env_file(ENV_FILE_PATH)
    sender_email = env_values.get("EMAIL_ME_GMAIL_ADDRESS", "")
    app_password = env_values.get("EMAIL_ME_GMAIL_APP_PASSWORD", "")

    if not sender_email or not app_password:
        hint = f"Set EMAIL_ME_GMAIL_ADDRESS and EMAIL_ME_GMAIL_APP_PASSWORD in {ENV_FILE_PATH}."
        print(
            hint,
            file=sys.stderr,
        )
        return 2
    if "@" not in sender_email or any(ch.isspace() for ch in sender_email):
        print("Invalid Gmail address format.", file=sys.stderr)
        return 2

    try:
        msg = build_message(
            sender_email=sender_email,
            title=args.title,
            content=args.content,
            add_pwd_footer=args.add_pwd_footer,
            prepared_subject=subject,
            reply_headers=reply_headers,
            tmux_target=subject_tmux_target,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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
        print(
            "Authentication failed. Ensure Gmail 2-Step Verification is enabled and use a valid app password.",
            file=sys.stderr,
        )
        return 1
    except (OSError, smtplib.SMTPException) as exc:
        print(f"Email send failed: {exc}", file=sys.stderr)
        return 1

    if args.manager_human:
        log_manager_email(subject)
        print("Emailed the human")
    else:
        print("Email sent.")
    return 0


if __name__ == "__main__":
    exit(main(sys.argv[1:]))
