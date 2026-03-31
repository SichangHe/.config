#!/usr/bin/env python3
"""Send a plain text email to yourself using Gmail SMTP.

Credentials are loaded from `~/.config/.env`:
- `EMAIL_ME_GMAIL_ADDRESS`
- `EMAIL_ME_GMAIL_APP_PASSWORD`
"""

from __future__ import annotations

import argparse
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
ENV_FILE_PATH = Path.home() / ".config" / ".env"


@dataclass(frozen=True)
class CliArgs:
    title: str
    content: str


class ParsedArgs(argparse.Namespace):
    title: str = ""
    content: str = ""


def parse_args(argv: list[str]) -> CliArgs:
    parser = argparse.ArgumentParser(
        description="Send a plain text email to your own Gmail inbox."
    )
    _ = parser.add_argument("title", type=str, help="Email subject/title")
    _ = parser.add_argument("content", type=str, help="Email plain text body")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    title = parsed.title
    content = parsed.content
    for ch in title:
        codepoint = ord(ch)
        if codepoint < 32 or codepoint == 127:
            parser.error("`title` must not contain control characters.")
    return CliArgs(title=title, content=content)


def build_message(sender_email: str, title: str, content: str) -> EmailMessage:
    msg = EmailMessage()
    msg.add_header("Subject", title)
    msg.add_header("From", sender_email)
    msg.add_header("To", sender_email)
    msg.set_content(content)
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

    msg = build_message(
        sender_email=sender_email, title=args.title, content=args.content
    )

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
