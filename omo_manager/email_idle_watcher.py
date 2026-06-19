#!/usr/bin/env python3
"""IMAP IDLE watcher for manager emails; stores repo-local `.txt` files and pushes refs."""
from __future__ import annotations

import argparse
import os
import fcntl
import imaplib
import logging
import queue
import re
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email import policy
from email.utils import parsedate_to_datetime
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
MANAGER_REPLY_PREFIX = "Re: [a]"
MANAGER_SUBJECT_TOKEN = "[a]"
MANAGER_SUBJECT_TOKENS = (MANAGER_SUBJECT_TOKEN, "[omo_manager]")
MANAGER_REPLY_SEARCH_PREFIXES = (MANAGER_REPLY_PREFIX, "Re:[a]", "Re: [omo_manager]", "Re:[omo_manager]")
NORMAL_REPLY_SEARCH_PREFIXES = MANAGER_REPLY_SEARCH_PREFIXES
MANAGER_REPLY_SUBJECT_RE = re.compile(r"^re:\s*(?:\[a\]|\[omo_manager\])\s*", re.IGNORECASE)
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
DEFAULT_IDLE_RESPONSE_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_RESPONSE_TIMEOUT_S", "10"))
DEFAULT_IMAP_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IMAP_TIMEOUT_S", str(max(90.0, DEFAULT_IDLE_WAIT_S + 30.0))))
DEFAULT_PULL_INTERVAL_S = float(os.environ.get("OMO_MANAGER_EMAIL_PULL_INTERVAL_S", "600"))
DEFAULT_IDLE_EXIT_AFTER_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_EXIT_AFTER_S", "3600"))
DEFAULT_PROCESSED_RECOVERY_UID_WINDOW = int(os.environ.get("OMO_MANAGER_EMAIL_PROCESSED_RECOVERY_UID_WINDOW", "256"))
DEFAULT_EMAIL_PUSH_READY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_PUSH_READY_TIMEOUT_S", "2"))
DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S", "1"))
DEFAULT_EMAIL_PUSH_PROCESS_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_PUSH_PROCESS_TIMEOUT_S", str(max(30.0, DEFAULT_EMAIL_PUSH_READY_TIMEOUT_S + (2 * DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S) + 15.0))))
DEFAULT_MANAGER_UNREAD_COMPRESSION_THRESHOLD = int(os.environ.get("OMO_MANAGER_EMAIL_UNREAD_COMPRESSION_THRESHOLD", "16"))
DEFAULT_MANAGER_RECENT_CLEANUP_THRESHOLD = int(os.environ.get("OMO_MANAGER_EMAIL_RECENT_CLEANUP_THRESHOLD", "64"))
DEFAULT_MANAGER_RECENT_CLEANUP_WINDOW_S = float(os.environ.get("OMO_MANAGER_EMAIL_RECENT_CLEANUP_WINDOW_S", str(24 * 60 * 60)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class EmailPush:
    line_no: int
    command: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class ManagerMailCounts:
    total: int
    unread: int
    recent_window_s: float
    recent_total: int
    recent_exact: bool


_email_push_queue: queue.Queue[EmailPush] = queue.Queue()
_email_push_worker_started = False


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
    pull_interval_s: float = DEFAULT_PULL_INTERVAL_S
    idle_exit_after_s: float = DEFAULT_IDLE_EXIT_AFTER_S
    unread_compression_threshold: int = DEFAULT_MANAGER_UNREAD_COMPRESSION_THRESHOLD
    recent_cleanup_threshold: int = DEFAULT_MANAGER_RECENT_CLEANUP_THRESHOLD
    recent_cleanup_window_s: float = DEFAULT_MANAGER_RECENT_CLEANUP_WINDOW_S


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
    pull_interval_s: float = DEFAULT_PULL_INTERVAL_S
    idle_exit_after_s: float = DEFAULT_IDLE_EXIT_AFTER_S
    unread_compression_threshold: int = DEFAULT_MANAGER_UNREAD_COMPRESSION_THRESHOLD
    recent_cleanup_threshold: int = DEFAULT_MANAGER_RECENT_CLEANUP_THRESHOLD
    recent_cleanup_window_s: float = DEFAULT_MANAGER_RECENT_CLEANUP_WINDOW_S


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
    parser.add_argument("--pull-interval-s", type=float, default=DEFAULT_PULL_INTERVAL_S, help="Unread mailbox scan interval while IDLE is otherwise quiet")
    parser.add_argument("--idle-exit-after-s", type=float, default=DEFAULT_IDLE_EXIT_AFTER_S, help="Exit after this many quiet seconds so the outer supervisor refreshes the process; set <=0 to disable")
    parser.add_argument("--unread-compression-threshold", type=int, default=DEFAULT_MANAGER_UNREAD_COMPRESSION_THRESHOLD, help="Queue manager-sent unread mail compression when unread manager mail exceeds this count; set <=0 to disable")
    parser.add_argument("--recent-cleanup-threshold", type=int, default=DEFAULT_MANAGER_RECENT_CLEANUP_THRESHOLD, help="Queue manager-human cleanup when recent manager mail exceeds this count; set <=0 to disable")
    parser.add_argument("--recent-cleanup-window-s", type=float, default=DEFAULT_MANAGER_RECENT_CLEANUP_WINDOW_S, help="Recent manager-human cleanup threshold window")
    parser.add_argument("--once", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    root = parsed.root
    manager_file = parsed.manager_file
    if manager_file is not None and not manager_file.is_absolute():
        manager_file = root / manager_file
    return Args(root, parsed.manager_url.rstrip("/"), parsed.mail_dir, parsed.state_dir, manager_file, parsed.once, "", parsed.recovery_debounce_s, parsed.restart_script, parsed.idle_wait_s, parsed.manager_target.strip(), parsed.imap_timeout_s, parsed.pull_interval_s, parsed.idle_exit_after_s, parsed.unread_compression_threshold, parsed.recent_cleanup_threshold, parsed.recent_cleanup_window_s)


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
    return MANAGER_REPLY_SUBJECT_RE.match(subject) is not None


def normalize_human_subject(subject: str) -> str:
    return MANAGER_REPLY_SUBJECT_RE.sub("Re: ", subject, count=1)


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


def unaccepted_pending_uids_path(args: Args) -> Path:
    return args.state_dir / "email-unaccepted-pending-uids.tsv"


def manager_mail_counts_path(args: Args) -> Path:
    return args.state_dir / "email-manager-mail-counts.tsv"


def manager_mail_threshold_state_path(args: Args) -> Path:
    return args.state_dir / "email-manager-mail-thresholds.tsv"


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


def write_private_state(path: Path, body: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)


def save_manager_mail_counts(path: Path, counts: ManagerMailCounts) -> None:
    body = (
        f"updated_at_s\t{int(time.time())}\n"
        f"manager_total\t{counts.total}\n"
        f"manager_unread\t{counts.unread}\n"
        f"manager_human_recent_window_s\t{int(counts.recent_window_s)}\n"
        f"manager_human_recent_total\t{counts.recent_total}\n"
        f"manager_human_recent_exact\t{int(counts.recent_exact)}\n"
    )
    write_private_state(path, body)


def load_active_manager_mail_thresholds(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    active: set[str] = set()
    for line in lines:
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1] == "1":
            active.add(parts[0])
    return active


def save_active_manager_mail_thresholds(path: Path, active: set[str]) -> None:
    body = "".join(f"{kind}\t1\n" for kind in sorted(active))
    write_private_state(path, body)


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


def existing_source_line_in_root(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    source_line = existing_source_line(root, txt_path, manager_file)
    if source_line is not None:
        return source_line
    for path in sorted(root.glob("work_manager_*.md")):
        if manager_file is not None and path == manager_file:
            continue
        source_line = existing_source_line(root, txt_path, path)
        if source_line is not None:
            return source_line
    return None


def existing_source_pending_line(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    manager_file = manager_file or dated_manager_file(root)
    source_line = existing_source_line(root, txt_path, manager_file)
    if source_line is None or source_line <= 1:
        return None
    lines = manager_file.read_text(encoding="utf-8").splitlines()
    for pending_idx in range(source_line - 2, -1, -1):
        stripped = lines[pending_idx].strip()
        if stripped == "(pending)":
            return pending_idx + 1
        if stripped.startswith(("(", "#")) and stripped != "(pending)":
            break
    return None


def existing_source_pending_line_in_root(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    pending_line = existing_source_pending_line(root, txt_path, manager_file)
    if pending_line is not None:
        return pending_line
    for path in sorted(root.glob("work_manager_*.md")):
        if manager_file is not None and path == manager_file:
            continue
        pending_line = existing_source_pending_line(root, txt_path, path)
        if pending_line is not None:
            return pending_line
    return None


def existing_source_pending_path_line_in_root(root: Path, txt_path: Path, manager_file: Path | None = None) -> tuple[Path, int] | None:
    manager_file = manager_file or dated_manager_file(root)
    pending_line = existing_source_pending_line(root, txt_path, manager_file)
    if pending_line is not None:
        return manager_file, pending_line
    for path in sorted(root.glob("work_manager_*.md")):
        if path == manager_file:
            continue
        pending_line = existing_source_pending_line(root, txt_path, path)
        if pending_line is not None:
            return path, pending_line
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


def run_email_push(push: EmailPush) -> bool:
    try:
        result = subprocess.run(push.command, check=False, env=push.env, timeout=DEFAULT_EMAIL_PUSH_PROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        logging.error("email pending async push timed out: line=%s timeout_s=%s command=%s", push.line_no, exc.timeout, shell_join(push.command))
        return False
    except OSError as exc:
        logging.error("email pending async push failed: line=%s error=%s", push.line_no, exc)
        return False
    if result.returncode != 0:
        logging.error("email pending async push failed: line=%s status=%s", push.line_no, result.returncode)
        return False
    return True


def email_push_worker() -> None:
    while True:
        push = _email_push_queue.get()
        try:
            run_email_push(push)
        except Exception as exc:
            logging.exception("email pending async push worker failed: %s", exc)
        finally:
            _email_push_queue.task_done()


def start_email_push_worker() -> None:
    global _email_push_worker_started
    if _email_push_worker_started:
        return
    thread = threading.Thread(target=email_push_worker, name="omo-email-push", daemon=True)
    thread.start()
    _email_push_worker_started = True


def wait_email_pushes() -> None:
    _email_push_queue.join()


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
    env = os.environ.copy()
    env["OMO_MANAGER_TMUX_READY_TIMEOUT_S"] = str(DEFAULT_EMAIL_PUSH_READY_TIMEOUT_S)
    env["OMO_MANAGER_TMUX_SUBMIT_VERIFY_TIMEOUT_S"] = str(DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S)
    return run_email_push(EmailPush(line_no, command, env))


def push_manager_mail_threshold_ref(args: Args, line_no: int, kind: str) -> bool:
    if not args.manager_url and not args.manager_target:
        logging.error("manager mail threshold push failed: kind=%s manager URL or target is required", kind)
        return False
    manager_file = current_manager_file(args)
    ref = manager_file.relative_to(args.root) if manager_file.is_relative_to(args.root) else manager_file
    command = ["omo_push_to_manager.py", f"pending: file={ref} line={line_no} origin=agent source=email-watcher action=no-human-ack kind={kind}", "--root", str(args.root), "--submit"]
    command.extend(["--pending-file", str(ref), "--pending-line", str(line_no)])
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
    if args.manager_url:
        command.extend(["--manager-url", args.manager_url])
    env = os.environ.copy()
    env["OMO_MANAGER_TMUX_READY_TIMEOUT_S"] = str(DEFAULT_EMAIL_PUSH_READY_TIMEOUT_S)
    env["OMO_MANAGER_TMUX_SUBMIT_VERIFY_TIMEOUT_S"] = str(DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S)
    try:
        start_email_push_worker()
    except RuntimeError as exc:
        logging.error("manager mail threshold async push worker start failed: kind=%s line=%s error=%s", kind, line_no, exc)
        return False
    _email_push_queue.put(EmailPush(line_no, command, env))
    logging.info("manager mail threshold async push queued: kind=%s line=%s", kind, line_no)
    return True


def threshold_marker(kind: str) -> str:
    return f"(from manager-email-threshold {kind})"


def existing_current_threshold_pending_line(manager_file: Path, kind: str) -> int | None:
    if not manager_file.exists():
        return None
    lines = manager_file.read_text(encoding="utf-8").splitlines()
    marker = threshold_marker(kind)
    for idx, line in enumerate(lines):
        if line.strip() != marker:
            continue
        for pending_idx in range(max(0, idx - 6), idx):
            if lines[pending_idx].strip() == "(pending)":
                return pending_idx + 1
        return idx + 1
    return None


def append_manager_mail_threshold_pending(args: Args, kind: str, counts: ManagerMailCounts, dedupe_current: bool = False) -> int:
    manager_file = current_manager_file(args)
    if dedupe_current:
        existing_line = existing_current_threshold_pending_line(manager_file, kind)
        if existing_line is not None:
            return existing_line
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    if kind == "unread-compression":
        summary = f"manager email watcher threshold: unread manager mail {counts.unread} exceeds {args.unread_compression_threshold}"
        route = "route a worker through `~/.config/omo_manager/docs/manager-mail-compression.md`"
        retention = "compress only unread manager-sent mail, send replacement summaries first, then mark only the explicit superseded UID set seen"
    elif kind == "recent-cleanup":
        hours = args.recent_cleanup_window_s / 3600
        summary = f"manager email watcher threshold: manager-human mail within last {hours:g}h is {counts.recent_total}, exceeding {args.recent_cleanup_threshold}"
        route = "route a worker through `~/.config/omo_manager/docs/manager-mail-cleanup.md` and the compression workflow if replacement summaries are needed"
        retention = "threshold is trigger-only; rerun cleanup classification and retain recent, unread, active, human-pending, long-report, and uncertain threads"
    else:
        raise ValueError(f"unknown manager mail threshold kind: {kind}")
    block = [
        "",
        "(pending)",
        f"[omo-message-source: origin=agent agent=email_idle_watcher via=manager-mail-threshold status=trigger kind={kind}]",
        threshold_marker(kind),
        summary,
        f"- counts: manager_total={counts.total} manager_unread={counts.unread} manager_human_recent={counts.recent_total} recent_window_s={int(counts.recent_window_s)}",
        f"- action: {route}",
        f"- retention: {retention}",
    ]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


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
    body = f"Subject: {normalize_human_subject(subject)}\n\n{message_text(msg)}"
    fd = os.open(txt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return txt_path


def write_private_temp(text: str, suffix: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="omo-recovery-", suffix=suffix, text=True)
    path = Path(raw_path)
    try:
        path.chmod(0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def email_human(args: Args, subject: str, body: str) -> None:
    subject_path = write_private_temp(subject.rstrip("\n") + "\n", ".txt")
    body_path = write_private_temp(body, ".md")
    try:
        result = subprocess.run([str(Path.home() / ".config/omo_manager/omo_email_human.sh"), "--subject-file", str(subject_path), "--message-file", str(body_path)], text=True, check=False)
        if result.returncode != 0:
            logging.error("recovery human email failed: status=%s", result.returncode)
    finally:
        subject_path.unlink(missing_ok=True)
        body_path.unlink(missing_ok=True)


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
            email_human(args, "[a] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart was refused because manager-url is not loopback: {args.manager_url}\n\nRun only after correcting configuration:\n\n```sh\n{shell_join(command)}\n```\n")
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
                email_human(args, "[a] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart helper launch failed.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
                return
        record_recovery_attempt(last_path, now_s, uid, f"returncode={result.returncode}")
        if result.returncode != 0:
            append_recovery_record(args.root, txt_path, f"recovery restart failed with exit {result.returncode}; see `{log_path}`", args.manager_file)
            email_human(args, "[a] Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart failed with exit {result.returncode}.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def uid_search_range(processed_uids: set[str]) -> str:
    max_uid = max((int(uid) for uid in processed_uids if uid.isdigit()), default=0)
    return f"{max_uid + 1}:*"


def search_uids(client: imaplib.IMAP4_SSL, subject: str, self_email: str, processed_uids: set[str]) -> set[bytes]:
    candidate_uids: set[bytes] = set()
    typ, data = client.uid("search", "UNSEEN", "FROM", f'"{self_email}"', "SUBJECT", f'"{subject}"')
    if typ == "OK" and data and data[0]:
        candidate_uids.update(data[0].split())
    if processed_uids:
        typ, data = client.uid("search", None, "UID", uid_search_range(processed_uids), "FROM", f'"{self_email}"', "SUBJECT", f'"{subject}"')
        if typ == "OK" and data and data[0]:
            candidate_uids.update(data[0].split())
    return candidate_uids


def chunks(values: list[str], n_values: int) -> list[list[str]]:
    return [values[idx : idx + n_values] for idx in range(0, len(values), n_values)]


def search_processed_uids(client: imaplib.IMAP4_SSL, subject: str, self_email: str, uids: list[str]) -> set[bytes]:
    candidate_uids: set[bytes] = set()
    for uid_chunk in chunks(uids, 50):
        typ, data = client.uid("search", None, "UID", ",".join(uid_chunk), "FROM", f'"{self_email}"', "SUBJECT", f'"{subject}"')
        if typ == "OK" and data and data[0]:
            candidate_uids.update(data[0].split())
    return candidate_uids


def decode_search_uids(data: list[object]) -> list[str]:
    if not data or not data[0]:
        return []
    raw = data[0]
    if isinstance(raw, bytes):
        return [uid.decode() for uid in raw.split()]
    if isinstance(raw, str):
        return raw.split()
    return []


def search_manager_mail_uids(client: imaplib.IMAP4_SSL, self_email: str, unread: bool = False, since: datetime | None = None) -> list[str]:
    criteria: list[str] = []
    if unread:
        criteria.append("UNSEEN")
    if since is not None:
        criteria.extend(["SINCE", since.strftime("%d-%b-%Y")])
    found: list[str] = []
    seen: set[str] = set()
    for token in MANAGER_SUBJECT_TOKENS:
        typ, data = client.uid("search", *(criteria + ["FROM", f'"{self_email}"', "SUBJECT", f'"{token}"']))
        if typ != "OK":
            raise RuntimeError(f"IMAP manager mail search failed: {typ}")
        for uid in decode_search_uids(data):
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def parsed_message_date(msg: Message) -> datetime | None:
    raw_date = str(msg.get("Date", ""))
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone()


def recent_manager_mail_uids(client: imaplib.IMAP4_SSL, self_email: str, candidate_uids: list[str], cutoff: datetime) -> list[str]:
    recent: list[str] = []
    for uid in candidate_uids:
        typ, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"IMAP manager mail header fetch failed: uid={uid} typ={typ}")
        msg = BytesParser(policy=policy.default).parsebytes(data[0][1])
        sender = str(msg.get("From", ""))
        subject = str(msg.get("Subject", ""))
        dt = parsed_message_date(msg)
        if dt is not None and dt >= cutoff and from_self(sender, self_email) and any(token.lower() in subject.lower() for token in MANAGER_SUBJECT_TOKENS):
            recent.append(uid)
    return recent


def manager_mail_counts(client: imaplib.IMAP4_SSL, self_email: str, recent_window_s: float, recent_threshold: int, now: datetime | None = None) -> ManagerMailCounts:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(seconds=recent_window_s)
    total_uids = search_manager_mail_uids(client, self_email)
    unread_uids = search_manager_mail_uids(client, self_email, unread=True)
    recent_candidates = search_manager_mail_uids(client, self_email, since=cutoff)
    recent_total = len(recent_manager_mail_uids(client, self_email, recent_candidates, cutoff))
    return ManagerMailCounts(len(total_uids), len(unread_uids), recent_window_s, recent_total, True)


def handle_manager_mail_thresholds(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    counts = manager_mail_counts(client, args.self_email, args.recent_cleanup_window_s, args.recent_cleanup_threshold)
    save_manager_mail_counts(manager_mail_counts_path(args), counts)
    logging.info("manager mail counts: total=%s unread=%s recent_window_s=%s recent_total=%s recent_exact=%s", counts.total, counts.unread, int(counts.recent_window_s), counts.recent_total, counts.recent_exact)
    threshold_state_path = manager_mail_threshold_state_path(args)
    state_missing = not threshold_state_path.exists()
    active = load_active_manager_mail_thresholds(threshold_state_path)
    next_active = set(active)
    triggered = False
    state_changed = False
    checks = (
        ("unread-compression", args.unread_compression_threshold, counts.unread),
        ("recent-cleanup", args.recent_cleanup_threshold, counts.recent_total),
    )
    for kind, threshold, count in checks:
        exceeded = threshold > 0 and count > threshold
        if exceeded and kind not in active:
            line_no = append_manager_mail_threshold_pending(args, kind, counts, dedupe_current=state_missing)
            next_active.add(kind)
            save_active_manager_mail_thresholds(threshold_state_path, next_active)
            state_missing = False
            active = set(next_active)
            push_manager_mail_threshold_ref(args, line_no, kind)
            triggered = True
        elif not exceeded and kind in next_active:
            next_active.remove(kind)
            state_changed = True
    if state_changed:
        save_active_manager_mail_thresholds(threshold_state_path, next_active)
    return triggered


def maybe_handle_manager_mail_thresholds(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    try:
        return handle_manager_mail_thresholds(client, args)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        logging.warning("manager mail threshold check failed: %s", exc)
        return False


def recoverable_processed_uids(processed_uids: set[str], root: Path, mail_dir: Path, manager_file: Path, uid_window: int = DEFAULT_PROCESSED_RECOVERY_UID_WINDOW) -> list[str]:
    numeric_uids = [int(uid) for uid in processed_uids if uid.isdigit()]
    if uid_window <= 0 or not numeric_uids:
        return []
    min_uid = max(numeric_uids) - uid_window + 1
    return [
        uid
        for uid in sorted(processed_uids, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))
        if uid.isdigit() and int(uid) >= min_uid and existing_source_line_in_root(root, mail_dir / f"{uid}.txt", manager_file) is None
    ]


def mark_seen(client: imaplib.IMAP4_SSL, uid: str) -> bool:
    try:
        typ, _data = client.uid("store", uid, "+FLAGS", r"(\Seen)")
    except imaplib.IMAP4.error as exc:
        logging.error("email mark read failed: uid=%s error=%s", uid, exc)
        return False
    if typ != "OK":
        logging.error("email mark read failed: uid=%s typ=%s", uid, typ)
        return False
    return True


def handle_unseen(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    processed_path = processed_uids_path(args)
    processed_uids = load_processed_uids(processed_path)
    unaccepted_path = unaccepted_pending_uids_path(args)
    unaccepted_pending_uids = load_processed_uids(unaccepted_path)
    processed_changed = False
    unaccepted_changed = False
    handled = False
    manager_file = current_manager_file(args)
    push_args = args_w_manager_file(args, manager_file)
    candidate_uids: set[bytes] = set()
    for subject_prefix in NORMAL_REPLY_SEARCH_PREFIXES:
        candidate_uids.update(search_uids(client, subject_prefix, args.self_email, processed_uids))
    processed_missing_source = set(recoverable_processed_uids(processed_uids, args.root, args.mail_dir, manager_file))
    if processed_missing_source:
        for subject_prefix in NORMAL_REPLY_SEARCH_PREFIXES:
            candidate_uids.update(search_processed_uids(client, subject_prefix, args.self_email, sorted(processed_missing_source, key=lambda value: int(value))))
    if unaccepted_pending_uids:
        for subject_prefix in NORMAL_REPLY_SEARCH_PREFIXES:
            candidate_uids.update(search_processed_uids(client, subject_prefix, args.self_email, sorted(unaccepted_pending_uids, key=lambda value: int(value))))
    for subject in RECOVERY_SUBJECTS:
        candidate_uids.update(search_uids(client, subject, args.self_email, processed_uids))
    if not candidate_uids:
        logging.info("email scan complete: n=0 processed_next=%s manager_file=%s", uid_search_range(processed_uids), manager_file)
        return maybe_handle_manager_mail_thresholds(client, args)
    logging.info("email candidates found: n=%s uids=%s processed_max=%s manager_file=%s", len(candidate_uids), ",".join(uid.decode() for uid in sorted(candidate_uids, key=lambda value: int(value))), uid_search_range(processed_uids), manager_file)
    args.mail_dir.mkdir(parents=True, exist_ok=True)
    for raw_uid in sorted(candidate_uids, key=lambda value: int(value)):
        uid = raw_uid.decode()
        expected_txt_path = args.mail_dir / f"{uid}.txt"
        pending_ref = existing_source_pending_path_line_in_root(args.root, expected_txt_path, manager_file) if uid in unaccepted_pending_uids else None
        if pending_ref is not None:
            pending_file, pending_line = pending_ref
            if push_email_ref(args_w_manager_file(args, pending_file), pending_line):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen(client, uid)
                handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
            continue
        if uid in processed_uids:
            existing_source_line = existing_source_line_in_root(args.root, expected_txt_path, manager_file)
            if uid not in unaccepted_pending_uids and existing_source_line is not None:
                handled = mark_seen(client, uid) or handled
                continue
            if uid in unaccepted_pending_uids and existing_source_line is not None:
                logging.warning("email unaccepted processed uid has source without pending; reprocessing: uid=%s root=%s", uid, args.root)
            elif uid in unaccepted_pending_uids:
                logging.warning("email unaccepted processed uid lacks source; reprocessing: uid=%s root=%s", uid, args.root)
            elif uid not in processed_missing_source:
                logging.warning("email processed uid lacks source in current root and is outside recovery window; skipping: uid=%s root=%s", uid, args.root)
                continue
            else:
                logging.warning("email processed uid lacks source in current root; reprocessing: uid=%s root=%s", uid, args.root)
        existing_pending_line = existing_source_pending_line(args.root, expected_txt_path, manager_file)
        if existing_pending_line is not None:
            if push_email_ref(push_args, existing_pending_line):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen(client, uid)
                handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
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
            handled = True
        else:
            pending_line = append_pending(args.root, txt_path, manager_file)
            if push_email_ref(push_args, pending_line):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen(client, uid)
                handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
    if processed_changed:
        save_processed_uids(processed_path, processed_uids)
    if unaccepted_changed:
        save_processed_uids(unaccepted_path, unaccepted_pending_uids)
    threshold_handled = maybe_handle_manager_mail_thresholds(client, args)
    return handled or processed_changed or threshold_handled


def read_imap_line(client: imaplib.IMAP4_SSL, phase: str, deadline_s: float | None = None) -> bytes:
    sock = client.socket() if deadline_s is not None else None
    old_timeout_s = None
    if deadline_s is not None:
        wait_s = deadline_s - time.monotonic()
        if wait_s <= 0:
            raise TimeoutError(f"IMAP timed out during {phase}")
        old_timeout_s = sock.gettimeout()
        sock.settimeout(wait_s)
    try:
        line = client.readline()
    except TimeoutError as exc:
        raise TimeoutError(f"IMAP timed out during {phase}") from exc
    finally:
        if sock is not None:
            sock.settimeout(old_timeout_s)
    if not line:
        raise ConnectionError(f"IMAP connection closed during {phase}")
    return line


def idle_once(client: imaplib.IMAP4_SSL, wait_s: float) -> bool:
    tag = "OMOIDLE"
    client.send(f"{tag} IDLE\r\n".encode())
    deadline_s = time.monotonic() + DEFAULT_IDLE_RESPONSE_TIMEOUT_S
    while True:
        line = read_imap_line(client, "IDLE start", deadline_s)
        if line.decode("utf-8", errors="ignore").startswith("+"):
            break
    readable, _, _ = select.select([client.socket()], [], [], wait_s)
    event_seen = bool(readable)
    client.send(b"DONE\r\n")
    deadline_s = time.monotonic() + DEFAULT_IDLE_RESPONSE_TIMEOUT_S
    while True:
        line = read_imap_line(client, "IDLE done", deadline_s).decode("utf-8", errors="ignore")
        if tag in line:
            break
        event_seen = True
    return event_seen


def watch_inbox(client: imaplib.IMAP4_SSL, args: Args) -> int:
    activity = handle_unseen(client, args)
    now_s = time.monotonic()
    last_activity_s = now_s
    next_pull_s = now_s + args.pull_interval_s if args.pull_interval_s > 0 else float("inf")
    if activity:
        logging.info("email watcher startup scan found candidate mail")
    while True:
        now_s = time.monotonic()
        if args.idle_exit_after_s > 0 and now_s - last_activity_s >= args.idle_exit_after_s:
            logging.info("email watcher exiting after idle refresh window: idle_s=%.1f", now_s - last_activity_s)
            return 0
        if now_s >= next_pull_s:
            client.select("INBOX")
            if handle_unseen(client, args):
                last_activity_s = time.monotonic()
            next_pull_s = time.monotonic() + args.pull_interval_s
            continue
        wait_s = args.idle_wait_s
        if args.pull_interval_s > 0:
            wait_s = min(wait_s, max(0.0, next_pull_s - now_s))
        if idle_once(client, wait_s):
            client.select("INBOX")
            if handle_unseen(client, args):
                last_activity_s = time.monotonic()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    config = parse_env_config(CONFIG_PATH)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        print(f"email_idle_watcher: missing config keys {sorted(missing)} in {CONFIG_PATH}", file=sys.stderr)
        return 1
    safe_args = Args(args.root, args.manager_url, args.mail_dir, args.state_dir, args.manager_file, args.once, config["user"], args.recovery_debounce_s, args.restart_script, args.idle_wait_s, args.manager_target, args.imap_timeout_s, args.pull_interval_s, args.idle_exit_after_s, args.unread_compression_threshold, args.recent_cleanup_threshold, args.recent_cleanup_window_s)
    logging.info("email watcher starting: root=%s mail_dir=%s state_dir=%s manager_target=%s manager_url=%s idle_wait_s=%s imap_timeout_s=%s pull_interval_s=%s idle_exit_after_s=%s unread_compression_threshold=%s recent_cleanup_threshold=%s recent_cleanup_window_s=%s", safe_args.root, safe_args.mail_dir, safe_args.state_dir, safe_args.manager_target or "unset", safe_args.manager_url or "unset", safe_args.idle_wait_s, safe_args.imap_timeout_s, safe_args.pull_interval_s, safe_args.idle_exit_after_s, safe_args.unread_compression_threshold, safe_args.recent_cleanup_threshold, int(safe_args.recent_cleanup_window_s))
    while True:
        try:
            with imaplib.IMAP4_SSL(config["host"], timeout=safe_args.imap_timeout_s) as client:
                client.login(config["user"], config["password"])
                client.select("INBOX")
                logging.info("email watcher connected and selected INBOX")
                if safe_args.once:
                    handle_unseen(client, safe_args)
                    wait_email_pushes()
                    return 0
                result = watch_inbox(client, safe_args)
                wait_email_pushes()
                return result
        except Exception as exc:
            logging.error("email watcher failed: %s", exc)
            if safe_args.once:
                wait_email_pushes()
                return 1
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
