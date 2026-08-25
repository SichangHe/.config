#!/usr/bin/env python3
"""IMAP IDLE watcher for manager emails; stores repo-local `.txt` files and pushes refs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import imaplib
import logging
import os
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
from email.utils import getaddresses, parsedate_to_datetime
from email.utils import parseaddr
from email.message import Message
from email.parser import BytesParser
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

try:
    from .omo_email_config import AgentMailSettings, GMAIL_IMAP_HOST, configured_agent_mail, human_config_path
    from .omo_email_subject import subject_base
    from .omo_agent_status import TaskFrontmatterError, parse_task_metadata
    from .omo_task_lock import task_file_lock
    from .omo_tmux_send import CodexSendOptions, DEFAULT_TMUX_ENTER_COUNT, require_sendable_codex_target, send_system_to_codex as send_to_codex
except ImportError:
    try:
        from omo_email_config import AgentMailSettings, GMAIL_IMAP_HOST, configured_agent_mail, human_config_path
        from omo_email_subject import subject_base
        from omo_agent_status import TaskFrontmatterError, parse_task_metadata
        from omo_task_lock import task_file_lock
        from omo_tmux_send import CodexSendOptions, DEFAULT_TMUX_ENTER_COUNT, require_sendable_codex_target, send_system_to_codex as send_to_codex
    except ImportError:
        subject_base = None
        TaskFrontmatterError = ValueError

        def parse_task_metadata(_text: str, _work_log_root: Path | None = None) -> object:
            return None

        try:
            from .omo_task_lock import task_file_lock
        except ImportError:
            from omo_task_lock import task_file_lock

def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


def dated_manager_file(root: Path) -> Path:
    return root / f"work_manager_{datetime.now().astimezone().strftime('%Y-%m-%d')}.md"


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "http://127.0.0.1:18790")
DEFAULT_MANAGER_TARGET = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
DEFAULT_MAIL_DIR = Path(os.environ.get("OMO_MANAGER_MAIL_DIR", DEFAULT_ROOT / "manager_mail"))
CONFIG_PATH = human_config_path()
LEGACY_MANAGER_SUBJECT_TOKENS = ("[a]", "[omo_manager]")
PB_CLEANUP_EXCLUDED_SUBJECT_PREFIXES = ("PB news", "PB stock watch", "PB urgent")
PB_CLEANUP_EXCLUDED_SUBJECT_RE = re.compile(r"^(?:PB news|PB stock watch|PB urgent)\b", re.IGNORECASE)
MANAGER_REPLY_SUBJECT_RE = re.compile(r"^re:\s*(?:\[a\]|\[omo_manager\])\s*", re.IGNORECASE)
MANAGER_TARGET_SUBJECT_RE = re.compile(r"^(?:re:\s*)*(?:(?:\[a\]|\[omo_manager\])\s+)?(?:\[([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\]|([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?))(?:\s+|$)", re.IGNORECASE)
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
PWD_FOOTER_RE = re.compile(r"(?:^|\n)PWD: [^\n]+\n?\Z")
TMUX_FOOTER_RE = re.compile(r"(?:^|\n)tmux: [^\r\n]+\r?\n?\Z", re.IGNORECASE)
RECOVERY_SUBJECTS = {"[omo_manager_recover]", "Re: [omo_manager_recover]"}
ROUTED_PREFIXES = ("(manager handled:",)
IGNORE_PARTS = {".git", ".venv", "__pycache__", "manager_mail"}
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
GMAIL_METADATA_FETCH = "(X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE)"
GMAIL_MSGID_RE = re.compile(rb"X-GM-MSGID (\d+)")
GMAIL_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")
GMAIL_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
GMAIL_SENT_LABEL_RE = re.compile(rb"X-GM-LABELS \([^)]*\\Sent(?:\s|\))")
AMH_LIVE_MAILBOX_APPROVAL_RECEIPT_SCHEMA = "amh-live-mailbox-authenticated-approval-receipt/v1"
AMH_LIVE_MAILBOX_SENDER = "sichangheagent@gmail.com"
AMH_LIVE_MAILBOX_RECIPIENT = "stevensichanghe@gmail.com"
AMH_LIVE_MAILBOX_EMAIL1_SUBJECT = "Gmail threading test"
AMH_LIVE_MAILBOX_EMAIL2_SUBJECT = f"Re: {AMH_LIVE_MAILBOX_EMAIL1_SUBJECT}"
AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_SUBJECT = "Approve Email 1 only: Gmail threading test"
AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT = f"Re: {AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_SUBJECT}"
AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_REQUEST_SUBJECT = "Approve Email 2 Gmail threading reply test"
AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_SUBJECT = f"Re: {AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_REQUEST_SUBJECT}"
AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID = "<gmail-threading-approval-email1@test.local>"
AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_REQUEST_MESSAGE_ID = "<gmail-threading-approval-email2@test.local>"
AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_DEADLINE_UNIX_MS = 1787412600000
AMH_LIVE_MAILBOX_EMAIL1_MESSAGE_ID = "<gmail-threading-test-1@test.local>"
AMH_LIVE_MAILBOX_EMAIL2_MESSAGE_ID = "<gmail-threading-test-2@test.local>"
AMH_LIVE_MAILBOX_EMAIL1_BODY_SHA256 = "c0918fab9b4aa68b38d9db195820b1523fa2d2127e1b0303c3116567b0d5f6c2"
AMH_LIVE_MAILBOX_EMAIL2_BODY_SHA256 = "56f6fc624cb982a2e0bcaa926fee777a396983af7428716b484193affdd5c217"
AMH_LIVE_MAILBOX_EMAIL1_ARTIFACT_SHA256 = "3fb59bde88d467177e17165d22a2506dca2975d9547024bf6f3829b50f1736f2"
AMH_LIVE_MAILBOX_EMAIL2_ARTIFACT_SHA256 = "4ccfeea60c6634b2882a23a34ca8ff1cb2fb880fa1be3dc58f61f9816aa72d9d"
AMH_LIVE_MAILBOX_PACKET_ROOT = Path("/shagent/amh_work_logs/amh-live-mailbox-parity-20260821")
AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR = Path("/ssd1/sichangheagent/work_logs/202607/manager_mail")
AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR = default_state_dir()
AMH_LIVE_MAILBOX_PROVIDER_AUTHENTICATION = "trusted-gmail-auth-results"
TRUST_LIVE_MAILBOX_AUTH_RESULTS = os.environ.get("OMO_MANAGER_TRUST_LIVE_MAILBOX_AUTH_RESULTS", "").lower() in {"1", "true", "yes"}
AMH_LIVE_MAILBOX_WORKER_CALLER = "agent:mailbox-parity-worker"
AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT = "Approved GT-20260821-EMAIL1."
AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_RE = re.compile(
    r"Approved: send Email 2 only for Gmail threading test "
    r"from sichangheagent@gmail\.com to stevensichanghe@gmail\.com using "
    r"Email 1 Message-ID: (<[^>\r\n]+>)\. Approval code GT-20260821-EMAIL2\."
)
AMH_LIVE_MAILBOX_RECEIPT_TTL_MS = 30 * 60 * 1000
AMH_SUBJECT_TAG_RE = re.compile(r"^\s*(?:re:\s*)*\[([A-Za-z0-9._-]{1,128})\](?:\s+|$)", re.IGNORECASE)
RESERVED_AMH_SUBJECT_TAGS = frozenset({"a", "omo", "omo_manager"})
MAIN_MANAGER_AGENT_ID = "main-manager"
MAIN_MANAGER_SUBJECT_TAG = "main"
AMH_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
DEFAULT_AMH_WORKDIR = Path(os.environ.get("OMO_AMH_WORKDIR", "/ssd1/sichangheagent/amh"))
DEFAULT_AMH_BRIDGE_ROOT = Path(os.environ.get("OMO_AMH_BRIDGE_ROOT", "/ssd1/sichangheagent/agent_managers"))
DEFAULT_AMH_EXECUTABLE = Path(os.environ.get("OMO_AMH_EXECUTABLE", "/ssd1/sichangheagent/agent_managers/target/debug/amh"))
DEFAULT_AMH_RUNTIME_ROOT = Path(
    os.environ.get(
        "OMO_AMH_RUNTIME_ROOT",
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager" / "amh-runtime",
    )
)
DEFAULT_RECOVERY_DEBOUNCE_S = int(os.environ.get("OMO_MANAGER_RECOVERY_DEBOUNCE_S", "900"))
DEFAULT_IDLE_WAIT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_WAIT_S", "60"))
DEFAULT_IDLE_RESPONSE_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_RESPONSE_TIMEOUT_S", "10"))
DEFAULT_IMAP_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IMAP_TIMEOUT_S", str(max(90.0, DEFAULT_IDLE_WAIT_S + 30.0))))
DEFAULT_PULL_INTERVAL_S = float(os.environ.get("OMO_MANAGER_EMAIL_PULL_INTERVAL_S", "600"))
DEFAULT_IDLE_EXIT_AFTER_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_EXIT_AFTER_S", "3600"))
DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S", "1"))
DEFAULT_MANAGER_UNREAD_COMPRESSION_THRESHOLD = int(os.environ.get("OMO_MANAGER_EMAIL_UNREAD_COMPRESSION_THRESHOLD", "16"))
DEFAULT_MANAGER_RECENT_CLEANUP_THRESHOLD = int(os.environ.get("OMO_MANAGER_EMAIL_RECENT_CLEANUP_THRESHOLD", "64"))
DEFAULT_MANAGER_RECENT_CLEANUP_WINDOW_S = float(os.environ.get("OMO_MANAGER_EMAIL_RECENT_CLEANUP_WINDOW_S", str(24 * 60 * 60)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class EmailPush:
    line_no: int
    target: str
    text: str
    root: Path
    pending_file: Path
    threshold_kind: str = ""
    state_dir: Path | None = None


@dataclass(frozen=True)
class ManagerMailCounts:
    total: int
    unread: int
    recent_window_s: float
    recent_total: int
    recent_exact: bool


@dataclass(frozen=True)
class EmailRoute:
    manager_file: Path
    manager_target: str
    pending_watcher_delivery: bool = False


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
    mail_thresholds: bool = False
    inbox_identity: str = ""
    manager_mail_recipient: str = ""
    manager_mail_subject_tags: bool = True
    live_mailbox_approval_only: bool = False
    live_mailbox_stage: str = "email1"


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
    live_mailbox_approval_only: bool = False
    live_mailbox_stage: str = "email1"


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
    parser.add_argument("--live-mailbox-approval-only", action="store_true", help="One-shot scan only for AMH live-mailbox approval replies in the pinned approval thread")
    parser.add_argument("--live-mailbox-stage", choices=("email1", "email2"), default="email1", help="Pinned live-mailbox approval stage to collect")
    parser.add_argument("--once", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.live_mailbox_approval_only and not parsed.once:
        parser.error("--live-mailbox-approval-only requires --once")
    root = parsed.root
    manager_file = parsed.manager_file
    if manager_file is not None and not manager_file.is_absolute():
        manager_file = root / manager_file
    return Args(
        root,
        parsed.manager_url.rstrip("/"),
        parsed.mail_dir,
        parsed.state_dir,
        manager_file,
        parsed.once,
        "",
        parsed.recovery_debounce_s,
        parsed.restart_script,
        parsed.idle_wait_s,
        parsed.manager_target.strip(),
        parsed.imap_timeout_s,
        parsed.pull_interval_s,
        parsed.idle_exit_after_s,
        parsed.unread_compression_threshold,
        parsed.recent_cleanup_threshold,
        parsed.recent_cleanup_window_s,
        mail_thresholds=True,
        live_mailbox_approval_only=parsed.live_mailbox_approval_only,
        live_mailbox_stage=parsed.live_mailbox_stage,
    )


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


def exact_human_sender(msg: Message, human_email: str, require_transport_identity: bool) -> bool:
    """Validate the visible sender and, in split mode, Gmail's transport sender."""
    from_headers = [str(value) for value in msg.get_all("From", [])]
    if len(from_headers) != 1 or not from_self(from_headers[0], human_email):
        return False
    sender_headers = [str(value) for value in msg.get_all("Sender", [])]
    if len(sender_headers) > 1:
        return False
    if len(sender_headers) == 1 and not from_self(sender_headers[0], human_email):
        return False
    if not require_transport_identity:
        return True
    return_path_headers = [str(value) for value in msg.get_all("Return-Path", [])]
    return (
        bool(return_path_headers)
        and all(from_self(header, human_email) for header in return_path_headers)
        and gmail_spf_authenticated_sender(msg, human_email)
    )


def gmail_spf_authenticated_sender(msg: Message, sender_email: str) -> bool:
    """Require Gmail's top authentication result to pass SPF for the exact sender."""
    escaped_sender = re.escape(sender_email.casefold())
    headers = msg.get_all("Authentication-Results", [])
    if len(headers) != 1:
        return False
    parts = str(headers[0]).split(";")
    if parts[0].strip().casefold() not in TRUSTED_AUTH_SERVERS:
        return False
    clean_segments = [" ".join(segment.casefold().split()) for segment in parts[1:]]
    return any(
        re.search(r"(?:^|\s)spf=pass(?:\s|$)", segment) is not None
        and re.search(rf"(?:^|\s)smtp\.mailfrom={escaped_sender}(?:\s|$)", segment) is not None
        for segment in clean_segments
    )


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
    if subject_base is not None:
        base = subject_base(subject)
    else:
        base = MANAGER_REPLY_SUBJECT_RE.sub("", subject, count=1).strip()
        while True:
            next_base = re.sub(r"^(?:\[[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\]|[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?:\s+|$)", "", base, count=1).strip()
            if next_base == base:
                break
            base = next_base
    return f"Re: {base}" if re.match(r"^\s*re:\s*", subject, flags=re.IGNORECASE) else base


def subject_manager_target(subject: str) -> str:
    match = MANAGER_TARGET_SUBJECT_RE.match(subject.strip())
    return next((group for group in match.groups() if group), "") if match is not None else ""


def target_aliases(target: str) -> set[str]:
    if not target:
        return set()
    aliases = {target}
    window_target, dot, _pane = target.rpartition(".")
    if dot and ":" in window_target:
        aliases.add(window_target)
    elif not dot:
        aliases.add(f"{target}.0")
    return aliases


def runat_targets(text: str, work_log_root: Path | None = None) -> list[str]:
    try:
        metadata = parse_task_metadata(text, work_log_root)
    except TaskFrontmatterError:
        return []
    if metadata is not None:
        return [metadata.runat]
    return []


def managerat_target(text: str, work_log_root: Path | None = None) -> str:
    try:
        metadata = parse_task_metadata(text, work_log_root)
    except TaskFrontmatterError:
        return ""
    if metadata is not None:
        return metadata.managerat
    return ""


def is_ignored(path: Path) -> bool:
    return bool(set(path.parts) & IGNORE_PARTS)


def todo_task_candidates(root: Path) -> list[Path]:
    todo = root / "TODO.md"
    if not todo.exists():
        return []
    resolved_root = root.resolve()
    try:
        text = todo.read_text(encoding="utf-8")
    except OSError:
        return []
    candidates: list[Path] = []
    seen: set[Path] = set()
    for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", text):
        path = (root / match).resolve(strict=False)
        if path in seen or path == todo.resolve(strict=False) or (path != resolved_root and resolved_root not in path.parents):
            continue
        seen.add(path)
        candidates.append(path)
    return candidates


ACTIVE_TODO_SECTIONS = {"current", "human pending", "low priority"}


def current_todo_task_candidates(root: Path) -> list[Path]:
    todo = root / "TODO.md"
    if not todo.exists():
        return []
    resolved_root = root.resolve()
    try:
        lines = todo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    candidates: list[Path] = []
    seen: set[Path] = set()
    in_active_section = False
    for line in lines:
        stripped = line.strip().casefold()
        section = stripped[:-1] if stripped.endswith(":") else ""
        if section in ACTIVE_TODO_SECTIONS:
            in_active_section = True
            continue
        if stripped.endswith(":"):
            in_active_section = False
        if not in_active_section:
            continue
        for match in re.findall(r"`?([A-Za-z0-9_./-]+\.md)`?", line):
            path = (root / match).resolve(strict=False)
            if path in seen or path == todo.resolve(strict=False) or (path != resolved_root and resolved_root not in path.parents):
                continue
            seen.add(path)
            candidates.append(path)
    return candidates


def markdown_task_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*.md"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if is_ignored(rel) or path.name == "TODO.md" or path.name.startswith("work_manager_"):
            continue
        paths.append(path)
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns if path.exists() else 0, reverse=True)


def task_file_for_target(root: Path, tmux_target: str) -> Path | None:
    return task_file_for_target_in_candidates(root, tmux_target, todo_task_candidates(root))


def current_task_file_for_target(root: Path, tmux_target: str) -> Path | None:
    return task_file_for_target_in_candidates(root, tmux_target, current_todo_task_candidates(root))


def current_route_for_owner(args: Args, owner_target: str) -> EmailRoute | None:
    if not owner_target:
        return None
    if owner_target in target_aliases(args.manager_target):
        if current_task_file_for_target(args.root, owner_target) is None and ":" in owner_target:
            return None
        return EmailRoute(current_manager_file(args), args.manager_target, pending_watcher_delivery=True)
    owner_file = current_task_file_for_target(args.root, owner_target)
    if owner_file is None:
        return None
    return EmailRoute(owner_file, owner_target, pending_watcher_delivery=True)


def sendable_codex_target(target: str) -> bool:
    if not target:
        return False
    try:
        require_sendable_codex_target(target)
    except Exception:
        return False
    return True


def fallback_manager_target_for_file(args: Args, manager_file: Path, requested_target: str) -> str:
    if not requested_target or sendable_codex_target(requested_target):
        return requested_target
    try:
        manager_text = manager_file.read_text(encoding="utf-8")
        metadata = parse_task_metadata(manager_text, args.root) if manager_text else None
    except (OSError, TaskFrontmatterError):
        metadata = None
    owner_target = metadata.managerat if metadata is not None else ""
    if owner_target and owner_target not in target_aliases(requested_target):
        owner_route = current_route_for_owner(args, owner_target)
        if owner_route is not None and sendable_codex_target(owner_route.manager_target):
            logging.warning("email route target is not sendable; using owner manager: task=%s target=%s owner=%s", manager_file, requested_target, owner_route.manager_target)
            return owner_route.manager_target
    if requested_target not in target_aliases(args.manager_target) and sendable_codex_target(args.manager_target):
        logging.warning("email route target is not sendable; using default manager: task=%s target=%s default=%s", manager_file, requested_target, args.manager_target)
        return args.manager_target
    return requested_target


def task_file_for_target_in_candidates(root: Path, tmux_target: str, candidates: list[Path]) -> Path | None:
    aliases = target_aliases(tmux_target)
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        try:
            targets = runat_targets(path.read_text(encoding="utf-8"), root)
        except OSError:
            continue
        if any(target in aliases for target in targets):
            return path
    return None


def inactive_task_files_for_target(root: Path, tmux_target: str) -> list[Path]:
    aliases = target_aliases(tmux_target)
    active = {path.resolve(strict=False) for path in current_todo_task_candidates(root)}
    matches: list[Path] = []
    for path in markdown_task_files(root):
        resolved = path.resolve(strict=False)
        if resolved in active or not path.is_file():
            continue
        try:
            targets = runat_targets(path.read_text(encoding="utf-8"), root)
        except OSError:
            continue
        if any(target in aliases for target in targets):
            matches.append(path)
    return matches


def email_route(args: Args, subject: str, body: str = "") -> EmailRoute:
    del body
    tmux_target = subject_manager_target(subject)
    if not tmux_target:
        return EmailRoute(current_manager_file(args), args.manager_target, pending_watcher_delivery=True)
    manager_file = current_task_file_for_target(args.root, tmux_target)
    if manager_file is None:
        for inactive_file in inactive_task_files_for_target(args.root, tmux_target):
            try:
                inactive_owner_target = managerat_target(inactive_file.read_text(encoding="utf-8"), args.root)
            except OSError:
                inactive_owner_target = ""
            owner_route = current_route_for_owner(args, inactive_owner_target)
            if owner_route is not None:
                return owner_route
        logging.warning("sub-manager email target did not map to a task file; using default manager: target=%s", tmux_target)
        return EmailRoute(current_manager_file(args), args.manager_target, pending_watcher_delivery=True)
    try:
        manager_text = manager_file.read_text(encoding="utf-8")
    except OSError:
        manager_text = ""
    try:
        metadata = parse_task_metadata(manager_text, args.root) if manager_text else None
    except TaskFrontmatterError:
        metadata = None
    if metadata is not None:
        return EmailRoute(manager_file, metadata.runat, pending_watcher_delivery=True)
    return EmailRoute(manager_file, fallback_manager_target_for_file(args, manager_file, tmux_target), pending_watcher_delivery=True)


def manager_target_for_file(args: Args, manager_file: Path) -> str:
    current = current_manager_file(args)
    if manager_file == current or manager_file.name.startswith("work_manager_"):
        return args.manager_target
    try:
        text = manager_file.read_text(encoding="utf-8")
    except OSError:
        return args.manager_target
    try:
        metadata = parse_task_metadata(text, args.root)
    except TaskFrontmatterError:
        metadata = None
    if metadata is not None:
        return metadata.runat if metadata.is_manager else metadata.managerat
    return args.manager_target


def args_for_manager_file(args: Args, manager_file: Path, pending_line: int = 0) -> Args:
    del pending_line
    manager_target = fallback_manager_target_for_file(args, manager_file, manager_target_for_file(args, manager_file))
    return replace(args, manager_file=manager_file, manager_target=manager_target)


def is_recovery_subject(subject: str) -> bool:
    return " ".join(subject.split()) in RECOVERY_SUBJECTS


def has_agent_footer(text: str) -> bool:
    return PWD_FOOTER_RE.search(text) is not None or TMUX_FOOTER_RE.search(text) is not None


def manager_url_is_loopback(manager_url: str) -> bool:
    parsed = urlparse(manager_url.rstrip("/"))
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is not None


def source_ref(root: Path, txt_path: Path) -> Path:
    try:
        return txt_path.relative_to(root)
    except ValueError:
        pass
    try:
        return txt_path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return txt_path


def processed_uids_path(args: Args) -> Path:
    return email_uid_state_path(args, "email-processed-uids")


def ignored_uids_path(args: Args) -> Path:
    return email_uid_state_path(args, "email-ignored-uids")


def unaccepted_pending_uids_path(args: Args) -> Path:
    return email_uid_state_path(args, "email-unaccepted-pending-uids")


def email_uid_state_path(args: Args, stem: str) -> Path:
    """Keep Gmail UID state separate when the watched mailbox changes."""
    if not args.inbox_identity:
        return args.state_dir / f"{stem}.tsv"
    mailbox_id = hashlib.sha256(args.inbox_identity.strip().casefold().encode()).hexdigest()[:12]
    return args.state_dir / f"{stem}-{mailbox_id}.tsv"


def mailbox_state_identity(client: imaplib.IMAP4_SSL, mailbox_address: str) -> str:
    """Bind Gmail UID state to the mailbox's current UIDVALIDITY epoch."""
    typ, data = client.response("UIDVALIDITY")
    if typ != "UIDVALIDITY" or not data or not data[0]:
        raise RuntimeError("email watcher could not read mailbox UIDVALIDITY")
    raw_epoch = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    if not raw_epoch.isdecimal():
        raise RuntimeError("email watcher received invalid mailbox UIDVALIDITY")
    return f"{mailbox_address.casefold()}\0{raw_epoch}"


def manager_mail_counts_path(args: Args) -> Path:
    return email_uid_state_path(args, "email-manager-mail-counts")


def manager_mail_threshold_state_path(args: Args) -> Path:
    return email_uid_state_path(args, "email-manager-mail-thresholds")


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


def email_source_lines(root: Path, txt_path: Path) -> tuple[str, ...]:
    ref = source_ref(root, txt_path)
    return f"(record and delegate {ref})", f"(from email {ref})", f"[source: email {ref}]"


def source_search_files(root: Path, manager_file: Path | None = None) -> list[Path]:
    candidates = [manager_file or dated_manager_file(root), *sorted(root.glob("work_manager_*.md")), *todo_task_candidates(root)]
    files: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append(path)
    return files


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
    for path in source_search_files(root, manager_file):
        source_line = existing_source_line(root, txt_path, path)
        if source_line is not None:
            return source_line
    return None


def source_marker_consumed_by_routed_prose(lines: list[str], source_idx: int) -> bool:
    routed_prose_seen = False
    for prior_idx in range(source_idx - 1, -1, -1):
        stripped = lines[prior_idx].strip()
        if not stripped:
            continue
        if stripped.startswith(ROUTED_PREFIXES):
            routed_prose_seen = True
            continue
        if stripped == "(pending)":
            return routed_prose_seen
        if stripped.startswith(("(", "#")):
            return False
    return False


def consumed_source_reference_line(stripped: str, ref: str) -> bool:
    quoted_ref = rf"`{re.escape(ref)}`"
    plain_ref = rf"{re.escape(ref)}(?![A-Za-z0-9_-])(?!(?:\.[A-Za-z0-9_-]))"
    ref_pattern = rf"(?:{quoted_ref}|{plain_ref})"
    negative_patterns = (
        r"\bunhandled\b",
        r"\bnot\s+acknowledged\b",
        r"\bnot\s+consumed\b",
        r"\bnot\s+handled\b",
        r"\bnot\s+removed\b",
        r"\bnot\s+cleared\b",
        r"\bnot\s+routed\b",
        r"\bnot-yet\s+(?:acknowledged|consumed|handled|removed|cleared|routed)\b",
        r"\bnot\s+yet\s+(?:acknowledged|consumed|handled|removed|cleared|routed)\b",
        r"\bstill[-\s]+pending\b",
        r"\bis[-\s]+pending\b",
        r"\bpending\b(?!\s+watcher\s+markers?\b)",
        r"\bremains[-\s]+pending\b",
        r"\bpending[-\s]+remains\b",
        r"\bstill[-\s]+needs(?:[-\s]+work)?\b",
        r"\bneeds[-\s]+work\b",
        r"\bkeep\s+pending\b",
        r"\bdeferred\b",
        r"\bactive\b",
        r"\blive\s+mail\b",
        r"\bis\s+live\b",
        r"\bfollow-up\b",
    )
    if not re.search(ref_pattern, stripped):
        return False
    if stripped.startswith((*ROUTED_PREFIXES, "(done", "(running", "(blocked")):
        return True
    if not stripped.startswith("(comment:"):
        return False
    mail_ref_pattern = r"`?manager_mail/\d+\.txt`?"
    mail_refs = list(re.finditer(mail_ref_pattern, stripped))
    for pattern in negative_patterns:
        for negative_match in re.finditer(pattern, stripped, re.IGNORECASE):
            negative_prefix = stripped[max(0, negative_match.start() - 32) : negative_match.start()]
            if negative_match.group(0).lower() == "pending" and re.search(
                r"(?:\bno\b|\bnot\b|\bno\s+longer\b)\s*$",
                negative_prefix,
                re.IGNORECASE,
            ):
                continue
            if negative_match.group(0).lower() == "pending" and re.search(
                r"\b(?:removed|cleared|consumed)\s+.*(?:repeated|duplicate|stale)\s*$",
                stripped[: negative_match.start()],
                re.IGNORECASE,
            ):
                continue
            if re.search(r"\bactive\b|\bfollow-up\b|\blive\s+mail\b", negative_match.group(0), re.IGNORECASE) and re.search(
                r"(?:\bno\b|\bnot\b|\bno\s+longer\b|\binactive\b)(?:\s+active(?:\s+or)?)?(?:\s+a)?\s*$",
                negative_prefix,
                re.IGNORECASE,
            ):
                continue
            previous_ref = next((match for match in reversed(mail_refs) if match.end() <= negative_match.start()), None)
            next_ref = next((match for match in mail_refs if match.start() >= negative_match.end()), None)
            previous_is_target = previous_ref is not None and re.fullmatch(ref_pattern, previous_ref.group(0))
            next_is_target = next_ref is not None and re.fullmatch(ref_pattern, next_ref.group(0))
            target_before_negative = next(
                (match for match in reversed(list(re.finditer(ref_pattern, stripped[: negative_match.start()])))),
                None,
            )
            if next_is_target and previous_ref is None:
                tail = stripped[negative_match.end() : next_ref.start()]
                if negative_match.group(0).lower() == "pending" and re.search(
                    r"\b(?:removed|cleared|consumed)\s+.*(?:pending|watcher)\s+(?:markers?|batch)\b",
                    tail,
                    re.IGNORECASE,
                ):
                    continue
                return False
            if next_is_target and previous_ref is not None:
                head = stripped[previous_ref.end() : negative_match.start()]
                if ";" in head or re.search(r"\.\s*", head):
                    return False
            if previous_is_target and next_ref is None:
                return False
            if target_before_negative is not None and next_ref is None:
                target_tail = stripped[target_before_negative.end() : negative_match.start()]
                target_head = stripped[: target_before_negative.start()]
                status_boundary = re.search(r";|(?<=`)\.\s|(?<=\.txt)\.\s|\band\s*$", target_tail)
                before_status = target_tail[: status_boundary.start()] if status_boundary is not None else target_tail
                status_prefix = target_tail[status_boundary.end() :] if status_boundary is not None else ""
                before_status_words = {
                    word.lower()
                    for word in re.findall(r"\b[A-Za-z]+\b", re.sub(mail_ref_pattern, "", before_status))
                }
                if (
                    status_boundary is not None
                    and re.search(mail_ref_pattern, status_prefix) is None
                    and before_status_words <= {"and", "or"}
                    and re.search(
                    r"\b(?:removed|cleared|consumed)\s+.*(?:pending|watcher)\s+(?:markers?|batch)\b",
                    target_head,
                    re.IGNORECASE,
                    )
                ):
                    return False
            if previous_is_target and next_is_target:
                return False
            if previous_is_target and next_ref is not None:
                tail = stripped[negative_match.end() : next_ref.start()]
                if ";" in tail or re.search(r"\.\s*", tail) or re.search(r"\b(?:behind|after|before)\b", tail, re.IGNORECASE):
                    return False
    for clause in re.split(r";\s*|(?<=[`)])\.\s*", stripped):
        if not re.search(ref_pattern, clause):
            continue
        consumed_verbs = ("acknowledged", "consumed", "handled", "routed", "removed", "cleared")
        if re.search(rf"^(?:\(comment:\s*)?(?:{'|'.join(consumed_verbs)})\s+{ref_pattern}(?:$|\b|[\s`,.;)])", clause.strip(), re.IGNORECASE):
            return True
        lowered = clause.lower()
        stale_cleanup = r"(?:removed|cleared|consumed)\s+(?:another\s+|immediately\s+)?(?:(?:repeated|duplicate)(?:\s+stale)?|stale(?:\s+(?:repeated|duplicate))?)?\s+(?:(?:pending|watcher)\s+)*(?:markers?|batch)\b"
        for stale_match in re.finditer(rf"\b{stale_cleanup}", clause, re.IGNORECASE):
            stale_tail = clause[stale_match.end() :]
            cleanup_targets = re.split(
                r"\b(?:then|later|mentioned|noted|reviewed|separately|deferred|did\s+not|not\s+(?:acknowledged|consumed|handled|removed|cleared|routed)|still[-\s]+pending|is[-\s]+pending|pending|remains[-\s]+pending|pending[-\s]+remains|still[-\s]+needs(?:[-\s]+work)?|needs[-\s]+work|keep\s+pending|active|follow-up)\b|;",
                stale_tail,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            cleanup_target = re.search(ref_pattern, cleanup_targets)
            if cleanup_target is None:
                continue
            cleanup_prefix = cleanup_targets[: cleanup_target.start()]
            cleanup_prefix = re.sub(mail_ref_pattern, "", cleanup_prefix)
            cleanup_words = re.findall(r"\b[A-Za-z]+\b", cleanup_prefix)
            cleanup_suffix = cleanup_targets[cleanup_target.end() :]
            suffix_has_other_ref = re.search(mail_ref_pattern, cleanup_suffix) is not None
            cleanup_suffix = re.sub(mail_ref_pattern, "", cleanup_suffix)
            suffix_words = {word.lower() for word in re.findall(r"\b[A-Za-z]+\b", cleanup_suffix)}
            allowed_suffix_words = {"and", "or", "but", "while", "for", "mail", "remains", "no", "not", "longer", "a"}
            if suffix_has_other_ref:
                allowed_suffix_words.add("is")
            inactive_suffix_without_connector = (
                bool(suffix_words & {"no", "not", "longer"})
                and not suffix_has_other_ref
                and not re.match(r"\s*(?:and|but|while)\b", cleanup_suffix, re.IGNORECASE)
            )
            list_connector = (
                (not cleanup_words or cleanup_words[-1].lower() in {"for", "and", "or"})
                and suffix_words <= allowed_suffix_words
                and not inactive_suffix_without_connector
            )
            if list_connector and not re.search(rf"\bnot\s+(?:for\s+)?{ref_pattern}", cleanup_targets, re.IGNORECASE) and ("watcher marker" in lowered or "stale" in lowered or "duplicate" in lowered):
                return True
    return False


def existing_consumed_source_line(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    ref = str(source_ref(root, txt_path))
    source_lines = set(email_source_lines(root, txt_path))
    for path in source_search_files(root, manager_file):
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if consumed_source_reference_line(stripped, ref):
                return idx + 1
            if stripped in source_lines and source_marker_consumed_by_routed_prose(lines, idx):
                return idx + 1
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
        if stripped.startswith(ROUTED_PREFIXES):
            break
        if stripped.startswith(("(", "#")) and stripped != "(pending)":
            break
    return None


def existing_source_pending_line_in_root(root: Path, txt_path: Path, manager_file: Path | None = None) -> int | None:
    for path in source_search_files(root, manager_file):
        pending_line = existing_source_pending_line(root, txt_path, path)
        if pending_line is not None:
            return pending_line
    return None


def existing_source_pending_path_line_in_root(root: Path, txt_path: Path, manager_file: Path | None = None) -> tuple[Path, int] | None:
    for path in source_search_files(root, manager_file):
        pending_line = existing_source_pending_line(root, txt_path, path)
        if pending_line is not None:
            return path, pending_line
    return None


def append_pending(
    root: Path,
    txt_path: Path,
    manager_file: Path | None = None,
) -> int:
    manager_file = manager_file or dated_manager_file(root)
    with task_file_lock(manager_file):
        existing_line = existing_source_pending_line(root, txt_path, manager_file)
        if existing_line is not None:
            return existing_line
        consumed_line = existing_consumed_source_line(root, txt_path, manager_file)
        if consumed_line is not None:
            return consumed_line
        text = manager_file.read_text(encoding="utf-8") if manager_file.exists() else ""
        pending_line = len(text.splitlines()) + 2
        from_line = email_source_lines(root, txt_path)[0]
        separator = "\n" if not text or text.endswith("\n") else "\n\n"
        with manager_file.open("a", encoding="utf-8") as handle:
            _ = handle.write(f"{separator}(pending)\n{from_line}\n")
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(manager_file.parent)
        return pending_line


def pending_marker_present(root: Path, pending_file: Path, pending_line: int) -> bool:
    try:
        lines = (root / pending_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    idx = pending_line - 1
    return 0 <= idx < len(lines) and lines[idx].strip() == "(pending)"


def pending_watcher_delivery_present(root: Path, pending_file: Path, pending_line: int) -> bool:
    return pending_marker_present(root, pending_file, pending_line)


def threshold_push_failure_marker(kind: str) -> str:
    return f"(email_idle_watcher manager-mail-threshold-push-failed {kind})"


def sanitized_one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def claim_threshold_push_failure(state_dir: Path | None, root: Path, pending_file: Path, pending_line: int, kind: str) -> Path | None:
    if state_dir is None:
        return None
    digest = hashlib.sha256(f"{root.resolve(strict=False)}\n{pending_file}\n{pending_line}\n{kind}".encode("utf-8")).hexdigest()
    claim_dir = state_dir / "email-threshold-push-failures"
    try:
        claim_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(claim_dir / f"{digest}.seen", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{pending_file}\t{pending_line}\t{kind}\n")
    return claim_dir / f"{digest}.seen"


def record_threshold_push_failure(root: Path, pending_file: Path, pending_line: int, kind: str, target: str, error: str, state_dir: Path | None = None) -> bool:
    path = root / pending_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    idx = pending_line - 1
    if idx < 0 or idx >= len(lines) or lines[idx].strip() != "(pending)":
        return False
    marker = threshold_push_failure_marker(kind)
    if any(line.strip() == marker for line in lines):
        return False
    claim_path = claim_threshold_push_failure(state_dir, root, pending_file, pending_line, kind)
    if state_dir is not None and claim_path is None:
        return False
    failure_text = "\n".join(
        [
            "",
            marker,
            f"manager mail threshold tmux poke failed: target={sanitized_one_line(target)} error={sanitized_one_line(error)}; durable pending marker remains for pending watcher dispatch",
        ]
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{failure_text}\n")
    except OSError:
        if claim_path is not None:
            claim_path.unlink(missing_ok=True)
        return False
    return True


def run_email_push(push: EmailPush) -> bool:
    if not pending_marker_present(push.root, push.pending_file, push.line_no):
        logging.error("email pending async push skipped: line=%s marker cleared", push.line_no)
        return False
    try:
        send_to_codex(
            push.target,
            push.text,
            CodexSendOptions(
                DEFAULT_TMUX_ENTER_COUNT,
                0.15,
                False,
                DEFAULT_EMAIL_PUSH_SUBMIT_VERIFY_TIMEOUT_S,
                True,
            ),
        )
    except Exception as exc:
        logging.error("email pending async push failed: line=%s error=%s", push.line_no, exc)
        if push.threshold_kind:
            record_threshold_push_failure(push.root, push.pending_file, push.line_no, push.threshold_kind, push.target, str(exc), push.state_dir)
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


def email_tmux_push(args: Args, text: str, ref: Path, line_no: int, threshold_kind: str = "") -> EmailPush:
    return EmailPush(line_no, args.manager_target, text, args.root, ref, threshold_kind, args.state_dir)


def push_email_ref(args: Args, line_no: int) -> bool:
    if not args.manager_target:
        logging.error("email pending push failed: manager target is required")
        return False
    manager_file = current_manager_file(args)
    ref = manager_file.relative_to(args.root) if manager_file.is_relative_to(args.root) else manager_file
    return run_email_push(email_tmux_push(args, f"pending: file={ref} line={line_no} origin=human source=email action=ack-human", Path(ref), line_no))


def push_manager_mail_threshold_ref(args: Args, line_no: int, kind: str) -> bool:
    if not args.manager_target:
        logging.error("manager mail threshold push failed: kind=%s manager target is required", kind)
        return False
    manager_file = current_manager_file(args)
    ref = manager_file.relative_to(args.root) if manager_file.is_relative_to(args.root) else manager_file
    text = f"pending: file={ref} line={line_no} origin=agent source=email-watcher action=no-human-ack kind={kind}"
    try:
        start_email_push_worker()
    except RuntimeError as exc:
        logging.error("manager mail threshold async push worker start failed: kind=%s line=%s error=%s", kind, line_no, exc)
        record_threshold_push_failure(args.root, Path(ref), line_no, kind, args.manager_target, str(exc), args.state_dir)
        return False
    _email_push_queue.put(email_tmux_push(args, text, Path(ref), line_no, kind))
    logging.info("manager mail threshold async push queued: kind=%s line=%s", kind, line_no)
    return True


def threshold_marker(kind: str) -> str:
    return f"(from agent email_idle_watcher manager-mail-threshold {kind})"


def legacy_threshold_marker(kind: str) -> str:
    return f"(from manager-email-threshold {kind})"


def threshold_markers(kind: str) -> set[str]:
    return {threshold_marker(kind), legacy_threshold_marker(kind)}


def existing_current_threshold_pending_line(manager_file: Path, kind: str) -> int | None:
    if not manager_file.exists():
        return None
    lines = manager_file.read_text(encoding="utf-8").splitlines()
    markers = threshold_markers(kind)
    for idx, line in enumerate(lines):
        if line.strip() not in markers:
            continue
        for pending_idx in range(max(0, idx - 6), idx):
            if lines[pending_idx].strip() == "(pending)":
                return pending_idx + 1
    return None


def append_manager_mail_threshold_pending(args: Args, kind: str, counts: ManagerMailCounts, dedupe_current: bool = True) -> int:
    manager_file = current_manager_file(args)
    if dedupe_current:
        existing_line = existing_current_threshold_pending_line(manager_file, kind)
        if existing_line is not None:
            return existing_line
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    if kind == "unread-compression":
        summary = f"manager email watcher threshold: unread manager mail {counts.unread} exceeds {args.unread_compression_threshold}"
        route = "route a worker through `~/.config/omo_manager/docs/mail/compression.md`"
        retention = "compress only unread manager-sent mail, retain full-read memos, send replacement summaries first, then move only explicitly superseded source UIDs to Trash"
    elif kind == "recent-cleanup":
        hours = args.recent_cleanup_window_s / 3600
        summary = f"manager email watcher threshold: manager-human mail within last {hours:g}h is {counts.recent_total}, exceeding {args.recent_cleanup_threshold}"
        route = "route a worker through `~/.config/omo_manager/docs/mail/cleanup.md` and the compression workflow if replacement summaries are needed"
        retention = "threshold is trigger-only; rerun cleanup classification and retain recent, unread, active, human-pending, long-report, and uncertain threads"
    else:
        raise ValueError(f"unknown manager mail threshold kind: {kind}")
    block = [
        "",
        "(pending)",
        threshold_marker(kind),
        summary,
        f"- counts: manager_total={counts.total} manager_unread={counts.unread} manager_human_recent={counts.recent_total} recent_window_s={int(counts.recent_window_s)}",
        f"- action: {route}",
        f"- retention: {retention}",
    ]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def ensure_manager_mail_threshold_pending(args: Args, kind: str, counts: ManagerMailCounts) -> tuple[int, bool]:
    manager_file = current_manager_file(args)
    existing_line = existing_current_threshold_pending_line(manager_file, kind)
    if existing_line is not None:
        return existing_line, False
    return append_manager_mail_threshold_pending(args, kind, counts, dedupe_current=False), True


def append_recovery_record(root: Path, txt_path: Path, summary: str, manager_file: Path | None = None) -> int:
    manager_file = manager_file or dated_manager_file(root)
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    from_line, legacy_source_line, *_ = email_source_lines(root, txt_path)
    block = ["", f"(manager recovery email: {stamp})", from_line, legacy_source_line, summary]
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def write_mail(args: Args, uid: str, msg: Message, _sender: str, subject: str) -> Path:
    missing_dirs: list[Path] = []
    candidate = args.mail_dir
    while not candidate.exists():
        missing_dirs.append(candidate)
        candidate = candidate.parent
    args.mail_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for created_dir in reversed(missing_dirs):
        fsync_directory(created_dir.parent)
    args.mail_dir.chmod(0o700)
    txt_path = args.mail_dir / mail_artifact_name(args, uid)
    body = f"Subject: {normalize_human_subject(subject)}\n\n{message_text(msg)}"
    fd = os.open(txt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(args.mail_dir)
    return txt_path


def mail_artifact_name(args: Args, uid: str) -> str:
    """Keep durable mail pointers unique across mailboxes and UID epochs."""
    if not args.inbox_identity:
        return f"{uid}.txt"
    mailbox_id = hashlib.sha256(args.inbox_identity.strip().casefold().encode()).hexdigest()[:12]
    return f"{mailbox_id}-{uid}.txt"


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
    command = [str(Path.home() / ".config/helper.sh/email_me.py"), "--manager-human"]
    if args.manager_target:
        command.extend(("--sender-tmux-target", args.manager_target))
    command.extend(("--subject-file", str(subject_path), "--message-file", str(body_path)))
    try:
        result = subprocess.run(command, text=True, check=False)
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
            email_human(args, "Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart was refused because manager-url is not loopback: {args.manager_url}\n\nRun only after correcting configuration:\n\n```sh\n{shell_join(command)}\n```\n")
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
                email_human(args, "Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart helper launch failed.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
                return
        record_recovery_attempt(last_path, now_s, uid, f"returncode={result.returncode}")
        if result.returncode != 0:
            append_recovery_record(args.root, txt_path, f"recovery restart failed with exit {result.returncode}; see `{log_path}`", args.manager_file)
            email_human(args, "Recovery action needed", f"Recovery email {source_ref(args.root, txt_path)} was accepted from the configured self address, but automatic restart failed with exit {result.returncode}.\n\nLog: {log_path}\n\nManual recovery command:\n\n```sh\n{shell_join(command)}\n```\n")
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


def search_sender_uids(client: imaplib.IMAP4_SSL, sender_email: str, processed_uids: set[str]) -> set[bytes]:
    """Find new mail from the only authorized human sender, regardless of subject."""
    candidate_uids: set[bytes] = set()
    typ, data = client.uid("search", "UNSEEN", "FROM", f'"{sender_email}"')
    if typ == "OK" and data and data[0]:
        candidate_uids.update(data[0].split())
    if processed_uids:
        typ, data = client.uid("search", None, "UID", uid_search_range(processed_uids), "FROM", f'"{sender_email}"')
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


def search_processed_sender_uids(client: imaplib.IMAP4_SSL, sender_email: str, uids: list[str]) -> set[bytes]:
    """Retry explicitly unaccepted mail from the authorized human sender."""
    candidate_uids: set[bytes] = set()
    for uid_chunk in chunks(uids, 50):
        typ, data = client.uid("search", None, "UID", ",".join(uid_chunk), "FROM", f'"{sender_email}"')
        if typ == "OK" and data and data[0]:
            candidate_uids.update(data[0].split())
    return candidate_uids


def search_live_mailbox_approval_uids(
    client: imaplib.IMAP4_SSL,
    *,
    sender_email: str,
    subject: str,
    request_message_id: str,
    processed_uids: set[str],
) -> set[bytes]:
    """Find only replies bound to the pinned live-mailbox approval request."""
    candidate_uids: set[bytes] = set()
    for header_name in ("In-Reply-To", "References"):
        typ, data = client.uid(
            "search",
            "UNSEEN",
            "FROM",
            f'"{sender_email}"',
            "SUBJECT",
            f'"{subject}"',
            "HEADER",
            header_name,
            request_message_id,
        )
        if typ == "OK" and data and data[0]:
            candidate_uids.update(data[0].split())
        if processed_uids:
            typ, data = client.uid(
                "search",
                None,
                "UID",
                uid_search_range(processed_uids),
                "FROM",
                f'"{sender_email}"',
                "SUBJECT",
                f'"{subject}"',
                "HEADER",
                header_name,
                request_message_id,
            )
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


def search_manager_mail_uids(
    client: imaplib.IMAP4_SSL,
    sender_email: str,
    recipient_email: str,
    unread: bool = False,
    since: datetime | None = None,
    require_subject_tags: bool = True,
) -> list[str]:
    criteria: list[str] = []
    if unread:
        criteria.append("UNSEEN")
    if since is not None:
        criteria.extend(["SINCE", since.strftime("%d-%b-%Y")])
    found: list[str] = []
    seen: set[str] = set()
    subject_tokens = LEGACY_MANAGER_SUBJECT_TOKENS if require_subject_tags else ("",)
    for token in subject_tokens:
        boundary = ["FROM", f'"{sender_email}"', "TO", f'"{recipient_email}"']
        subject = ["SUBJECT", f'"{token}"'] if token else []
        typ, data = client.uid("search", *(criteria + boundary + subject))
        if typ != "OK":
            raise RuntimeError(f"IMAP manager mail search failed: {typ}")
        for uid in decode_search_uids(data):
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def fetch_manager_count_header(client: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    typ, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"IMAP manager mail header fetch failed: uid={uid} typ={typ}")
    return BytesParser(policy=policy.default).parsebytes(data[0][1])


def is_mail_cleanup_excluded_subject(subject: str) -> bool:
    normalize = subject_base
    if normalize is None:
        try:
            from .omo_email_subject import subject_base as normalize
        except ImportError:
            from omo_email_subject import subject_base as normalize
    return PB_CLEANUP_EXCLUDED_SUBJECT_RE.match(normalize(subject)) is not None


def is_manager_mail_header(msg: Message, sender_email: str, recipient_email: str, require_subject_tags: bool) -> bool:
    normalized_recipient = recipient_email.casefold()
    sender = str(msg.get("From", ""))
    recipients = str(msg.get("To", ""))
    subject = str(msg.get("Subject", ""))
    return (
        from_self(sender, sender_email)
        and any(address.casefold() == normalized_recipient for _name, address in getaddresses([recipients]))
        and (not require_subject_tags or any(token.lower() in subject.lower() for token in LEGACY_MANAGER_SUBJECT_TOKENS))
    )


def cleanup_manager_mail_uids(
    client: imaplib.IMAP4_SSL,
    sender_email: str,
    recipient_email: str,
    candidate_uids: list[str],
    require_subject_tags: bool,
    header_cache: dict[str, Message | None] | None = None,
) -> list[str]:
    cache = header_cache if header_cache is not None else {}
    accepted: list[str] = []
    for uid in candidate_uids:
        if uid not in cache:
            cache[uid] = fetch_manager_count_header(client, uid)
        msg = cache[uid]
        if msg is None or not is_manager_mail_header(msg, sender_email, recipient_email, require_subject_tags):
            continue
        if not is_mail_cleanup_excluded_subject(str(msg.get("Subject", ""))):
            accepted.append(uid)
    return accepted


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


def recent_manager_mail_uids(
    client: imaplib.IMAP4_SSL,
    sender_email: str,
    recipient_email: str,
    candidate_uids: list[str],
    cutoff: datetime,
    require_subject_tags: bool,
    header_cache: dict[str, Message | None] | None = None,
) -> list[str]:
    cache = header_cache if header_cache is not None else {}
    recent: list[str] = []
    accepted_uids = cleanup_manager_mail_uids(client, sender_email, recipient_email, candidate_uids, require_subject_tags, cache)
    for uid in accepted_uids:
        msg = cache[uid]
        if msg is None:
            continue
        dt = parsed_message_date(msg)
        if dt is not None and dt >= cutoff:
            recent.append(uid)
    return recent


def filter_ignored_uids(uids: list[str], ignored_uids: set[str]) -> list[str]:
    if not ignored_uids:
        return uids
    return [uid for uid in uids if uid not in ignored_uids]


def manager_mail_counts(
    client: imaplib.IMAP4_SSL,
    sender_email: str,
    recent_window_s: float,
    recent_threshold: int,
    now: datetime | None = None,
    ignored_uids: set[str] | None = None,
    recipient_email: str | None = None,
    require_subject_tags: bool = True,
) -> ManagerMailCounts:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(seconds=recent_window_s)
    ignored = ignored_uids or set()
    recipient = recipient_email or sender_email
    header_cache: dict[str, Message | None] = {}
    total_candidates = filter_ignored_uids(search_manager_mail_uids(client, sender_email, recipient, require_subject_tags=require_subject_tags), ignored)
    unread_candidates = filter_ignored_uids(search_manager_mail_uids(client, sender_email, recipient, unread=True, require_subject_tags=require_subject_tags), ignored)
    recent_candidates = filter_ignored_uids(search_manager_mail_uids(client, sender_email, recipient, since=cutoff, require_subject_tags=require_subject_tags), ignored)
    total_uids = cleanup_manager_mail_uids(client, sender_email, recipient, total_candidates, require_subject_tags, header_cache)
    unread_uids = cleanup_manager_mail_uids(client, sender_email, recipient, unread_candidates, require_subject_tags, header_cache)
    recent_total = len(recent_manager_mail_uids(client, sender_email, recipient, recent_candidates, cutoff, require_subject_tags, header_cache))
    return ManagerMailCounts(len(total_uids), len(unread_uids), recent_window_s, recent_total, True)


def handle_manager_mail_thresholds(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    recipient = args.manager_mail_recipient or args.self_email
    ignored_uids = load_processed_uids(ignored_uids_path(args)) if recipient.casefold() == args.self_email.casefold() else set()
    counts = manager_mail_counts(
        client,
        args.self_email,
        args.recent_cleanup_window_s,
        args.recent_cleanup_threshold,
        ignored_uids=ignored_uids,
        recipient_email=recipient,
        require_subject_tags=args.manager_mail_subject_tags,
    )
    save_manager_mail_counts(manager_mail_counts_path(args), counts)
    logging.info("manager mail counts: total=%s unread=%s recent_window_s=%s recent_total=%s recent_exact=%s", counts.total, counts.unread, int(counts.recent_window_s), counts.recent_total, counts.recent_exact)
    threshold_state_path = manager_mail_threshold_state_path(args)
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
            line_no, created = ensure_manager_mail_threshold_pending(args, kind, counts)
            next_active.add(kind)
            save_active_manager_mail_thresholds(threshold_state_path, next_active)
            active = set(next_active)
            if created:
                push_manager_mail_threshold_ref(args, line_no, kind)
                triggered = True
        elif not exceeded and kind in next_active:
            next_active.remove(kind)
            state_changed = True
    if state_changed:
        save_active_manager_mail_thresholds(threshold_state_path, next_active)
    return triggered


def maybe_handle_manager_mail_thresholds(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    if not args.mail_thresholds:
        return False
    try:
        return handle_manager_mail_thresholds(client, args)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        logging.warning("manager mail threshold check failed: %s", exc)
        return False


def handle_split_manager_mail_thresholds(args: Args, settings: AgentMailSettings) -> bool:
    """Count agent-to-human mail in the human inbox, separate from request intake."""
    config_path = human_config_path()
    config = parse_env_config(config_path)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        raise RuntimeError(f"missing email config keys {sorted(missing)} in {config_path}")
    if config["user"].casefold() != settings.human_address.casefold():
        raise RuntimeError("human cleanup mailbox does not match OMO_HUMAN_EMAIL_ADDRESS")
    with imaplib.IMAP4_SSL(config["host"], timeout=args.imap_timeout_s) as client:
        client.login(config["user"], config["password"])
        typ, _data = client.select("INBOX")
        if typ != "OK":
            raise RuntimeError(f"IMAP select human INBOX failed: {typ}")
        threshold_args = replace(
            args,
            self_email=settings.agent_address,
            mail_thresholds=True,
            inbox_identity=mailbox_state_identity(client, config["user"]),
            manager_mail_recipient=settings.human_address,
            manager_mail_subject_tags=False,
        )
        return handle_manager_mail_thresholds(client, threshold_args)


def maybe_handle_split_manager_mail_thresholds(args: Args, settings: AgentMailSettings) -> bool:
    try:
        return handle_split_manager_mail_thresholds(args, settings)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        logging.warning("split manager mail threshold check failed: %s", exc)
        return False


class AmhRouteDisposition(Enum):
    FALLBACK = "fallback"
    HOLD = "hold"
    ADVANCED = "advanced"
    SKIP = "skip"


class LiveMailboxApprovalDisposition(Enum):
    STORED = "stored"
    HARD_REJECT = "hard_reject"
    RETRY = "retry"


class LiveMailboxApprovalReconnectRequired(RuntimeError):
    """Raised when approval handling cannot safely continue on the selected mailbox."""


@dataclass(frozen=True)
class LiveMailboxApprovalReceiptResult:
    disposition: LiveMailboxApprovalDisposition
    path: Path | None = None
    reason: str = ""
    requires_reconnect: bool = False


def amh_mailbox_account(args: Args) -> str:
    return args.inbox_identity.split("\0", 1)[0].strip()


def amh_committed_messages_path(args: Args) -> Path:
    account = amh_mailbox_account(args)
    mailbox_id = hashlib.sha256(account.casefold().encode()).hexdigest()[:12]
    return args.state_dir / f"amh-committed-messages-{mailbox_id}.tsv"


def load_amh_committed_rows(args: Args) -> list[tuple[str, str, str]]:
    path = amh_committed_messages_path(args)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def amh_committed_operation(args: Args, *, message_id: str | None = None, uid: str | None = None) -> str | None:
    for recorded_message_id, operation_id, recorded_uid in load_amh_committed_rows(args):
        if message_id and recorded_message_id == message_id:
            return operation_id
        if uid and recorded_uid == uid:
            return operation_id
    return None


def record_amh_committed_message(args: Args, uid: str, message_id: str, operation_id: str) -> None:
    if amh_committed_operation(args, message_id=message_id) == operation_id:
        return
    path = amh_committed_messages_path(args)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    write_private_state(path, existing + f"{message_id}\t{operation_id}\t{uid}\n")


def amh_subject_candidate(subject: str) -> bool:
    return bool(amh_subject_agent_id(subject))


def amh_subject_agent_id(subject: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in subject) or subject.splitlines() != [subject]:
        return ""
    match = AMH_SUBJECT_TAG_RE.match(subject)
    if match is None:
        return ""
    if match.group(1).casefold() in RESERVED_AMH_SUBJECT_TAGS:
        return ""
    remainder = subject[match.end() :]
    if re.match(r"^\s*\[[^\]]+\]", remainder) is not None:
        return ""
    agent_id = match.group(1)
    return "main-manager" if agent_id.casefold() == "main" else agent_id


def exact_decoded_prompt(msg: Message) -> bytes:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload
        return b""
    payload = msg.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def fetch_blob(msg_data: list[object]) -> bytes:
    blob = b""
    for item in msg_data:
        if isinstance(item, tuple):
            part = item[0]
            if isinstance(part, bytes):
                blob += part
        elif isinstance(item, bytes):
            blob += item
    return blob


def gmail_internaldate_unix_ms(value: bytes) -> str | None:
    try:
        parsed = parsedate_to_datetime(value.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return str(int(parsed.timestamp() * 1000))


def parse_gmail_metadata(msg_data: list[object]) -> tuple[str | None, str | None, str | None]:
    blob = fetch_blob(msg_data)
    msgid = GMAIL_MSGID_RE.search(blob)
    thrid = GMAIL_THRID_RE.search(blob)
    internaldate = GMAIL_INTERNALDATE_RE.search(blob)
    return (
        msgid.group(1).decode() if msgid else None,
        thrid.group(1).decode() if thrid else None,
        gmail_internaldate_unix_ms(internaldate.group(1)) if internaldate else None,
    )


def fetch_gmail_metadata(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str | None, str | None] | None:
    typ, msg_data = client.uid("fetch", uid, GMAIL_METADATA_FETCH)
    if typ != "OK" or not msg_data:
        return None
    message_id, thread_id, internaldate_unix_ms = parse_gmail_metadata(msg_data)
    if not message_id:
        return None
    if not thread_id:
        return (message_id, None, internaldate_unix_ms)
    return (message_id, thread_id, internaldate_unix_ms)


def fetch_gmail_metadata_for_message_id(
    client: imaplib.IMAP4_SSL,
    message_id: str,
) -> tuple[str, str, str, str] | None:
    result: tuple[str, str, str, str] | None = None
    try:
        try:
            typ_select, _select_data = client.select('"[Gmail]/All Mail"', readonly=True)
            if typ_select == "OK":
                typ, uid_data = client.uid("search", None, "HEADER", "Message-ID", message_id)
                uid_tokens = (
                    b" ".join(item for item in uid_data if isinstance(item, bytes)).split()
                    if typ == "OK" and uid_data
                    else []
                )
                if len(uid_tokens) == 1:
                    try:
                        uid = uid_tokens[0].decode("ascii")
                    except UnicodeDecodeError:
                        uid = ""
                    if uid:
                        typ_labels, label_data = client.uid("fetch", uid, "(X-GM-LABELS)")
                        metadata = fetch_gmail_metadata(client, uid)
                        if (
                            typ_labels == "OK"
                            and GMAIL_SENT_LABEL_RE.search(fetch_blob(label_data))
                            and metadata is not None
                        ):
                            gmail_message_id, gmail_thread_id, gmail_internaldate_unix_ms = metadata
                            if (
                                uid.isdigit()
                                and gmail_message_id.isdigit()
                                and gmail_thread_id
                                and gmail_thread_id.isdigit()
                                and gmail_internaldate_unix_ms
                            ):
                                result = (
                                    uid,
                                    gmail_message_id,
                                    gmail_thread_id,
                                    gmail_internaldate_unix_ms,
                                )
        except imaplib.IMAP4.error:
            result = None
    finally:
        try:
            typ_restore, _restore_data = client.select("INBOX")
        except imaplib.IMAP4.error:
            typ_restore = "NO"
        if typ_restore != "OK":
            raise LiveMailboxApprovalReconnectRequired(
                "failed to restore INBOX after approval-request Gmail metadata lookup"
            )
    return result


def live_mailbox_approval_receipts_dir(args: Args) -> Path:
    return args.state_dir / "amh-live-mailbox-approval-receipts"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _live_mailbox_command_lines(body: str) -> list[str]:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if normalized == "":
        return []
    return normalized.split("\n")


def _live_mailbox_stage(body: str) -> tuple[str, str, str] | None:
    commands = _live_mailbox_command_lines(body)
    if len(commands) != 1:
        return None
    command = commands[0]
    if command == AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT:
        return "email1", AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_TEXT, ""
    match = AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_RE.fullmatch(command)
    if match is None:
        return None
    observed = match.group(1)
    approval_text = (
        "Approved: send Email 2 only for Gmail threading test "
        "from sichangheagent@gmail.com to stevensichanghe@gmail.com using "
        f"Email 1 Message-ID: {observed}. Approval code GT-20260821-EMAIL2."
    )
    return "email2", approval_text, observed


def _live_mailbox_approval_subject(stage: str) -> str:
    if stage == "email1":
        return AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_SUBJECT
    return AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_SUBJECT


def _live_mailbox_approval_request_message_id(stage: str) -> str:
    if stage == "email1":
        return AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_REQUEST_MESSAGE_ID
    return AMH_LIVE_MAILBOX_EMAIL2_APPROVAL_REQUEST_MESSAGE_ID


def _message_id_tokens(values: list[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(re.findall(r"<[^<>\s]+>", str(value)))
    return tokens


def _live_mailbox_reply_binds_request(msg: Message, request_message_id: str) -> bool:
    in_reply_to = _message_id_tokens([str(value) for value in msg.get_all("In-Reply-To", [])])
    references = _message_id_tokens([str(value) for value in msg.get_all("References", [])])
    return request_message_id in in_reply_to and request_message_id in references


def _live_mailbox_reply_has_exact_recipients(msg: Message) -> bool:
    to_addresses = [
        address.casefold()
        for _display, address in getaddresses([str(value) for value in msg.get_all("To", [])])
        if address
    ]
    if to_addresses != [AMH_LIVE_MAILBOX_SENDER.casefold()]:
        return False
    return not msg.get_all("Cc", []) and not msg.get_all("Bcc", [])


def _live_mailbox_approval_body_container_ok(msg: Message, expected_approval_text: str) -> bool:
    del expected_approval_text
    if msg.is_multipart():
        return False
    if msg.get_content_type() != "text/plain":
        return False
    return (
        msg.get_filename() is None
        and msg.get_content_disposition() is None
        and not msg.get_all("Content-Disposition", [])
    )


def live_mailbox_approval_thread_candidate(msg: Message) -> bool:
    subject_values = [str(value) for value in msg.get_all("Subject", [])]
    if len(subject_values) != 1:
        return False
    for stage_name in ("email1", "email2"):
        if subject_values[0] != _live_mailbox_approval_subject(stage_name):
            continue
        if _live_mailbox_reply_binds_request(
            msg,
            _live_mailbox_approval_request_message_id(stage_name),
        ):
            return True
    return False


def _live_mailbox_stage_fields(stage: str) -> dict[str, str]:
    if stage == "email1":
        return {
            "artifact": str(AMH_LIVE_MAILBOX_PACKET_ROOT / "email-1.eml"),
            "artifact_sha256": AMH_LIVE_MAILBOX_EMAIL1_ARTIFACT_SHA256,
            "body_sha256": AMH_LIVE_MAILBOX_EMAIL1_BODY_SHA256,
            "message_id": AMH_LIVE_MAILBOX_EMAIL1_MESSAGE_ID,
            "subject": AMH_LIVE_MAILBOX_EMAIL1_SUBJECT,
        }
    return {
        "artifact": str(AMH_LIVE_MAILBOX_PACKET_ROOT / "email-2.eml"),
        "artifact_sha256": AMH_LIVE_MAILBOX_EMAIL2_ARTIFACT_SHA256,
        "body_sha256": AMH_LIVE_MAILBOX_EMAIL2_BODY_SHA256,
        "message_id": AMH_LIVE_MAILBOX_EMAIL2_MESSAGE_ID,
        "subject": AMH_LIVE_MAILBOX_EMAIL2_SUBJECT,
    }


def _live_mailbox_receipt_values(receipt: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in receipt.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _live_mailbox_receipt_matches(
    values: dict[str, str],
    *,
    stage: str,
    request_message_id: str,
) -> bool:
    return (
        values.get("schema") == AMH_LIVE_MAILBOX_APPROVAL_RECEIPT_SCHEMA
        and values.get("stage") == stage
        and values.get("approval_request_message_id") == request_message_id
    )


def live_mailbox_existing_receipt(
    args: Args,
    *,
    stage: str,
    request_message_id: str,
) -> bool | None:
    receipts = live_mailbox_approval_receipts_dir(args)
    if not receipts.exists():
        return False
    for receipt in receipts.glob("*.receipt"):
        try:
            values = _live_mailbox_receipt_values(receipt)
        except (OSError, UnicodeDecodeError):
            return None
        if _live_mailbox_receipt_matches(
            values,
            stage=stage,
            request_message_id=request_message_id,
        ):
            return True if values.get("receipt_finalized") == "true" else None
    return False


def maybe_write_live_mailbox_approval_receipt(
    client: imaplib.IMAP4_SSL,
    args: Args,
    uid: str,
    msg: Message,
    raw_mime: bytes,
    txt_path: Path,
    body_text: str,
) -> Path | None:
    result = live_mailbox_approval_receipt_result(
        client,
        args,
        uid,
        msg,
        raw_mime,
        txt_path,
        body_text,
    )
    return result.path


def live_mailbox_approval_receipt_result(
    client: imaplib.IMAP4_SSL,
    args: Args,
    uid: str,
    msg: Message,
    raw_mime: bytes,
    txt_path: Path,
    body_text: str,
) -> LiveMailboxApprovalReceiptResult:
    stage = _live_mailbox_stage(body_text)
    if stage is None:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval body does not contain exactly one recognized command",
        )
    stage_name, approval_text, observed_message_id = stage
    if not _live_mailbox_approval_body_container_ok(msg, approval_text):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval MIME container is not single-part text/plain",
        )
    if not TRUST_LIVE_MAILBOX_AUTH_RESULTS:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="trusted Gmail Authentication-Results gate is disabled",
        )
    if not args.inbox_identity:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="mailbox UID identity is missing",
        )
    if args.self_email.casefold() != AMH_LIVE_MAILBOX_RECIPIENT.casefold():
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="Human sender configuration does not match pinned recipient",
        )
    if amh_mailbox_account(args).casefold() != AMH_LIVE_MAILBOX_SENDER.casefold():
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="IMAP account does not match pinned sender mailbox",
        )
    if args.mail_dir.resolve(strict=False) != AMH_LIVE_MAILBOX_APPROVAL_MAIL_DIR.resolve(strict=False):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval mail directory is not the pinned manager_mail root",
        )
    if args.state_dir.resolve(strict=False) != AMH_LIVE_MAILBOX_APPROVAL_STATE_DIR.resolve(strict=False):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval state directory is not the pinned state root",
        )
    if not exact_human_sender(msg, args.self_email, require_transport_identity=True):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="sender is not exactly authenticated Human",
        )
    if not _live_mailbox_reply_has_exact_recipients(msg):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval reply recipients are not exactly pinned",
        )
    metadata = fetch_gmail_metadata(client, uid)
    if metadata is None:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval Gmail metadata fetch failed",
        )
    gmail_message_id, gmail_thread_id, gmail_internaldate_unix_ms = metadata
    if (
        not uid.isdigit()
        or not gmail_thread_id
        or not gmail_thread_id.isdigit()
        or not gmail_internaldate_unix_ms
    ):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval Gmail metadata is incomplete",
        )
    subject_values = [str(value) for value in msg.get_all("Subject", [])]
    if subject_values != [_live_mailbox_approval_subject(stage_name)]:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval subject is not pinned",
        )
    request_message_id = _live_mailbox_approval_request_message_id(stage_name)
    if not _live_mailbox_reply_binds_request(msg, request_message_id):
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval reply is not bound to the pinned request",
        )
    existing_receipt = live_mailbox_existing_receipt(
        args,
        stage=stage_name,
        request_message_id=request_message_id,
    )
    if existing_receipt is None:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval receipt scan failed",
        )
    if existing_receipt:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval receipt already exists",
        )
    now_ms = int(time.time() * 1000)
    if stage_name == "email1":
        if now_ms >= AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_DEADLINE_UNIX_MS:
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.HARD_REJECT,
                reason="approval was processed after the deadline",
            )
        if int(gmail_internaldate_unix_ms) >= AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_DEADLINE_UNIX_MS:
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.HARD_REJECT,
                reason="approval arrived after the deadline",
            )
    try:
        request_metadata = fetch_gmail_metadata_for_message_id(client, request_message_id)
    except LiveMailboxApprovalReconnectRequired as exc:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason=str(exc),
            requires_reconnect=True,
        )
    if request_metadata is None:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval-request Gmail metadata fetch failed",
        )
    (
        request_uid,
        request_gmail_message_id,
        request_gmail_thread_id,
        request_gmail_internaldate_unix_ms,
    ) = request_metadata
    if request_gmail_thread_id != gmail_thread_id:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval reply is not in the approval-request Gmail thread",
        )
    stage_fields = _live_mailbox_stage_fields(stage_name)
    try:
        source_sha256 = hashlib.sha256(txt_path.read_bytes()).hexdigest()
    except OSError:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval source file read failed",
        )
    raw_mime_sha256 = hashlib.sha256(raw_mime).hexdigest()
    body_sha256 = _sha256_text(body_text)
    lines = [
        f"schema={AMH_LIVE_MAILBOX_APPROVAL_RECEIPT_SCHEMA}",
        "source=external-email-watcher",
        f"stage={stage_name}",
        f"mail_from={AMH_LIVE_MAILBOX_SENDER}",
        f"rcpt_to={AMH_LIVE_MAILBOX_RECIPIENT}",
        f"artifact={stage_fields['artifact']}",
        f"artifact_sha256={stage_fields['artifact_sha256']}",
        f"body_sha256={stage_fields['body_sha256']}",
        f"message_id={stage_fields['message_id']}",
        "command_boundary=amh-live-mailbox-dispatch",
        f"approval_source={txt_path}",
        f"approval_source_sha256={source_sha256}",
        f"approval_text_sha256={_sha256_text(approval_text)}",
        f"approval_subject={subject_values[0]}",
        f"approval_request_message_id={request_message_id}",
        f"approval_request_gmail_uid={request_uid}",
        f"approval_request_gmail_message_id={request_gmail_message_id}",
        f"approval_request_gmail_thread_id={request_gmail_thread_id}",
        f"approval_request_gmail_internaldate_unix_ms={request_gmail_internaldate_unix_ms}",
        f"approval_in_reply_to={request_message_id}",
        f"approval_references_contains={request_message_id}",
        f"approval_body_sha256={body_sha256}",
        f"approval_deadline_unix_ms={AMH_LIVE_MAILBOX_EMAIL1_APPROVAL_DEADLINE_UNIX_MS if stage_name == 'email1' else ''}",
        f"human_sender={AMH_LIVE_MAILBOX_RECIPIENT}",
        f"imap_account={amh_mailbox_account(args)}",
        "authenticated_sender=true",
        f"provider_authentication={AMH_LIVE_MAILBOX_PROVIDER_AUTHENTICATION}",
        f"gmail_uid={uid}",
        f"gmail_message_id={gmail_message_id}",
        f"gmail_thread_id={gmail_thread_id}",
        f"gmail_internaldate_unix_ms={gmail_internaldate_unix_ms}",
        f"raw_mime_sha256={raw_mime_sha256}",
        f"approval_created_unix_ms={now_ms}",
        f"expires_unix_ms={now_ms + AMH_LIVE_MAILBOX_RECEIPT_TTL_MS}",
        "receipt_finalized=true",
    ]
    if stage_name == "email2":
        lines.extend(
            [
                f"observed_email1_message_id={observed_message_id}",
                f"expected_amh_caller={AMH_LIVE_MAILBOX_WORKER_CALLER}",
            ]
        )
    payload = "\n".join(lines) + "\n"
    receipts = live_mailbox_approval_receipts_dir(args)
    try:
        receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipts.chmod(0o700)
    except OSError:
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval receipt directory setup failed",
        )
    receipt_id = hashlib.sha256(
        f"{args.inbox_identity}\0{stage_name}\0{request_message_id}".encode()
    ).hexdigest()
    path = receipts / f"{receipt_id}.receipt"
    tmp_path = receipts / f".{receipt_id}.{os.getpid()}.tmp"
    if path.exists():
        try:
            existing_values = _live_mailbox_receipt_values(path)
        except (OSError, UnicodeDecodeError):
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.RETRY,
                reason="approval receipt path read failed",
            )
        if _live_mailbox_receipt_matches(
            existing_values,
            stage=stage_name,
            request_message_id=request_message_id,
        ):
            if existing_values.get("receipt_finalized") == "true":
                return LiveMailboxApprovalReceiptResult(
                    LiveMailboxApprovalDisposition.HARD_REJECT,
                    reason="approval receipt path already exists",
                )
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.RETRY,
                reason="approval receipt path is unfinalized",
            )
        else:
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.RETRY,
                reason="approval receipt path is occupied",
            )
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            tmp_path.unlink(missing_ok=True)
        except (OSError, UnicodeDecodeError):
            pass
        if not path.exists():
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.RETRY,
                reason="approval receipt temporary path already exists",
            )
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.HARD_REJECT,
            reason="approval receipt path already exists",
        )
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd != -1:
                os.close(fd)
        fsync_directory(receipts)
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            try:
                existing_values = _live_mailbox_receipt_values(path)
            except (OSError, UnicodeDecodeError):
                return LiveMailboxApprovalReceiptResult(
                    LiveMailboxApprovalDisposition.RETRY,
                    reason="approval receipt path read failed",
                )
            if _live_mailbox_receipt_matches(
                existing_values,
                stage=stage_name,
                request_message_id=request_message_id,
            ) and existing_values.get("receipt_finalized") == "true":
                return LiveMailboxApprovalReceiptResult(
                    LiveMailboxApprovalDisposition.HARD_REJECT,
                    reason="approval receipt path already exists",
                )
            return LiveMailboxApprovalReceiptResult(
                LiveMailboxApprovalDisposition.RETRY,
                reason="approval receipt path is occupied",
            )
        fsync_directory(receipts)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except (OSError, UnicodeDecodeError):
            pass
        try:
            if path.exists():
                values = _live_mailbox_receipt_values(path)
                if _live_mailbox_receipt_matches(
                    values,
                    stage=stage_name,
                    request_message_id=request_message_id,
                ):
                    path.unlink(missing_ok=True)
                    fsync_directory(receipts)
        except OSError:
            pass
        return LiveMailboxApprovalReceiptResult(
            LiveMailboxApprovalDisposition.RETRY,
            reason="approval receipt write failed",
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return LiveMailboxApprovalReceiptResult(
        LiveMailboxApprovalDisposition.STORED,
        path=path,
    )


def load_amh_bridge_modules() -> tuple[object, object]:
    root = os.fspath(DEFAULT_AMH_BRIDGE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from tools import amh_email_ingress, amh_email_watcher_bridge

    return amh_email_watcher_bridge, amh_email_ingress


def amh_agent_status_is_supported(agent_id: str) -> bool:
    if not agent_id or AMH_AGENT_ID_RE.fullmatch(agent_id) is None:
        return False
    executable = DEFAULT_AMH_EXECUTABLE
    if not executable.is_file():
        return False
    runtime_root = DEFAULT_AMH_RUNTIME_ROOT if DEFAULT_AMH_RUNTIME_ROOT.is_absolute() else DEFAULT_AMH_RUNTIME_ROOT.resolve()
    result = subprocess.run(
        [os.fspath(executable), "--runtime-root", os.fspath(runtime_root), "agent", "status", agent_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


def launch_amh_worker_for_route(args: Args, operation_id: str) -> None:
    launcher = Path(__file__).with_name("omo_amh_route_launch.py")
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(launcher),
            "--root",
            os.fspath(args.root),
            "--state-dir",
            os.fspath(args.state_dir),
            "--amh-executable",
            os.fspath(DEFAULT_AMH_EXECUTABLE),
            "--amh-runtime-root",
            os.fspath(DEFAULT_AMH_RUNTIME_ROOT),
            "--operation-id",
            operation_id,
            "--manager-target",
            args.manager_target,
            "--workdir",
            os.fspath(DEFAULT_AMH_WORKDIR),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "AMH route launch failed")


def try_route_amh_message(client: imaplib.IMAP4_SSL, args: Args, uid: str, msg: Message, raw_mime: bytes) -> AmhRouteDisposition:
    subject = str(msg.get("Subject", ""))
    subject_agent_id = amh_subject_agent_id(subject)
    if not subject_agent_id:
        return AmhRouteDisposition.FALLBACK
    metadata = fetch_gmail_metadata(client, uid)
    committed_by_uid = amh_committed_operation(args, uid=uid)
    if metadata is None:
        return AmhRouteDisposition.HOLD if committed_by_uid else AmhRouteDisposition.SKIP
    message_id, thread_id, _internaldate_unix_ms = metadata
    committed = amh_committed_operation(args, message_id=message_id) or committed_by_uid
    if not thread_id:
        return AmhRouteDisposition.HOLD if committed else AmhRouteDisposition.FALLBACK
    if not args.manager_target:
        return AmhRouteDisposition.HOLD if committed else AmhRouteDisposition.FALLBACK
    loaded = load_amh_bridge_modules()
    if not loaded:
        return AmhRouteDisposition.HOLD if committed else AmhRouteDisposition.SKIP
    bridge, ingress = loaded
    decision = bridge.route_from_watcher_subject(subject, enabled_agent_ids=frozenset({subject_agent_id}))
    if not decision.routes_to_amh:
        return AmhRouteDisposition.HOLD if committed else AmhRouteDisposition.FALLBACK
    agent_id = getattr(decision.route, "agent_id", None)
    if agent_id is not None and not amh_agent_status_is_supported(agent_id):
        return AmhRouteDisposition.HOLD if committed else AmhRouteDisposition.FALLBACK
    identity = ingress.ProviderMessageIdentity(
        provider="gmail",
        account_id=amh_mailbox_account(args) or args.self_email,
        message_id=message_id,
        thread_id=thread_id,
    )
    ids = ingress.derive_replay_ids(identity)
    runtime_root = DEFAULT_AMH_RUNTIME_ROOT if DEFAULT_AMH_RUNTIME_ROOT.is_absolute() else DEFAULT_AMH_RUNTIME_ROOT.resolve()
    staging_directory = (args.state_dir / "amh-email-staging").resolve()
    staging_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    message = bridge.WatcherMessage(
        identity=identity,
        sender_identity=parseaddr(str(msg.get("From", "")))[1] or str(msg.get("From", "")),
        configured_human_sender=args.self_email,
        route=decision.route,
        raw_mime=raw_mime,
        decoded_prompt=exact_decoded_prompt(msg),
        decoded_subject=subject.encode("utf-8"),
        amh_runtime_root=runtime_root,
    )
    config = bridge.BridgeConfig(
        amh_executable=DEFAULT_AMH_EXECUTABLE if DEFAULT_AMH_EXECUTABLE.is_absolute() else DEFAULT_AMH_EXECUTABLE.resolve(),
        staging_directory=staging_directory,
    )

    def persist_cursor() -> None:
        record_amh_committed_message(args, uid, message_id, ids.operation_id)
        launch_amh_worker_for_route(args, ids.operation_id)

    def mark_seen_callback() -> bool:
        return mark_seen(client, uid)

    outcome = bridge.bridge_watcher_message(
        message,
        config=config,
        persist_cursor=persist_cursor,
        mark_seen=mark_seen_callback,
        ownership=bridge.SideBySideOwner.AMH,
    )
    if isinstance(outcome, bridge.MailboxAdvanced):
        return AmhRouteDisposition.ADVANCED
    return AmhRouteDisposition.HOLD


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


def fetch_message(client: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    typ_msg, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
    if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
        logging.error("email fetch failed: uid=%s typ=%s", uid, typ_msg)
        return None
    return BytesParser(policy=policy.default).parsebytes(msg_data[0][1])


def sender_display_name(sender: str) -> str:
    name, _address = parseaddr(sender)
    return name.strip().lower()


def manager_authored_message(msg: Message, self_email: str) -> bool:
    sender = str(msg.get("From", ""))
    if not from_self(sender, self_email):
        return False
    if sender_display_name(sender) == "human":
        return False
    return has_agent_footer(message_text(msg))


def stored_mail_has_agent_footer(path: Path) -> bool:
    try:
        return has_agent_footer(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def mark_seen_after_human_intake(client: imaplib.IMAP4_SSL, uid: str, args: Args, msg: Message | None = None, txt_path: Path | None = None) -> bool:
    candidate = msg
    if candidate is None and txt_path is not None and txt_path.exists() and stored_mail_has_agent_footer(txt_path):
        candidate = fetch_message(client, uid)
        if candidate is None:
            return False
    if candidate is not None and manager_authored_message(candidate, args.self_email):
        logging.info("email mark read skipped for manager-authored mail: uid=%s", uid)
        return False
    return mark_seen(client, uid)


def live_mailbox_approval_processed_uids_path(args: Args) -> Path:
    return email_uid_state_path(args, "amh-live-mailbox-approval-processed-uids")


def handle_live_mailbox_approval_replies(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    processed_path = live_mailbox_approval_processed_uids_path(args)
    processed_uids = load_processed_uids(processed_path)
    processed_changed = False
    handled = False
    subject = _live_mailbox_approval_subject(args.live_mailbox_stage)
    request_message_id = _live_mailbox_approval_request_message_id(args.live_mailbox_stage)
    candidate_uids = search_live_mailbox_approval_uids(
        client,
        sender_email=args.self_email,
        subject=subject,
        request_message_id=request_message_id,
        processed_uids=processed_uids,
    )
    if not candidate_uids:
        logging.info(
            "email AMH live mailbox approval scan complete: n=0 stage=%s subject=%r",
            args.live_mailbox_stage,
            subject,
        )
        return False
    logging.info(
        "email AMH live mailbox approval candidates found: n=%s uids=%s stage=%s subject=%r",
        len(candidate_uids),
        ",".join(uid.decode() for uid in sorted(candidate_uids, key=lambda value: int(value))),
        args.live_mailbox_stage,
        subject,
    )
    for raw_uid in sorted(candidate_uids, key=lambda value: int(value)):
        uid = raw_uid.decode()
        if uid in processed_uids:
            continue
        typ_msg, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            logging.error("email AMH live mailbox approval fetch failed: uid=%s typ=%s", uid, typ_msg)
            continue
        raw_mime = msg_data[0][1]
        if not isinstance(raw_mime, bytes):
            logging.error("email AMH live mailbox approval fetch returned non-bytes MIME: uid=%s", uid)
            continue
        msg = BytesParser(policy=policy.default).parsebytes(raw_mime)
        sender = str(msg.get("From", ""))
        if not exact_human_sender(msg, args.self_email, require_transport_identity=bool(args.inbox_identity)):
            logging.warning(
                "email AMH live mailbox approval candidate rejected after fetch: uid=%s from_self=%s",
                uid,
                from_self(sender, args.self_email),
            )
            if mark_seen_after_human_intake(client, uid, args, msg):
                processed_uids.add(uid)
                processed_changed = True
                handled = True
            continue
        body_text = message_text(msg)
        txt_path = write_mail(args, uid, msg, sender, str(msg.get("Subject", "")))
        receipt_result = live_mailbox_approval_receipt_result(
            client,
            args,
            uid,
            msg,
            raw_mime,
            txt_path,
            body_text,
        )
        if receipt_result.disposition is LiveMailboxApprovalDisposition.RETRY:
            logging.warning(
                "email AMH live mailbox approval candidate left unread for retry: uid=%s reason=%s",
                uid,
                receipt_result.reason,
            )
            if receipt_result.requires_reconnect:
                raise LiveMailboxApprovalReconnectRequired(receipt_result.reason)
            continue
        if receipt_result.path is not None:
            logging.info(
                "email AMH live mailbox approval receipt stored: uid=%s path=%s",
                uid,
                receipt_result.path,
            )
        else:
            logging.warning(
                "email AMH live mailbox approval candidate hard-rejected: uid=%s reason=%s",
                uid,
                receipt_result.reason,
            )
        if mark_seen_after_human_intake(client, uid, args, msg, txt_path):
            processed_uids.add(uid)
            processed_changed = True
            handled = True
    if processed_changed:
        save_processed_uids(processed_path, processed_uids)
    return handled or processed_changed


def handle_unseen(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    processed_path = processed_uids_path(args)
    processed_uids = load_processed_uids(processed_path)
    ignored_path = ignored_uids_path(args)
    ignored_uids = load_processed_uids(ignored_path)
    unaccepted_path = unaccepted_pending_uids_path(args)
    unaccepted_pending_uids = load_processed_uids(unaccepted_path)
    processed_changed = False
    ignored_changed = False
    unaccepted_changed = False
    handled = False
    manager_file = current_manager_file(args)
    candidate_uids: set[bytes] = set()
    candidate_uids.update(search_sender_uids(client, args.self_email, processed_uids))
    if unaccepted_pending_uids:
        candidate_uids.update(search_processed_sender_uids(client, args.self_email, sorted(unaccepted_pending_uids, key=lambda value: int(value))))
    if not candidate_uids:
        logging.info("email scan complete: n=0 processed_next=%s manager_file=%s", uid_search_range(processed_uids), manager_file)
        return maybe_handle_manager_mail_thresholds(client, args)
    logging.info("email candidates found: n=%s uids=%s processed_max=%s manager_file=%s", len(candidate_uids), ",".join(uid.decode() for uid in sorted(candidate_uids, key=lambda value: int(value))), uid_search_range(processed_uids), manager_file)
    for raw_uid in sorted(candidate_uids, key=lambda value: int(value)):
        uid = raw_uid.decode()
        if uid in ignored_uids:
            continue
        expected_txt_path = args.mail_dir / mail_artifact_name(args, uid)
        pending_ref = existing_source_pending_path_line_in_root(args.root, expected_txt_path, manager_file) if uid in unaccepted_pending_uids else None
        if pending_ref is not None:
            pending_file, pending_line = pending_ref
            if pending_watcher_delivery_present(args.root, pending_file, pending_line) or push_email_ref(
                args_for_manager_file(args, pending_file, pending_line), pending_line
            ):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                if mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path):
                    processed_uids.add(uid)
                    processed_changed = True
                    handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
            continue
        if uid in unaccepted_pending_uids and (
            existing_source_line_in_root(args.root, expected_txt_path, manager_file) is not None
            or existing_consumed_source_line(args.root, expected_txt_path, manager_file) is not None
        ):
            logging.info("email unaccepted uid already has consumed source; accepting: uid=%s root=%s", uid, args.root)
            unaccepted_pending_uids.discard(uid)
            unaccepted_changed = True
            if mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path):
                processed_uids.add(uid)
                processed_changed = True
                handled = True
            continue
        if uid in processed_uids:
            if uid not in unaccepted_pending_uids:
                logging.info("email processed uid remains authoritative without an active source marker: uid=%s root=%s", uid, args.root)
                handled = mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path) or handled
                continue
            existing_source_line = existing_source_line_in_root(args.root, expected_txt_path, manager_file)
            if uid in unaccepted_pending_uids and existing_source_line is not None:
                logging.warning("email unaccepted processed uid has source without pending; reprocessing: uid=%s root=%s", uid, args.root)
            elif uid in unaccepted_pending_uids:
                logging.warning("email unaccepted processed uid lacks source; reprocessing: uid=%s root=%s", uid, args.root)
        existing_pending = existing_source_pending_path_line_in_root(args.root, expected_txt_path, manager_file)
        if existing_pending is not None:
            pending_file, existing_pending_line = existing_pending
            if pending_watcher_delivery_present(args.root, pending_file, existing_pending_line) or push_email_ref(
                args_for_manager_file(args, pending_file, existing_pending_line), existing_pending_line
            ):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                if mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path):
                    processed_uids.add(uid)
                    processed_changed = True
                    handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
            continue
        if existing_consumed_source_line(args.root, expected_txt_path, manager_file) is not None:
            logging.info("email uid already has acknowledged or routed source; accepting without duplicate pending: uid=%s root=%s", uid, args.root)
            unaccepted_pending_uids.discard(uid)
            unaccepted_changed = True
            if mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path):
                processed_uids.add(uid)
                processed_changed = True
                handled = True
            continue
        typ_msg, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            logging.error("email fetch failed: uid=%s typ=%s", uid, typ_msg)
            continue
        msg = BytesParser(policy=policy.default).parsebytes(msg_data[0][1])
        sender = str(msg.get("From", ""))
        subject = str(msg.get("Subject", ""))
        sender_accepted = exact_human_sender(msg, args.self_email, require_transport_identity=bool(args.inbox_identity))
        if not sender_accepted:
            logging.warning("email candidate rejected after fetch: uid=%s subject=%r from_self=%s", uid, subject, from_self(sender, args.self_email))
            continue
        if manager_authored_message(msg, args.self_email):
            logging.info("email candidate ignored as manager-authored echo: uid=%s subject=%r", uid, subject)
            ignored_uids.add(uid)
            ignored_changed = True
            continue
        raw_mime = msg_data[0][1]
        body_text = message_text(msg)
        amh_result = try_route_amh_message(client, args, uid, msg, raw_mime if isinstance(raw_mime, bytes) else b"")
        if amh_result is AmhRouteDisposition.HOLD:
            logging.info("email AMH route held for replay: uid=%s subject=%r", uid, subject)
            handled = True
            continue
        if amh_result is AmhRouteDisposition.ADVANCED:
            logging.info("email AMH route advanced: uid=%s subject=%r", uid, subject)
            unaccepted_pending_uids.discard(uid)
            unaccepted_changed = True
            processed_uids.add(uid)
            processed_changed = True
            handled = True
            continue
        if amh_result is AmhRouteDisposition.SKIP:
            logging.warning("email AMH metadata unavailable; leaving unread: uid=%s subject=%r", uid, subject)
            continue
        txt_path = write_mail(args, uid, msg, sender, subject)
        logging.info("email stored: uid=%s path=%s subject=%r", uid, source_ref(args.root, txt_path), subject)
        if is_recovery_subject(subject):
            if recovery_sender_authenticated(msg, args.self_email):
                handle_recovery_email(args, uid, txt_path)
            else:
                append_recovery_record(args.root, txt_path, "recovery email recorded; restart refused because sender authentication did not pass", manager_file)
            if mark_seen_after_human_intake(client, uid, args, msg):
                processed_uids.add(uid)
                processed_changed = True
                handled = True
        else:
            route = email_route(args, subject, body_text)
            route_args = replace(args, manager_file=route.manager_file, manager_target=route.manager_target)
            pending_line = append_pending(
                args.root,
                txt_path,
                route.manager_file,
            )
            if route.pending_watcher_delivery or push_email_ref(route_args, pending_line):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                if mark_seen_after_human_intake(client, uid, args, msg):
                    processed_uids.add(uid)
                    processed_changed = True
                    handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
    if processed_changed:
        save_processed_uids(processed_path, processed_uids)
    if ignored_changed:
        save_processed_uids(ignored_path, ignored_uids)
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


def watch_inbox(client: imaplib.IMAP4_SSL, args: Args, threshold_check: Callable[[], bool] | None = None) -> int:
    activity = handle_unseen(client, args)
    if threshold_check is not None:
        activity = threshold_check() or activity
    now_s = time.monotonic()
    last_activity_s = now_s
    next_pull_s = now_s + args.pull_interval_s if args.pull_interval_s > 0 else float("inf")
    threshold_interval_s = min(max(args.idle_wait_s, 1.0), 60.0)
    next_threshold_s = now_s + threshold_interval_s if threshold_check is not None else float("inf")
    if activity:
        logging.info("email watcher startup scan found candidate mail")
    while True:
        now_s = time.monotonic()
        if args.idle_exit_after_s > 0 and now_s - last_activity_s >= args.idle_exit_after_s:
            logging.info("email watcher exiting after idle refresh window: idle_s=%.1f", now_s - last_activity_s)
            return 0
        if now_s >= next_threshold_s:
            if threshold_check is not None and threshold_check():
                last_activity_s = time.monotonic()
            next_threshold_s = time.monotonic() + threshold_interval_s
            continue
        if now_s >= next_pull_s:
            client.select("INBOX")
            if handle_unseen(client, args):
                last_activity_s = time.monotonic()
            next_pull_s = time.monotonic() + args.pull_interval_s
            continue
        wait_s = args.idle_wait_s
        if args.pull_interval_s > 0:
            wait_s = min(wait_s, max(0.0, next_pull_s - now_s))
        wait_s = min(wait_s, max(0.0, next_threshold_s - now_s))
        if idle_once(client, wait_s):
            client.select("INBOX")
            if handle_unseen(client, args):
                last_activity_s = time.monotonic()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        split_settings = configured_agent_mail()
    except ValueError as exc:
        print(f"email_idle_watcher: {exc}", file=sys.stderr)
        return 1
    config = (
        {"host": GMAIL_IMAP_HOST, "user": split_settings.agent_address, "password": split_settings.app_password}
        if split_settings is not None
        else parse_env_config(CONFIG_PATH)
    )
    missing = {"host", "user", "password"} - set(config)
    if missing:
        print(f"email_idle_watcher: missing config keys {sorted(missing)} in {CONFIG_PATH}", file=sys.stderr)
        return 1
    accepted_human = split_settings.human_address if split_settings is not None else config["user"]
    safe_args = Args(args.root, args.manager_url, args.mail_dir, args.state_dir, args.manager_file, args.once, accepted_human, args.recovery_debounce_s, args.restart_script, args.idle_wait_s, args.manager_target, args.imap_timeout_s, args.pull_interval_s, args.idle_exit_after_s, args.unread_compression_threshold, args.recent_cleanup_threshold, args.recent_cleanup_window_s, mail_thresholds=split_settings is None, inbox_identity=config["user"] if split_settings is not None else "", live_mailbox_approval_only=args.live_mailbox_approval_only, live_mailbox_stage=args.live_mailbox_stage)
    logging.info("email watcher starting: root=%s mail_dir=%s state_dir=%s manager_target=%s manager_url=%s idle_wait_s=%s imap_timeout_s=%s pull_interval_s=%s idle_exit_after_s=%s unread_compression_threshold=%s recent_cleanup_threshold=%s recent_cleanup_window_s=%s", safe_args.root, safe_args.mail_dir, safe_args.state_dir, safe_args.manager_target or "unset", safe_args.manager_url or "unset", safe_args.idle_wait_s, safe_args.imap_timeout_s, safe_args.pull_interval_s, safe_args.idle_exit_after_s, safe_args.unread_compression_threshold, safe_args.recent_cleanup_threshold, int(safe_args.recent_cleanup_window_s))
    while True:
        try:
            with imaplib.IMAP4_SSL(config["host"], timeout=safe_args.imap_timeout_s) as client:
                client.login(config["user"], config["password"])
                client.select("INBOX")
                runtime_args = replace(safe_args, inbox_identity=mailbox_state_identity(client, config["user"])) if split_settings is not None else safe_args
                logging.info("email watcher connected and selected INBOX")
                threshold_check = (lambda: maybe_handle_split_manager_mail_thresholds(runtime_args, split_settings)) if split_settings is not None else None
                if runtime_args.live_mailbox_approval_only:
                    handle_live_mailbox_approval_replies(client, runtime_args)
                    wait_email_pushes()
                    return 0
                if runtime_args.once:
                    handle_unseen(client, runtime_args)
                    if threshold_check is not None:
                        threshold_check()
                    wait_email_pushes()
                    return 0
                result = watch_inbox(client, runtime_args, threshold_check)
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
