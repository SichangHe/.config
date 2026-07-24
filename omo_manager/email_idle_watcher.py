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
from pathlib import Path
from urllib.parse import urlparse

try:
    from .omo_email_subject import subject_base
    from .omo_agent_status import TaskFrontmatterError, parse_task_metadata
    from .omo_pending_watch import text_marks_dm, text_marks_dm_only
    from .omo_tmux_send import CodexSendOptions, DEFAULT_TMUX_ENTER_COUNT, require_sendable_codex_target, send_to_codex
except ImportError:
    try:
        from omo_email_subject import subject_base
        from omo_agent_status import TaskFrontmatterError, parse_task_metadata
        from omo_pending_watch import text_marks_dm, text_marks_dm_only
        from omo_tmux_send import CodexSendOptions, DEFAULT_TMUX_ENTER_COUNT, require_sendable_codex_target, send_to_codex
    except ImportError:
        subject_base = None
        TaskFrontmatterError = ValueError

        def parse_task_metadata(_text: str) -> object:
            return None

        def text_marks_dm(_text: str) -> bool:
            return False

        def text_marks_dm_only(_text: str) -> bool:
            return False


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
MANAGER_EMAIL_HEADER = "X-OMO-Manager-Email"
MANAGER_REPLY_SEARCH_PREFIXES = (MANAGER_REPLY_PREFIX, "Re:[a]", "Re: [omo_manager]", "Re:[omo_manager]")
NORMAL_REPLY_SEARCH_PREFIXES = MANAGER_REPLY_SEARCH_PREFIXES
MANAGER_REPLY_SUBJECT_RE = re.compile(r"^re:\s*(?:\[a\]|\[omo_manager\])\s*", re.IGNORECASE)
MANAGER_TARGET_SUBJECT_RE = re.compile(r"^(?:re:\s*)*(?:\[a\]|\[omo_manager\])\s+(?:\[([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\]|([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?))(?:\s+|$)", re.IGNORECASE)
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
PWD_FOOTER_RE = re.compile(r"(?:^|\n)PWD: [^\n]+\n?\Z")
TMUX_FOOTER_RE = re.compile(r"(?:^|\n)tmux: [^\r\n]+\r?\n?\Z", re.IGNORECASE)
RECOVERY_SUBJECTS = {"[omo_manager_recover]", "Re: [omo_manager_recover]"}
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
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
DEFAULT_RECOVERY_DEBOUNCE_S = int(os.environ.get("OMO_MANAGER_RECOVERY_DEBOUNCE_S", "900"))
DEFAULT_IDLE_WAIT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_WAIT_S", "60"))
DEFAULT_IDLE_RESPONSE_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_RESPONSE_TIMEOUT_S", "10"))
DEFAULT_IMAP_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_EMAIL_IMAP_TIMEOUT_S", str(max(90.0, DEFAULT_IDLE_WAIT_S + 30.0))))
DEFAULT_PULL_INTERVAL_S = float(os.environ.get("OMO_MANAGER_EMAIL_PULL_INTERVAL_S", "600"))
DEFAULT_IDLE_EXIT_AFTER_S = float(os.environ.get("OMO_MANAGER_EMAIL_IDLE_EXIT_AFTER_S", "3600"))
DEFAULT_PROCESSED_RECOVERY_UID_WINDOW = int(os.environ.get("OMO_MANAGER_EMAIL_PROCESSED_RECOVERY_UID_WINDOW", "256"))
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
    routed_target: str = ""
    direct_delivery: bool = False


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


def runat_targets(text: str) -> list[str]:
    try:
        metadata = parse_task_metadata(text)
    except TaskFrontmatterError:
        return []
    if metadata is not None:
        return [metadata.runat]
    return []


def managerat_target(text: str) -> str:
    try:
        metadata = parse_task_metadata(text)
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
        return EmailRoute(current_manager_file(args), args.manager_target, args.manager_target)
    owner_file = current_task_file_for_target(args.root, owner_target)
    if owner_file is None:
        return None
    return EmailRoute(owner_file, owner_target, owner_target)


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
        metadata = parse_task_metadata(manager_text) if manager_text else None
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
            targets = runat_targets(path.read_text(encoding="utf-8"))
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
            targets = runat_targets(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if any(target in aliases for target in targets):
            matches.append(path)
    return matches


def email_route(args: Args, subject: str, body: str = "") -> EmailRoute:
    direct_worker_reply = text_marks_dm(body) or text_marks_dm_only(body)
    tmux_target = subject_manager_target(subject)
    if not tmux_target:
        return EmailRoute(current_manager_file(args), args.manager_target)
    manager_file = current_task_file_for_target(args.root, tmux_target)
    if manager_file is None:
        for inactive_file in inactive_task_files_for_target(args.root, tmux_target):
            try:
                inactive_owner_target = managerat_target(inactive_file.read_text(encoding="utf-8"))
            except OSError:
                inactive_owner_target = ""
            owner_route = current_route_for_owner(args, inactive_owner_target)
            if owner_route is not None:
                return owner_route
        logging.warning("sub-manager email target did not map to a task file; using default manager: target=%s", tmux_target)
        return EmailRoute(current_manager_file(args), args.manager_target)
    try:
        manager_text = manager_file.read_text(encoding="utf-8")
    except OSError:
        manager_text = ""
    try:
        metadata = parse_task_metadata(manager_text) if manager_text else None
    except TaskFrontmatterError:
        metadata = None
    if metadata is not None and metadata.is_manager:
        return EmailRoute(manager_file, fallback_manager_target_for_file(args, manager_file, tmux_target), tmux_target)
    if metadata is not None and direct_worker_reply:
        return EmailRoute(manager_file, metadata.runat, direct_delivery=True)
    owner_target = metadata.managerat if metadata is not None else ""
    if owner_target and owner_target not in target_aliases(tmux_target):
        owner_route = current_route_for_owner(args, owner_target)
        if owner_route is None:
            logging.warning("managerat target did not map to a task file; using default manager: task=%s managerat=%s", manager_file, owner_target)
            return EmailRoute(current_manager_file(args), args.manager_target)
        return owner_route
    return EmailRoute(manager_file, fallback_manager_target_for_file(args, manager_file, tmux_target), tmux_target)


def manager_target_for_file(args: Args, manager_file: Path) -> str:
    current = current_manager_file(args)
    if manager_file == current or manager_file.name.startswith("work_manager_"):
        return args.manager_target
    try:
        text = manager_file.read_text(encoding="utf-8")
    except OSError:
        return args.manager_target
    try:
        metadata = parse_task_metadata(text)
    except TaskFrontmatterError:
        metadata = None
    if metadata is not None:
        return metadata.runat if metadata.is_manager else metadata.managerat
    return args.manager_target


def routed_target_for_pending_line(path: Path, line_no: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines[line_no:]:
        stripped = line.strip()
        if stripped == "(pending)":
            return ""
        if stripped.startswith("(manager routed: ") and stripped.endswith(")"):
            target = stripped[len("(manager routed: ") : -1].strip()
            return target if TMUX_TARGET_RE.fullmatch(target) else ""
    return ""


def args_for_manager_file(args: Args, manager_file: Path, pending_line: int = 0) -> Args:
    routed_target = routed_target_for_pending_line(manager_file, pending_line) if pending_line > 0 else ""
    manager_target = fallback_manager_target_for_file(args, manager_file, routed_target or manager_target_for_file(args, manager_file))
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
        return txt_path


def processed_uids_path(args: Args) -> Path:
    return args.state_dir / "email-processed-uids.tsv"


def ignored_uids_path(args: Args) -> Path:
    return args.state_dir / "email-ignored-uids.tsv"


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


def retryable_routed_line(stripped: str) -> bool:
    if not stripped.startswith("(manager routed: ") or not stripped.endswith(")"):
        return False
    target = stripped[len("(manager routed: ") : -1].strip()
    return bool(TMUX_TARGET_RE.fullmatch(target))


def source_marker_consumed_by_routed_prose(lines: list[str], source_idx: int) -> bool:
    routed_prose_seen = False
    for prior_idx in range(source_idx - 1, -1, -1):
        stripped = lines[prior_idx].strip()
        if not stripped:
            continue
        if stripped.startswith(ROUTED_PREFIXES):
            if retryable_routed_line(stripped):
                continue
            routed_prose_seen = True
            continue
        if stripped == "(pending)":
            return routed_prose_seen
        if stripped.startswith(("(", "#")):
            return False
    return False


def source_marker_consumed_by_direct_dm_only(root: Path, txt_path: Path, task_path: Path, lines: list[str], source_idx: int) -> bool:
    try:
        text = txt_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text_marks_dm_only(text):
        return False
    try:
        metadata = parse_task_metadata("\n".join(lines) + "\n") if lines else None
    except TaskFrontmatterError:
        return False
    if metadata is None or metadata.is_manager:
        return False
    for prior_idx in range(source_idx - 1, -1, -1):
        stripped = lines[prior_idx].strip()
        if not stripped:
            continue
        if stripped == "(pending)":
            return False
        if stripped.startswith(("(", "#")):
            break
    return task_path.exists()


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
            if stripped in source_lines and source_marker_consumed_by_direct_dm_only(root, txt_path, path, lines, idx):
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
            if retryable_routed_line(stripped):
                continue
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
    routed_target: str = "",
    *,
    dm_only: bool = False,
) -> int:
    manager_file = manager_file or dated_manager_file(root)
    existing_line = existing_source_pending_line(root, txt_path, manager_file)
    if existing_line is not None:
        return existing_line
    consumed_line = existing_consumed_source_line(root, txt_path, manager_file)
    if consumed_line is not None:
        return consumed_line
    lines = manager_file.read_text(encoding="utf-8").splitlines() if manager_file.exists() else []
    line_no = len(lines) + 1
    from_line = email_source_lines(root, txt_path)[0]
    block = ["", "(pending)"]
    if dm_only:
        block.append("DM only")
    if routed_target:
        block.append(f"(manager routed: {routed_target})")
    block.append(from_line)
    manager_file.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
    return line_no + 1


def pending_marker_present(root: Path, pending_file: Path, pending_line: int) -> bool:
    try:
        lines = (root / pending_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    idx = pending_line - 1
    return 0 <= idx < len(lines) and lines[idx].strip() == "(pending)"


def dm_only_pending_routing_present(root: Path, pending_file: Path, pending_line: int) -> bool:
    try:
        lines = (root / pending_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    idx = pending_line - 1
    return (
        0 <= idx < len(lines) - 1
        and lines[idx].strip() == "(pending)"
        and lines[idx + 1].strip() == "DM only"
    )


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
        typ, data = client.uid("search", *(criteria + ["FROM", f'"{self_email}"', "TO", f'"{self_email}"', "SUBJECT", f'"{token}"']))
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


def is_self_addressed_manager_header(msg: Message, self_email: str) -> bool:
    normalized_self = self_email.lower()
    sender = str(msg.get("From", ""))
    recipients = str(msg.get("To", ""))
    subject = str(msg.get("Subject", ""))
    return (
        from_self(sender, self_email)
        and any(address.lower() == normalized_self for _name, address in getaddresses([recipients]))
        and any(token.lower() in subject.lower() for token in MANAGER_SUBJECT_TOKENS)
    )


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
        msg = fetch_manager_count_header(client, uid)
        dt = parsed_message_date(msg)
        if dt is not None and dt >= cutoff and is_self_addressed_manager_header(msg, self_email):
            recent.append(uid)
    return recent


def filter_ignored_uids(uids: list[str], ignored_uids: set[str]) -> list[str]:
    if not ignored_uids:
        return uids
    return [uid for uid in uids if uid not in ignored_uids]


def manager_mail_counts(client: imaplib.IMAP4_SSL, self_email: str, recent_window_s: float, recent_threshold: int, now: datetime | None = None, ignored_uids: set[str] | None = None) -> ManagerMailCounts:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(seconds=recent_window_s)
    ignored = ignored_uids or set()
    total_uids = filter_ignored_uids(search_manager_mail_uids(client, self_email), ignored)
    unread_uids = filter_ignored_uids(search_manager_mail_uids(client, self_email, unread=True), ignored)
    recent_candidates = filter_ignored_uids(search_manager_mail_uids(client, self_email, since=cutoff), ignored)
    recent_total = len(recent_manager_mail_uids(client, self_email, recent_candidates, cutoff))
    return ManagerMailCounts(len(total_uids), len(unread_uids), recent_window_s, recent_total, True)


def handle_manager_mail_thresholds(client: imaplib.IMAP4_SSL, args: Args) -> bool:
    counts = manager_mail_counts(client, args.self_email, args.recent_cleanup_window_s, args.recent_cleanup_threshold, ignored_uids=load_processed_uids(ignored_uids_path(args)))
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
        if uid.isdigit()
        and int(uid) >= min_uid
        and existing_source_line_in_root(root, mail_dir / f"{uid}.txt", manager_file) is None
        and existing_consumed_source_line(root, mail_dir / f"{uid}.txt", manager_file) is None
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


def fetch_message(client: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    typ_msg, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
    if typ_msg != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
        logging.error("email fetch failed: uid=%s typ=%s", uid, typ_msg)
        return None
    return BytesParser(policy=policy.default).parsebytes(msg_data[0][1])


def has_manager_subject_token(subject: str) -> bool:
    return any(token.lower() in subject.lower() for token in MANAGER_SUBJECT_TOKENS)


def sender_display_name(sender: str) -> str:
    name, _address = parseaddr(sender)
    return name.strip().lower()


def manager_authored_message(msg: Message, self_email: str) -> bool:
    sender = str(msg.get("From", ""))
    subject = str(msg.get("Subject", ""))
    if not has_manager_subject_token(subject) or not from_self(sender, self_email):
        return False
    if sender_display_name(sender) == "human":
        return False
    if str(msg.get(MANAGER_EMAIL_HEADER, "")).strip() == "1":
        return True
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
        if uid in ignored_uids:
            continue
        expected_txt_path = args.mail_dir / f"{uid}.txt"
        pending_ref = existing_source_pending_path_line_in_root(args.root, expected_txt_path, manager_file) if uid in unaccepted_pending_uids else None
        if pending_ref is not None:
            pending_file, pending_line = pending_ref
            if dm_only_pending_routing_present(args.root, pending_file, pending_line) or push_email_ref(
                args_for_manager_file(args, pending_file, pending_line), pending_line
            ):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path)
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
            processed_uids.add(uid)
            processed_changed = True
            mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path)
            handled = True
            continue
        if uid in processed_uids:
            existing_source_line = existing_source_line_in_root(args.root, expected_txt_path, manager_file)
            existing_consumed_line = existing_consumed_source_line(args.root, expected_txt_path, manager_file)
            if uid not in unaccepted_pending_uids and (existing_source_line is not None or existing_consumed_line is not None):
                handled = mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path) or handled
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
        existing_pending = existing_source_pending_path_line_in_root(args.root, expected_txt_path, manager_file)
        if existing_pending is not None:
            pending_file, existing_pending_line = existing_pending
            if dm_only_pending_routing_present(args.root, pending_file, existing_pending_line) or push_email_ref(
                args_for_manager_file(args, pending_file, existing_pending_line), existing_pending_line
            ):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path)
                handled = True
            else:
                unaccepted_pending_uids.add(uid)
                unaccepted_changed = True
            continue
        if existing_consumed_source_line(args.root, expected_txt_path, manager_file) is not None:
            logging.info("email uid already has acknowledged or routed source; accepting without duplicate pending: uid=%s root=%s", uid, args.root)
            unaccepted_pending_uids.discard(uid)
            unaccepted_changed = True
            processed_uids.add(uid)
            processed_changed = True
            mark_seen_after_human_intake(client, uid, args, txt_path=expected_txt_path)
            handled = True
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
        if manager_authored_message(msg, args.self_email):
            logging.info("email candidate ignored as manager-authored echo: uid=%s subject=%r", uid, subject)
            ignored_uids.add(uid)
            ignored_changed = True
            continue
        body_text = message_text(msg)
        txt_path = write_mail(args, uid, msg, sender, subject)
        logging.info("email stored: uid=%s path=%s subject=%r", uid, source_ref(args.root, txt_path), subject)
        if is_recovery_subject(subject):
            if recovery_sender_authenticated(msg, args.self_email):
                handle_recovery_email(args, uid, txt_path)
            else:
                append_recovery_record(args.root, txt_path, "recovery email recorded; restart refused because sender authentication did not pass", manager_file)
            processed_uids.add(uid)
            processed_changed = True
            mark_seen_after_human_intake(client, uid, args, msg)
            handled = True
        else:
            route = email_route(args, subject, body_text)
            route_args = replace(args, manager_file=route.manager_file, manager_target=route.manager_target)
            pending_line = append_pending(
                args.root,
                txt_path,
                route.manager_file,
                route.routed_target,
                dm_only=route.direct_delivery and text_marks_dm_only(body_text),
            )
            if route.direct_delivery or push_email_ref(route_args, pending_line):
                unaccepted_pending_uids.discard(uid)
                unaccepted_changed = True
                processed_uids.add(uid)
                processed_changed = True
                mark_seen_after_human_intake(client, uid, args, msg)
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
