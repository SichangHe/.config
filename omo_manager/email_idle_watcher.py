#!/usr/bin/env python3
"""IMAP IDLE watcher for `Re: [omo_manager]` replies; stores `.txt` and pushes refs."""
from __future__ import annotations

import argparse
import os
import imaplib
import logging
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from email import policy
from email.utils import parseaddr
from email.message import Message
from email.parser import BytesParser
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "http://127.0.0.1:18790")
DEFAULT_MAIL_DIR = Path(os.environ.get("OMO_MANAGER_MAIL_DIR", default_state_dir() / "mail"))
CONFIG_PATH = Path(os.environ.get("OMO_EMAIL_CONFIG_PATH", Path.home() / ".config/himalaya/config.toml"))
SUBJECT_PREFIX = "Re: [omo_manager]"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class Args:
    root: Path
    manager_url: str
    mail_dir: Path
    once: bool
    self_email: str


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    manager_url: str = DEFAULT_MANAGER_URL
    mail_dir: Path = DEFAULT_MAIL_DIR
    once: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manager-url", default=DEFAULT_MANAGER_URL)
    parser.add_argument("--mail-dir", type=Path, default=DEFAULT_MAIL_DIR)
    parser.add_argument("--once", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.root, parsed.manager_url.rstrip("/"), parsed.mail_dir, parsed.once, "")


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


def message_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    raw = msg.get_payload()
    return raw if isinstance(raw, str) else ""


def from_self(sender: str, self_email: str) -> bool:
    return parseaddr(sender)[1].lower() == self_email.lower()


def append_pending(root: Path, txt_path: Path) -> int:
    manager_file = root / "work_manager.md"
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    block = ["", "(pending)", f"[source: email {txt_path}]", "[summary: human reply to manager]"]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def handle_unseen(client: imaplib.IMAP4_SSL, args: Args) -> None:
    typ, data = client.uid("search", "UNSEEN", "SUBJECT", f'"{SUBJECT_PREFIX}"')
    if typ != "OK" or not data or not data[0]:
        return
    args.mail_dir.mkdir(parents=True, exist_ok=True)
    for raw_uid in data[0].split():
        uid = raw_uid.decode()
        typ_msg, msg_data = client.uid("fetch", uid, "(RFC822)")
        if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            continue
        msg = BytesParser(policy=policy.default).parsebytes(msg_data[0][1])
        sender = str(msg.get("From", ""))
        subject = str(msg.get("Subject", ""))
        if not subject.startswith(SUBJECT_PREFIX) or not from_self(sender, args.self_email):
            continue
        txt_path = args.mail_dir / f"{uid}.txt"
        args.mail_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.mail_dir.chmod(0o700)
        body = f"From: {sender}\nSubject: {subject}\nDate: {msg.get('Date', '')}\nUID: {uid}\n\n{message_text(msg)}"
        fd = os.open(txt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        line_no = append_pending(args.root, txt_path)
        subprocess.run([str(Path.home() / ".config/omo_manager/omo_push_to_manager.py"), f"email: file=work_manager.md line={line_no} txt={txt_path}", "--manager-url", args.manager_url, "--root", str(args.root), "--submit"], check=False)


def idle_once(client: imaplib.IMAP4_SSL, wait_s: float) -> None:
    tag = "OMOIDLE"
    client.send(f"{tag} IDLE\r\n".encode())
    while True:
        line = client.readline()
        if line.decode("utf-8", errors="ignore").startswith("+"):
            break
    readable, _, _ = select.select([client.socket()], [], [], wait_s)
    client.send(b"DONE\r\n")
    while True:
        line = client.readline().decode("utf-8", errors="ignore")
        if tag in line:
            break
    if readable:
        return


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config = parse_env_config(CONFIG_PATH)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        print(f"email_idle_watcher: missing config keys {sorted(missing)} in {CONFIG_PATH}", file=sys.stderr)
        return 1
    safe_args = Args(args.root, args.manager_url, args.mail_dir, args.once, config["user"])
    while True:
        try:
            with imaplib.IMAP4_SSL(config["host"]) as client:
                client.login(config["user"], config["password"])
                client.select("INBOX")
                handle_unseen(client, safe_args)
                if safe_args.once:
                    return 0
                while True:
                    idle_once(client, 600.0)
                    client.select("INBOX")
                    handle_unseen(client, safe_args)
        except Exception as exc:
            logging.error("email watcher failed: %s", exc)
            if safe_args.once:
                return 1
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
