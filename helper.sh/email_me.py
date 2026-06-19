#!/usr/bin/env python3
"""Email the human using Gmail SMTP, with Markdown HTML alternatives.

Credentials are loaded from `~/.config/.env`:
- `EMAIL_ME_GMAIL_ADDRESS`
- `EMAIL_ME_GMAIL_APP_PASSWORD`
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from pathlib import Path

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
ENV_FILE_PATH = Path.home() / ".config" / ".env"
DIRECT_AGENT_PREFIX = "[omo]"
PRESERVED_PREFIXES = ("[omo]", "[a]", "[omo_manager]", "[omo_manager_recover]")
PWD_FOOTER_RE = re.compile(r"(?:^|\n)(?:>\s*)?PWD: [^\n]+\n?\Z")
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


class ParsedArgs(argparse.Namespace):
    title: str = ""
    content: str = ""
    message_file: Path | None = None
    dry_run: bool = False


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        usage="email_me.py [--dry-run] [--message-file FILE] SUBJECT",
        description=(
            "Email the human. The body accepts Markdown input, but plain text is preferred. "
            "Reads the email body from standard input by default; "
            "use --message-file for a saved body. Do not pass body text as a shell argument."
        ),
    )
    _ = parser.add_argument("title", metavar="SUBJECT", type=str, help="Email subject/title.")
    _ = parser.add_argument("content", nargs="?", type=str, help=argparse.SUPPRESS)
    _ = parser.add_argument("--message-file", type=Path, help="Read the email body from this file instead of stdin.")
    _ = parser.add_argument("--dry-run", action="store_true", help="Validate without sending.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    title = parsed.title
    if parsed.content is not None and parsed.message_file is not None:
        parser.error("pass email body by standard input or --message-file, not both.")
    if parsed.content is not None:
        parser.error(
            "pass email body by standard input or --message-file, not as a shell argument; "
            "shells can expand $, backticks, command substitutions, and redirection-like text before this helper runs."
        )
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
    return CliArgs(title=title, content=content, dry_run=parsed.dry_run)


def normalize_subject(title: str) -> str:
    stripped = title.lstrip()
    lowered = stripped.lower()
    if re.match(r"re: *(?:\[a\]|\[omo_manager\])", lowered):
        return stripped
    if lowered.startswith("re:"):
        raise ValueError("Email subject must not start with `Re:`.")
    if lowered.startswith(DIRECT_AGENT_PREFIX):
        return f"{DIRECT_AGENT_PREFIX}{stripped[len(DIRECT_AGENT_PREFIX):]}"
    if lowered.startswith(PRESERVED_PREFIXES[1:]):
        return stripped
    return f"{DIRECT_AGENT_PREFIX} {stripped}"


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


def append_pwd_footer(content: str, cwd: str | Path | None = None) -> str:
    if PWD_FOOTER_RE.search(content):
        return content
    footer = f"PWD: {cwd or current_pwd()}"
    body = content.rstrip("\n")
    return f"{body}\n\n{footer}\n"


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
        unordered = UNORDERED_LIST_RE.match(line)
        ordered = ORDERED_LIST_RE.match(line)
        if unordered is not None or ordered is not None:
            flush_paragraph()
            flush_quote()
            kind = "ul" if unordered is not None else "ol"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_items.append((unordered or ordered).group(1))
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


def build_message(sender_email: str, title: str, content: str) -> EmailMessage:
    msg = EmailMessage()
    msg.add_header("Subject", normalize_subject(title))
    msg.add_header("From", sender_email)
    msg.add_header("To", sender_email)
    body = append_pwd_footer(content)
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


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.dry_run:
        try:
            subject = normalize_subject(args.title)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        body = append_pwd_footer(args.content)
        print(f"dry-run: email not sent; subject={subject}; body-bytes={len(body.encode())}")
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
            sender_email=sender_email, title=args.title, content=args.content
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

    print("Email sent.")
    return 0


if __name__ == "__main__":
    exit(main(sys.argv[1:]))
