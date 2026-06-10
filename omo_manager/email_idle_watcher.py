#!/usr/bin/env python3
"""IMAP IDLE watcher for manager emails; stores repo-local `.txt` files and pushes refs."""
from __future__ import annotations

import argparse
import os
import fcntl
import imaplib
import logging
import re
import select
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from email import policy
from email.utils import parseaddr
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


def dated_manager_file(root: Path) -> Path:
    return root / f"work_manager_{datetime.now().astimezone().strftime('%Y-%m-%d')}.md"

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "http://127.0.0.1:18790")
DEFAULT_MANAGER_TARGET = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
DEFAULT_MAIL_DIR = Path(os.environ.get("OMO_MANAGER_MAIL_DIR", DEFAULT_ROOT / "manager_mail"))
CONFIG_PATH = Path(os.environ.get("OMO_EMAIL_CONFIG_PATH", Path.home() / ".config/himalaya/config.toml"))
MANAGER_REPLY_PREFIX = "Re: [omo_manager]"
AGENT_REPLY_PREFIX = "Re: [omo]"
AGENT_REPLY_SEARCH_PREFIXES = (AGENT_REPLY_PREFIX, "Re: [OMO]")
NORMAL_REPLY_PREFIXES = (MANAGER_REPLY_PREFIX, AGENT_REPLY_PREFIX)
NORMAL_REPLY_SEARCH_PREFIXES = (MANAGER_REPLY_PREFIX, *AGENT_REPLY_SEARCH_PREFIXES)
RECOVERY_SUBJECTS = {"[omo_manager_recover]", "Re: [omo_manager_recover]"}
# Fail closed by default: raw email can contain sender-injected Authentication-Results.
# This opt-in is an operator/admin-trusted escape hatch for mailbox/provider setups
# known to strip or control forged Authentication-Results before delivery; it is
# not cryptographic proof that a matching header was provider-inserted.
TRUST_RECOVERY_AUTH_RESULTS = os.environ.get("OMO_MANAGER_TRUST_RECOVERY_AUTH_RESULTS", "").lower() in {"1", "true", "yes"}
_configured_auth_servers = tuple(
    value.strip().lower()
    for value in os.environ.get("OMO_MANAGER_TRUSTED_AUTH_SERVERS", "mx.google.com").split(",")
    if value.strip()
)
TRUSTED_AUTH_SERVERS = _configured_auth_servers or ("mx.google.com",)
DEFAULT_RECOVERY_DEBOUNCE_S = int(os.environ.get("OMO_MANAGER_RECOVERY_DEBOUNCE_S", "900"))
DEFAULT_IDLE_WAIT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_WAIT_S", "60"))
DEFAULT_IMAP_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IMAP_TIMEOUT_S", str(max(90.0, DEFAULT_IDLE_WAIT_S + 30.0))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class Args:
    root: Path
    manager_url: str
    mail_dir: Path
    state_dir: Path
    manager_file: Path | None
    once: bool
    self_email: str
    recovery_debounce_s: int
    restart_script: Path
    idle_wait_s: float = DEFAULT_IDLE_WAIT_S
    manager_target: str = ""
    imap_timeout_s: float = DEFAULT_IMAP_TIMEOUT_S


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    manager_url: str = DEFAULT_MANAGER_URL
    manager_target: str = DEFAULT_MANAGER_TARGET
    mail_dir: Path = DEFAULT_MAIL_DIR
    state_dir: Path = default_state_dir()
    manager_file: Path | None = None
    once: bool = False
    recovery_debounce_s: int = DEFAULT_RECOVERY_DEBOUNCE_S
    restart_script: Path = Path.home() / ".config/omo_manager/omo_manager_restart.sh"
    idle_wait_s: float = DEFAULT_IDLE_WAIT_S
    imap_timeout_s: float = DEFAULT_IMAP_TIMEOUT_S


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manager-url", default=DEFAULT_MANAGER_URL)
    parser.add_argument("--manager-target", default=DEFAULT_MANAGER_TARGET)
    parser.add_argument("--mail-dir", type=Path, default=DEFAULT_MAIL_DIR)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--manager-file", type=Path, default=None)
    parser.add_argument("--recovery-debounce-s", type=int, default=DEFAULT_RECOVERY_DEBOUNCE_S)
    parser.add_argument("--restart-script", type=Path, default=Path(os.environ.get("OMO_MANAGER_RECOVERY_RESTART_SCRIPT", Path.home() / ".config/omo_manager/omo_manager_restart.sh")))
    parser.add_argument("--idle-wait-s", type=float, default=DEFAULT_IDLE_WAIT_S, help="Maximum IMAP IDLE wait before polling again; lower values reduce perceived missed-email latency")
    parser.add_argument("--imap-timeout-s", type=float, default=DEFAULT_IMAP_TIMEOUT_S, help="Socket timeout for IMAP operations; prevents silent permanent IDLE/readline hangs")
    parser.add_argument("--once", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    root = parsed.root
    manager_file = parsed.manager_file
    if manager_file is not None and not manager_file.is_absolute():
        manager_file = root / manager_file
    return Args(root, parsed.manager_url.rstrip("/"), parsed.mail_dir, parsed.state_dir, manager_file, parsed.once, "", parsed.recovery_debounce_s, parsed.restart_script, parsed.idle_wait_s, parsed.manager_target.strip(), parsed.imap_timeout_s)


def current_manager_file(args: Args) -> Path:
    return args.manager_file or dated_manager_file(args.root)


def args_w_manager_file(args: Args, manager_file: Path) -> Args:
    return replace(args, manager_file=manager_file)


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


def email_domain(address: str) -> str:
    parsed = parseaddr(address)[1].lower()
    if "@" not in parsed:
        return ""
    return parsed.rsplit("@", 1)[1]


def auth_segment_passes(segment: str, method: str, prop: str, domain: str) -> bool:
    clean = " ".join(segment.lower().split())
    escaped_domain = re.escape(domain.lower())
    if not re.search(rf"(?:^|\s){re.escape(method)}=pass(?:\s|$)", clean):
        return False
    if prop == "header.i":
        return re.search(rf"(?:^|\s)header\.i=(?:[^@\s;]+@|@)?{escaped_domain}(?:\s|$)", clean) is not None
    if prop == "smtp.mailfrom":
        return re.search(rf"(?:^|\s)smtp\.mailfrom={escaped_domain}(?:\s|$)", clean) is not None
    return False


def recovery_sender_authenticated(msg: Message, self_email: str) -> bool:
    if not TRUST_RECOVERY_AUTH_RESULTS:
        return False
    domain = email_domain(self_email)
    if not domain:
        return False
    for header in msg.get_all("Authentication-Results", []):
        header_parts = str(header).split(";")
        authserv_id = header_parts[0].strip().lower()
        if TRUSTED_AUTH_SERVERS and authserv_id not in TRUSTED_AUTH_SERVERS:
            continue
        for segment in header_parts[1:]:
            if auth_segment_passes(segment, "dkim", "header.i", domain) or auth_segment_passes(segment, "spf", "smtp.mailfrom", domain):
                return True
    return False


def is_manager_subject(subject: str) -> bool:
    normalized = subject.lower()
    return normalized.startswith(tuple(prefix.lower() for prefix in NORMAL_REPLY_PREFIXES))


def is_recovery_subject(subject: str) -> bool:
    return " ".join(subject.split()) in RECOVERY_SUBJECTS


def manager_url_is_loopback(manager_url: str) -> bool:
    parsed = urlparse(manager_url.rstrip("/"))
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is not None


def source_ref(root: Path, txt_path: Path) -> Path:
    try:
        return txt_path.relative_to(root)
    except ValueError:
        return txt_path


def processed_uids_path(args: Args) -> Path:
    return args.state_dir / "email-processed-uids.tsv"


def load_processed_uids(path: Path) -> set[str]:
    try:
        return {line.split("\t", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def save_processed_uids(path: Path, uids: set[str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    now_s = int(time.time())
    body = "".join(f"{uid}\t{now_s}\n" for uid in sorted(uids, key=lambda value: int(value) if value.isdigit() else value))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)


def email_source_lines(root: Path, txt_path: Path) -> tuple[str, str]:
    ref = source_ref(root, txt_path)
    return f"(from email {ref})", f"[source: email {ref}]"


def existing_source_line(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    manager_file = manager_file or dated_manager_file(root)
    if not manager_file.exists():
        return None
    source_lines = set(email_source_lines(root, txt_path))
    lines = manager_file.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if line.strip() in source_lines:
            return idx + 1
    return None


def existing_source_pending_line(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    manager_file = manager_file or dated_manager_file(root)
    source_line = existing_source_line(root, txt_path, manager_file)
    if source_line is None or source_line <= 1:
        return None
    lines = manager_file.read_text(encoding="utf-8").splitlines()
    for pending_idx in range(max(0, source_line - 4), source_line - 1):
        if lines[pending_idx].strip() == "(pending)":
            return pending_idx + 1
    return None


def append_pending(root: Path, txt_path: Path, manager_file: Path | None = None) -> int:
    manager_file = manager_file or dated_manager_file(root)
    existing_line = existing_source_pending_line(root, txt_path, manager_file)
    if existing_line is not None:
        return existing_line
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    from_line, _legacy_source_line = email_source_lines(root, txt_path)
    block = ["", "(pending)", from_line]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def push_email_ref(args: Args, line_no: int) -> bool:
    if not args.manager_url and not args.manager_target:
        logging.error("email pending push failed: manager URL or target is required")
        return False
    manager_file = current_manager_file(args)
    ref = manager_file.relative_to(args.root) if manager_file.is_relative_to(args.root) else manager_file
    command = ["omo_push_to_manager.py", f"pending: file={ref} line={line_no} origin=human source=email action=ack-human", "--root", str(args.root), "--submit"]
    command.extend(["--pending-file", str(ref), "--pending-line", str(line_no)])
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
    if args.manager_url:
        command.extend(["--manager-url", args.manager_url])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        logging.error("email pending push failed: uid line=%s status=%s", line_no, result.returncode)
        return False
    return True


def append_recovery_record(root: Path, txt_path: Path, summary: str, manager_file: Path | None = None) -> int:
    manager_file = manager_file or dated_manager_file(root)
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    from_line, legacy_source_line = email_source_lines(root, txt_path)
    block = ["", f"(manager recovery email: {stamp})", from_line, legacy_source_line, summary]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def write_mail(args: Args, uid: str, msg: Message, _sender: str, subject: str) -> Path:
    args.mail_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.mail_dir.chmod(0o700)
    txt_path = args.mail_dir / f"{uid}.txt"
    body = f"Subject: {subject}\n\n{message_text(msg)}"
    fd = os.open(txt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return txt_path


def email_human(args: Args, subject: str, body: str) -> None:
    subprocess.run([str(Path.home() / ".config/omo_manager/omo_email_human.sh"), "--subject", subject], input=body, text=True, check=False)


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def record_recovery_attempt(last_path: Path, now_s: float, uid: str, result: str) -> None:
    last_fd = os.open(last_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(last_fd, "w", encoding="utf-8") as handle:
        handle.write(f"{now_s}\tuid={uid}\tresult={result}\n")


def handle_recovery_email(args: Args, uid: str, txt_path: Path) -> None:
    recovery_dir = args.state_dir / "recovery-email"
    recovery_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    recovery_dir.chmod(0o700)
    lock_path = recovery_dir / "recover.lock"
    last_path = recovery_dir / "last-recover.tsv"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        append_recovery_record(args.root, txt_path, "recovery email recorded; restart already running", args.manager_file)
        os.close(lock_fd)
        return
    try:
        now_s = time.time()
        last_s = 0.0
        if last_path.exists():
            raw_last = last_path.read_text(encoding="utf-8").split("\t", 1)[0]
            try:
                last_s = float(raw_last)
            except ValueError:
                last_s = 0.0
        if args.recovery_debounce_s > 0 and now_s - last_s < args.recovery_debounce_s:
            remaining_s = int(args.recovery_debounce_s - (now_s - last_s))
            append_recovery_record(args.root, txt_path, f"recovery email recorded; restart debounced for {remaining_s}s", args.manager_file)
            return
        if not manager_url_is_loopback(args.manager_url):
            command = [str(args.restart_script), "--manager-url", args.manager_url, "--root", str(args.root)]
            record_recovery_attempt(last_path, now_s, uid, "refused-non-loopback")
            append_recovery_record(args.root, txt_path, "recovery email recorded; restart refused because manager URL is not loopback", args.manager_file)
            email_human(args, "[omo_manager] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart was refused because manager-url is not loopback: {args.manager_url}\n\nRun only after correcting configuration:\n\n```sh\n{shell_join(command)}\n```\n")
            return
        log_path = recovery_dir / f"recover-{uid}-{int(now_s)}.log"
        command = [str(args.restart_script), "--manager-url", args.manager_url, "--root", str(args.root), "--state-dir", str(args.state_dir)]
        record_recovery_attempt(last_path, now_s, uid, "started")
        append_recovery_record(args.root, txt_path, f"recovery email accepted; running `{shell_join(command)}`; log `{log_path}`", args.manager_file)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log_handle:
            try:
                result = subprocess.run(command, stdout=log_handle, stderr=subprocess.STDOUT, check=False)
            except OSError as exc:
                log_handle.write(f"failed to run restart helper: {exc}\n")
                record_recovery_attempt(last_path, now_s, uid, "launch-failed")
                append_recovery_record(args.root, txt_path, f"recovery restart helper could not be launched; see `{log_path}`", args.manager_file)
                email_human(args, "[omo_manager] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart helper launch failed.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
                return
        record_recovery_attempt(last_path, now_s, uid, f"returncode={result.returncode}")
        if result.returncode != 0:
            append_recovery_record(args.root, txt_path, f"recovery restart failed with exit {result.returncode}; see `{log_path}`", args.manager_file)
            email_human(args, "[omo_manager] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart failed with exit {result.returncode}.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def uid_search_range(processed_uids: set[str]) -> str:
    max_uid = max((int(uid) for uid in processed_uids if uid.isdigit()), default=0)
    return f"{max_uid + 1}:*"


def search_uids(client: imaplib.IMAP4_SSL, subject: str, self_email: str, processed_uids: set[str]) -> set[bytes]:
    if processed_uids:
        typ, data = client.uid("search", None, "UID", uid_search_range(processed_uids), "FROM", f'"{self_email}"', "SUBJECT", f'"{subject}"')
    else:
        typ, data = client.uid("search", "UNSEEN", "FROM", f'"{self_email}"', "SUBJECT", f'"{subject}"')
    if typ != "OK" or not data or not data[0]:
        return set()
    return set(data[0].split())


def mark_seen(client: imaplib.IMAP4_SSL, uid: str) -> None:
    client.uid("store", uid, "+FLAGS", r"(\Seen)")


def handle_unseen(client: imaplib.IMAP4_SSL, args: Args) -> None:
    processed_path = processed_uids_path(args)
    processed_uids = load_processed_uids(processed_path)
    processed_changed = False
    manager_file = current_manager_file(args)
    push_args = args_w_manager_file(args, manager_file)
    candidate_uids: set[bytes] = set()
    for subject_prefix in NORMAL_REPLY_SEARCH_PREFIXES:
        candidate_uids.update(search_uids(client, subject_prefix, args.self_email, processed_uids))
    for subject in RECOVERY_SUBJECTS:
        candidate_uids.update(search_uids(client, subject, args.self_email, processed_uids))
    if not candidate_uids:
        logging.info("email scan complete: n=0 processed_next=%s manager_file=%s", uid_search_range(processed_uids), manager_file)
        return
    logging.info("email candidates found: n=%s uids=%s processed_max=%s manager_file=%s", len(candidate_uids), ",".join(uid.decode() for uid in sorted(candidate_uids, key=lambda value: int(value))), uid_search_range(processed_uids), manager_file)
    args.mail_dir.mkdir(parents=True, exist_ok=True)
    for raw_uid in sorted(candidate_uids, key=lambda value: int(value)):
        uid = raw_uid.decode()
        if uid in processed_uids:
            mark_seen(client, uid)
            continue
        expected_txt_path = args.mail_dir / f"{uid}.txt"
        existing_pending_line = existing_source_pending_line(args.root, expected_txt_path, manager_file)
        if existing_pending_line is not None:
            if push_email_ref(push_args, existing_pending_line):
                processed_uids.add(uid)
                processed_changed = True
                mark_seen(client, uid)
            continue
        if existing_source_line(args.root, expected_txt_path, manager_file) is not None:
            processed_uids.add(uid)
            processed_changed = True
            mark_seen(client, uid)
            continue
        typ_msg, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            logging.error("email fetch failed: uid=%s typ=%s", uid, typ_msg)
            continue
        msg = BytesParser(policy=policy.default).parsebytes(msg_data[0][1])
        sender = str(msg.get("From", ""))
        subject = str(msg.get("Subject", ""))
        if not (is_manager_subject(subject) or is_recovery_subject(subject)) or not from_self(sender, args.self_email):
            logging.warning("email candidate rejected after fetch: uid=%s subject=%r from_self=%s", uid, subject, from_self(sender, args.self_email))
            continue
        txt_path = write_mail(args, uid, msg, sender, subject)
        logging.info("email stored: uid=%s path=%s subject=%r", uid, source_ref(args.root, txt_path), subject)
        if is_recovery_subject(subject):
            if recovery_sender_authenticated(msg, args.self_email):
                handle_recovery_email(args, uid, txt_path)
            else:
                append_recovery_record(args.root, txt_path, "recovery email recorded; restart refused because sender authentication did not pass", manager_file)
            processed_uids.add(uid)
            processed_changed = True
            mark_seen(client, uid)
        else:
            pending_line = append_pending(args.root, txt_path, manager_file)
            if push_email_ref(push_args, pending_line):
                processed_uids.add(uid)
                processed_changed = True
                mark_seen(client, uid)
    if processed_changed:
        save_processed_uids(processed_path, processed_uids)


def read_imap_line(client: imaplib.IMAP4_SSL, phase: str) -> bytes:
    line = client.readline()
    if not line:
        raise ConnectionError(f"IMAP connection closed during {phase}")
    return line


def idle_once(client: imaplib.IMAP4_SSL, wait_s: float) -> None:
    tag = "OMOIDLE"
    client.send(f"{tag} IDLE\r\n".encode())
    while True:
        line = read_imap_line(client, "IDLE start")
        if line.decode("utf-8", errors="ignore").startswith("+"):
            break
    readable, _, _ = select.select([client.socket()], [], [], wait_s)
    client.send(b"DONE\r\n")
    while True:
        line = read_imap_line(client, "IDLE done").decode("utf-8", errors="ignore")
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
    safe_args = Args(args.root, args.manager_url, args.mail_dir, args.state_dir, args.manager_file, args.once, config["user"], args.recovery_debounce_s, args.restart_script, args.idle_wait_s, args.manager_target, args.imap_timeout_s)
    logging.info("email watcher starting: root=%s mail_dir=%s state_dir=%s manager_target=%s manager_url=%s idle_wait_s=%s imap_timeout_s=%s", safe_args.root, safe_args.mail_dir, safe_args.state_dir, safe_args.manager_target or "unset", safe_args.manager_url or "unset", safe_args.idle_wait_s, safe_args.imap_timeout_s)
    while True:
        try:
            with imaplib.IMAP4_SSL(config["host"], timeout=safe_args.imap_timeout_s) as client:
                client.login(config["user"], config["password"])
                client.select("INBOX")
                logging.info("email watcher connected and selected INBOX")
                handle_unseen(client, safe_args)
                if safe_args.once:
                    return 0
                while True:
                    idle_once(client, safe_args.idle_wait_s)
                    client.select("INBOX")
                    handle_unseen(client, safe_args)
        except Exception as exc:
            logging.error("email watcher failed: %s", exc)
            if safe_args.once:
                return 1
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
