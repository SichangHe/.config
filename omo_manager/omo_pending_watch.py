#!/usr/bin/env python3
"""Watch Markdown files for pending markers and push actionable context."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import html
import os
import hashlib
import random
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty
from queue import SimpleQueue
from threading import Lock

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskLine
from omo_manager.omo_agent_status import active_vl_submanager_target
from omo_manager.omo_agent_status import effective_owner_target
from omo_manager.omo_agent_status import is_main_manager_task_file
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_agent_status import resolve_task_path
from omo_manager.omo_agent_status import scan_task_state
from omo_manager.omo_pending_digest import PENDING_CONTENT_CHAR_LIMIT
from omo_manager.omo_pending_digest import pending_tail_digest
from omo_manager.omo_pending_digest import truncate_content
from omo_manager.omo_tmux_send import CodexSendOptions
from omo_manager.omo_tmux_send import DEFAULT_TMUX_ENTER_COUNT
from omo_manager.omo_tmux_send import DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S
from omo_manager.omo_tmux_send import inspect_lines_for_message
from omo_manager.omo_tmux_send import require_sendable_codex_target
from omo_manager.omo_tmux_send import send_to_codex as verified_send_to_codex


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_TARGET = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
DEFAULT_STATE = default_state_dir() / "pending-watch-unused"
DEFAULT_DIGEST_IDLE_AFTER_S = float(os.environ.get("OMO_MANAGER_DIGEST_IDLE_AFTER_S", "3600"))
DEFAULT_AGENT_PROBLEM_INTERVAL_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_INTERVAL_S", "300"))
DEFAULT_AGENT_PROBLEM_REPEAT_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_REPEAT_S", "1800"))
ASYNC_DELIVERY_STARTED = 202
BLOCKED_IDLE_BACKOFF_INITIAL_S = 600.0
BLOCKED_IDLE_BACKOFF_MULTIPLIER = 1.5
BLOCKED_IDLE_BACKOFF_STATE_TTL_S = 30 * 24 * 60 * 60.0
DEFAULT_AGENT_PROBLEM_TIMEOUT_S = float(
    os.environ.get(
        "OMO_MANAGER_AGENT_PROBLEM_TIMEOUT_S",
        str(max(30.0, float(os.environ.get("OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S", "300")) + 15.0)),
    )
)
DEFAULT_POLL_BACKSTOP_INTERVAL_S = float(os.environ.get("OMO_MANAGER_POLL_BACKSTOP_INTERVAL_S", "30"))
DEFAULT_HUMAN_EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
PENDING_MARKERS = {"(pending)"}
FOR_MANAGER_MARKER = "for manager"
DIRECT_MARKER_SEPARATOR_CHARS = {":", ".", "!", "?", ";", ","}
DIRECT_MARKER_WRAPPER_CLOSERS = {")": "(", "]": "[", "}": "{", '"': '"', "'": "'", "`": "`"}
POINTER_WRAPPER_PAIRS = {"`": "`", "'": "'", '"': '"', "(": ")", "[": "]", "<": ">", "{": "}"}
TASK_FILE_LINE_WARNING_THRESHOLD = 2000
TODO_LINE_WARNING_THRESHOLD = 200
EMAIL_CONTENT_CHAR_LIMIT = PENDING_CONTENT_CHAR_LIMIT
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
EMAIL_SOURCE_PREFIXES = ("(record and delegate ", "(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
FILE_REF_RE = re.compile(r"(?<![\w@.-])((?:/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.(?:md|txt|json|yaml|yml|toml|py|sh))(?![\w/.-])")
LIST_POINTER_PREFIX_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
CHECKBOX_POINTER_PREFIX_RE = re.compile(r"^\[[ xX]\]\s+")
MARKDOWN_POINTER_LINK_RE = re.compile(r"^\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)$")
STATUS_DETAIL_RE = re.compile(r"^\((pending|running|done|blocked)(?::\s*([^)]*))?\)(?:\s+\(([^)]*)\))?$")
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
AGENT_POINTER_WITH_TARGET_RE = re.compile(r"^\(from agent ([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) (/tmp/omo-agent-messages-[^)]*)\)$")
AGENT_MESSAGE_DIR_RE = re.compile(r"^/tmp/omo-agent-messages-[^/]+/")
AGENT_PROBLEM_HEADER = "Handle ALL omo_pending_watch agent problems below; only email human if you cannot handle them:"
MANAGER_COMPACTION_REMINDER = "Unless you know the exact content of MANAGER.md, read it. Normally, don't ack human"
TODO_LENGTH_REMINDER = "omo_pending_watch detected TODO.md with {n_lines} lines is too long. Move done material to YYYYMM/old_todos.md per docs/monthly-archive.md."
MANAGER_TASK_STATE_REMINDER_HEADER = (
    "manager task-state reminder: MANAGER.md requires each manager-owned task to have frontmatter `status: running`, `status: done`, or `status: blocked` while the manager is idle. "
    "Start/resume the task, mark it done, or block it with a reason. Single-tag enforcement is intentionally not checked."
)
MANAGER_TASK_STATE_OK = {"running", "done", "blocked"}
MANAGER_TASK_STATE_REMINDER_LIMIT = 20
MANAGER_TASK_STATE_LIVE_SECTIONS = {"todo:current", "todo:human pending", "todo:low priority"}
MANAGER_WORKTREE_REMINDER_LIMIT = 20
MANAGER_WORKTREE_CHECK_TIMEOUT_S = 10
MANAGER_POLICY_REMINDER_RATE = 0.125
MANAGER_POLICY_REMINDERS = (
    "Reminder: delegate work; do not do worker work in the manager.",
    "Reminder: stay high level; route concrete work to agents.",
)
MANAGER_EMAIL_POLICY_REMINDERS = (
    *MANAGER_POLICY_REMINDERS,
    "Reminder: acknowledge human email first, then delegate.",
)
IGNORE_PARTS = {".git", ".venv", "__pycache__"}
FENCE_PREFIXES = ("```", "~~~")
INOTIFY_EVENT = struct.Struct("iIII")
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_UNMOUNT = 0x00002000
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0o0004000)
IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0o2000000)
WATCH_MASK = IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT


def positive_float_env(name: str, default: float) -> float:
    """Read a positive float env var or use the default."""

    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_SEEN_TTL_S = positive_float_env("OMO_MANAGER_SEEN_TTL_S", 24 * 60 * 60)


@dataclass(frozen=True)
class Marker:
    """A single unresolved `(pending)` block found in Markdown."""

    file: Path
    line: int
    digest: str
    origin: str
    source: str
    delegate_source: str
    block_text: str
    pending_tail: str
    file_lines: int
    blocked_reason: str

    @property
    def ref(self) -> str:
        """Return the compact prompt pasted to the manager."""

        text = f"pending: file={self.file} line={self.line} origin={self.origin} source={self.source} action={self.action}"
        if self.delegate_source:
            text = f"{text}\n(delegate {self.delegate_source})"
        if self.blocked_reason:
            text = f"{text}\nblocked-context: latest prior status is blocked; reason={self.blocked_reason}"
        if self.file_lines <= TASK_FILE_LINE_WARNING_THRESHOLD:
            return text
        return f"{text}\ntask-file length warning: this file has {self.file_lines} lines; move future content into a linked continuation file and cross-link both files."

    @property
    def action(self) -> str:
        """Human-origin messages require acknowledgement; agent-origin messages do not."""

        return "ack-human" if self.origin == "human" else "no-human-ack"


@dataclass(frozen=True)
class Args:
    root: Path
    manager_url: str
    state: Path
    interval_s: float
    full_scan_interval_s: float
    idle_status_interval_s: float
    status_script: Path
    once: bool
    dry_run: bool
    manager_target: str = ""
    mail_dir: Path | None = None
    digest_script: Path | None = None
    digest_idle_after_s: float = DEFAULT_DIGEST_IDLE_AFTER_S
    agent_problem_interval_s: float = DEFAULT_AGENT_PROBLEM_INTERVAL_S
    agent_problem_repeat_s: float = DEFAULT_AGENT_PROBLEM_REPEAT_S
    poll_backstop_interval_s: float = DEFAULT_POLL_BACKSTOP_INTERVAL_S
    reminder_random: Callable[[], float] | None = None
    reminder_choice: Callable[[Sequence[str]], str] = random.choice


@dataclass
class FileState:
    mtimes_ns: dict[Path, int]


@dataclass
class CommandRun:
    name: str
    command: list[str]
    process: subprocess.Popen[str]
    started_wall_s: float
    timeout_s: float


@dataclass(frozen=True)
class SourceAttachment:
    source: str
    text: str
    start_line: int = 1
    end_line: int = 0
    error: str = ""


@dataclass(frozen=True)
class ProblemRow:
    status: str
    task: str
    target: str
    output: str = ""
    input_text: str = ""
    reason: str = ""
    pending_item: str = ""
    owner_target: str = ""
    unstick: str = ""
    main_manager: bool = False


@dataclass(frozen=True)
class AgentProblemDispatch:
    text: str
    digest_text: str
    blocked_idle_lines: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CommandOutput:
    name: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    status: int
    error: str = ""


@dataclass(frozen=True)
class PendingGuard:
    root: Path
    pending_file: Path
    pending_line: int
    pending_digest: str


@dataclass(frozen=True)
class DeliverySuccessEvent:
    seen_keys: tuple[str, ...] = ()
    seen_after_clear_keys: tuple[str, ...] = ()
    blocked_idle_lines: tuple[tuple[str, str], ...] = ()
    seen_at_s: float = 0.0
    clear_root: Path | None = None
    clear_marker: Marker | None = None


DELIVERY_SUCCESS_EVENTS: SimpleQueue[DeliverySuccessEvent] = SimpleQueue()
PENDING_SENDS: set[Future[None]] = set()
PENDING_SENDS_LOCK = Lock()


def delivery_accepted(status: int) -> bool:
    return status in {0, 2, ASYNC_DELIVERY_STARTED}


SEND_EXECUTOR: ThreadPoolExecutor | None = None


def send_executor() -> ThreadPoolExecutor:
    """Return the process-local sender pool used by watcher deliveries."""

    global SEND_EXECUTOR
    if SEND_EXECUTOR is None:
        SEND_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="omo-pending-send")
    return SEND_EXECUTOR


def run_verified_send(target: str, message: str, options: CodexSendOptions, pending_guard: PendingGuard | None = None) -> None:
    """Verify the pending marker immediately before the tmux paste."""

    def before_paste() -> None:
        if pending_guard is not None and not pending_marker_present(
            pending_guard.root,
            pending_guard.pending_file,
            pending_guard.pending_line,
            pending_guard.pending_digest,
        ):
            raise RuntimeError("pending marker cleared before tmux paste")

    verified_send_to_codex(target, message, options, before_paste=before_paste if pending_guard is not None else None)


def log_send_result(future: Future[None], success_event: DeliverySuccessEvent | None = None) -> None:
    """Log delivery failure or queue success-side effects for the main loop."""

    try:
        _ = future.result()
    except Exception as exc:
        print(f"omo_pending_watch: async delivery failed: {exc}", file=sys.stderr)
        return
    if success_event is not None:
        DELIVERY_SUCCESS_EVENTS.put(success_event)


def forget_send(future: Future[None]) -> None:
    with PENDING_SENDS_LOCK:
        PENDING_SENDS.discard(future)


def submit_send(
    target: str,
    message: str,
    options: CodexSendOptions,
    pending_guard: PendingGuard | None = None,
    success_event: DeliverySuccessEvent | None = None,
) -> Future[None]:
    """Submit verified tmux delivery without forking a helper process."""

    future = send_executor().submit(run_verified_send, target, message, options, pending_guard)
    with PENDING_SENDS_LOCK:
        PENDING_SENDS.add(future)
    future.add_done_callback(lambda completed: (log_send_result(completed, success_event), forget_send(completed)))
    return future


def send_to_codex(
    target: str,
    message: str,
    options: CodexSendOptions | None = None,
    *,
    pending_guard: PendingGuard | None = None,
    success_event: DeliverySuccessEvent | None = None,
) -> Future[None] | None:
    """Validate a target synchronously, then deliver through a background thread."""

    selected = options or CodexSendOptions(DEFAULT_TMUX_ENTER_COUNT, 0.15, False)
    if selected.dry_run:
        print(message)
        return None
    require_sendable_codex_target(target, inspect_lines_for_message(message))
    return submit_send(target, message, selected, pending_guard, success_event)


def drain_delivery_successes(args: Args, seen: dict[str, float], now_wall_s: float) -> bool:
    """Apply completed background-send side effects on the watcher thread."""

    changed = False
    while True:
        try:
            event = DELIVERY_SUCCESS_EVENTS.get_nowait()
        except Empty:
            return changed
        seen_at_s = event.seen_at_s or now_wall_s
        clear_ok = True
        if event.clear_root is not None and event.clear_marker is not None:
            clear_ok = clear_pending_marker_if_current(event.clear_root, event.clear_marker)
        for key in event.seen_keys:
            remember_seen(seen, key, seen_at_s)
            changed = True
        if clear_ok:
            for key in event.seen_after_clear_keys:
                remember_seen(seen, key, seen_at_s)
                changed = True
        for owner_target, line in event.blocked_idle_lines:
            remember_blocked_idle_report(args, seen, owner_target, line, seen_at_s)
            changed = True


def pending_send_snapshot() -> tuple[Future[None], ...]:
    with PENDING_SENDS_LOCK:
        return tuple(PENDING_SENDS)


def wait_for_delivery_successes(args: Args, seen: dict[str, float], timeout_s: float) -> bool:
    """Wait for already-launched sends so one-shot mode can apply callbacks."""

    changed = False
    deadline_s = time.monotonic() + timeout_s
    while futures := pending_send_snapshot():
        remaining_s = deadline_s - time.monotonic()
        if remaining_s <= 0:
            print(f"omo_pending_watch: timed out waiting for {len(futures)} async delivery result(s)", file=sys.stderr)
            return drain_delivery_successes(args, seen, time.time()) or changed
        _done, _pending = wait_futures(futures, timeout=min(remaining_s, 0.5))
        changed = drain_delivery_successes(args, seen, time.time()) or changed
    return drain_delivery_successes(args, seen, time.time()) or changed


class MarkdownChangeWatcher:
    def __init__(self, root: Path, libc: ctypes.CDLL, fd: int) -> None:
        self.root = root
        self.libc = libc
        self.fd = fd
        self.wd_paths: dict[int, Path] = {}

    @classmethod
    def open(cls, root: Path) -> "MarkdownChangeWatcher | None":
        libc_path = ctypes.util.find_library("c")
        if libc_path is None:
            return None
        libc = ctypes.CDLL(libc_path, use_errno=True)
        inotify_init1 = libc.inotify_init1
        inotify_init1.argtypes = [ctypes.c_int]
        inotify_init1.restype = ctypes.c_int
        fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            return None
        watcher = cls(root, libc, fd)
        try:
            watcher.add_tree(root)
        except OSError as exc:
            print(f"omo_pending_watch: inotify setup failed, falling back to polling: {exc}", file=sys.stderr)
            watcher.close()
            return None
        return watcher

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def add_watch(self, path: Path) -> None:
        inotify_add_watch = self.libc.inotify_add_watch
        inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        inotify_add_watch.restype = ctypes.c_int
        wd = inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            err = ctypes.get_errno()
            if err in {errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                return
            raise OSError(err, os.strerror(err), str(path))
        self.wd_paths[wd] = path

    def add_tree(self, path: Path) -> None:
        if path != self.root and is_ignored(path.relative_to(self.root)):
            return
        self.add_watch(path)
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or is_ignored(child.relative_to(self.root)):
                continue
            self.add_tree(child)

    def wait(self, timeout_s: float) -> tuple[list[Path], bool, bool]:
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout_s))
        if not ready:
            return [], False, False
        changed: set[Path] = set()
        full_scan = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                print(f"omo_pending_watch: inotify read failed, forcing full scan: {exc}", file=sys.stderr)
                return [], True, True
            if not data:
                break
            offset = 0
            while offset + INOTIFY_EVENT.size <= len(data):
                wd, mask, _cookie, name_len = INOTIFY_EVENT.unpack_from(data, offset)
                offset += INOTIFY_EVENT.size
                raw_name = data[offset : offset + name_len].split(b"\0", 1)[0]
                offset += name_len
                base = self.wd_paths.get(wd, self.root)
                path = base / os.fsdecode(raw_name) if raw_name else base
                if mask & (IN_Q_OVERFLOW | IN_UNMOUNT):
                    full_scan = True
                    continue
                if mask & IN_IGNORED:
                    self.wd_paths.pop(wd, None)
                    full_scan = True
                    continue
                if mask & IN_ISDIR:
                    if mask & (IN_CREATE | IN_MOVED_TO):
                        self.add_tree(path)
                    if not is_ignored(path.relative_to(self.root)):
                        full_scan = True
                    continue
                if is_ignored(path.relative_to(self.root)):
                    continue
                if path.suffix == ".md":
                    changed.add(path)
                    continue
                if path.suffix == ".txt" and path.parent.name == "manager_mail":
                    full_scan = True
        return sorted(changed), full_scan, True


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    interval_s: float = 2.0
    full_scan_interval_s: float = 300.0
    idle_status_interval_s: float = 1800.0
    agent_problem_interval_s: float = DEFAULT_AGENT_PROBLEM_INTERVAL_S
    agent_problem_repeat_s: float = DEFAULT_AGENT_PROBLEM_REPEAT_S
    poll_backstop_interval_s: float = DEFAULT_POLL_BACKSTOP_INTERVAL_S
    status_script: Path = Path(__file__).with_name("omo_agent_status.py")
    once: bool = False
    dry_run: bool = False
    mail_dir: Path | None = None
    digest_script: Path | None = None
    digest_idle_after_s: float = DEFAULT_DIGEST_IDLE_AFTER_S


SeenCache = dict[str, float]


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--interval-s", type=float, default=2.0)
    _ = parser.add_argument("--full-scan-interval-s", type=float, default=300.0)
    _ = parser.add_argument("--idle-status-interval-s", type=float, default=1800.0)
    _ = parser.add_argument("--agent-problem-interval-s", type=float, default=DEFAULT_AGENT_PROBLEM_INTERVAL_S)
    _ = parser.add_argument("--agent-problem-repeat-s", type=float, default=DEFAULT_AGENT_PROBLEM_REPEAT_S)
    _ = parser.add_argument("--poll-backstop-interval-s", type=float, default=DEFAULT_POLL_BACKSTOP_INTERVAL_S)
    _ = parser.add_argument("--status-script", type=Path, default=Path(__file__).with_name("omo_agent_status.py"))
    _ = parser.add_argument("--mail-dir", type=Path, default=None)
    _ = parser.add_argument("--digest-script", type=Path, default=None)
    _ = parser.add_argument("--digest-idle-after-s", type=float, default=DEFAULT_DIGEST_IDLE_AFTER_S)
    _ = parser.add_argument("--once", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.idle_status_interval_s <= 0:
        parser.error("--idle-status-interval-s must be positive.")
    if parsed.agent_problem_interval_s <= 0:
        parser.error("--agent-problem-interval-s must be positive.")
    if parsed.agent_problem_repeat_s <= 0:
        parser.error("--agent-problem-repeat-s must be positive.")
    if parsed.digest_idle_after_s <= 0:
        parser.error("--digest-idle-after-s must be positive.")
    if parsed.poll_backstop_interval_s <= 0:
        parser.error("--poll-backstop-interval-s must be positive.")
    root = parsed.root.resolve()
    return Args(
        root,
        "",
        DEFAULT_STATE,
        parsed.interval_s,
        parsed.full_scan_interval_s,
        parsed.idle_status_interval_s,
        parsed.status_script,
        parsed.once,
        parsed.dry_run,
        DEFAULT_MANAGER_TARGET,
        parsed.mail_dir or root / "manager_mail",
        parsed.digest_script or root / "scripts" / "manager-digest",
        parsed.digest_idle_after_s,
        parsed.agent_problem_interval_s,
        parsed.agent_problem_repeat_s,
        parsed.poll_backstop_interval_s,
        random.random,
    )


def new_seen_cache(initial: dict[str, float] | None = None) -> SeenCache:
    """Create the process-local delivery/throttle memory."""

    return dict(initial or {})


def prune_seen(seen: dict[str, float], now_s: float, ttl_s: float = DEFAULT_SEEN_TTL_S) -> None:
    """Delete entries older than the process-local seen TTL."""

    expired = [seen_key for seen_key, timestamp_s in seen.items() if now_s - timestamp_s >= ttl_s]
    for seen_key in expired:
        del seen[seen_key]


def seen_contains(seen: dict[str, float], key: str, now_s: float | None = None, ttl_s: float = DEFAULT_SEEN_TTL_S) -> bool:
    """Check whether a key is still inside the time-based seen window."""

    now = time.time() if now_s is None else now_s
    if key not in seen:
        return False
    if now - seen[key] >= ttl_s:
        del seen[key]
        return False
    return True


def seen_get(seen: dict[str, float], key: str, default: float = 0.0, now_s: float | None = None, ttl_s: float = DEFAULT_SEEN_TTL_S) -> float:
    """Read a timestamp if the key is still inside the seen TTL."""

    now = time.time() if now_s is None else now_s
    if key not in seen:
        return default
    if now - seen[key] >= ttl_s:
        del seen[key]
        return default
    return seen[key]


def remember_seen(seen: dict[str, float], key: str, timestamp_s: float, ttl_s: float = DEFAULT_SEEN_TTL_S) -> None:
    """Record a key and remove entries outside the time-based seen window."""

    prune_seen(seen, timestamp_s, ttl_s)
    seen[key] = timestamp_s


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_PARTS for part in path.parts)


def markdown_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.md") if p.is_file() and not is_ignored(p.relative_to(root))]


def mtime_changed_markdown_files(root: Path, state: FileState) -> list[Path]:
    files = markdown_files(root)
    current: dict[Path, int] = {}
    changed: list[Path] = []
    for path in files:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        current[path] = mtime_ns
        if state.mtimes_ns.get(path) != mtime_ns:
            changed.append(path)
    state.mtimes_ns = current
    return changed


def marker_origin_source(block_lines: list[str]) -> tuple[str, str]:
    """Classify who created a pending block from explicit source markers."""

    stripped_lines = [line.strip() for line in block_lines]
    if any(line.startswith(AGENT_SOURCE_PREFIXES) for line in stripped_lines):
        return "agent", "agent"
    if any(line.startswith(EMAIL_SOURCE_PREFIXES) for line in stripped_lines):
        return "human", "email"
    return "human", "manual"


def delegate_source(block_lines: list[str]) -> str:
    """Return the stored email source path when a pending block came from email."""

    for line in block_lines:
        stripped = line.strip()
        if stripped.startswith("(record and delegate ") and stripped.endswith(")"):
            return stripped[len("(record and delegate ") : -1]
        if stripped.startswith("(from email ") and stripped.endswith(")"):
            return stripped[len("(from email ") : -1]
        if stripped.startswith("[source: email ") and stripped.endswith("]"):
            return stripped[len("[source: email ") : -1]
    return ""


def marker_worker_target(args: Args, marker: Marker, manager_target: str) -> str:
    metadata = read_task_metadata(args.root / marker.file)
    target = metadata.runat if metadata is not None else ""
    if not target or same_tmux_target(target, manager_target) or same_tmux_window_unless_both_panes(target, manager_target):
        return ""
    return target


def pending_source_paths(marker: Marker) -> list[str]:
    sources: list[str] = []
    if marker.delegate_source:
        sources.append(marker.delegate_source)
    for match in FILE_REF_RE.finditer(unquoted_pending_content(marker.block_text)):
        source = match.group(1)
        if source not in sources:
            sources.append(source)
    return sources


def marker_attachments(args: Args, marker: Marker) -> list[SourceAttachment]:
    return [source_attachment(args.root, source) for source in pending_source_paths(marker)]


def attachment_payload_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def readable_attachment_payload(attachments: Sequence[SourceAttachment]) -> str:
    return "\n".join(f"{attachment.source}\0{attachment.text}" for attachment in attachments if not attachment.error)


def marker_is_dm(marker: Marker, attachments: Sequence[SourceAttachment]) -> bool:
    if text_marks_dm(marker.block_text):
        return True
    return any(not attachment.error and text_marks_dm(attachment.text) for attachment in attachments)


def marker_is_dm_only(marker: Marker, attachments: Sequence[SourceAttachment]) -> bool:
    if text_marks_dm_only(marker.block_text):
        return True
    return any(not attachment.error and text_marks_dm_only(attachment.text) for attachment in attachments)


def marker_is_for_manager(marker: Marker, attachments: Sequence[SourceAttachment]) -> bool:
    if text_marks_for_manager(marker.block_text):
        return True
    return any(not attachment.error and text_marks_for_manager(attachment.text) for attachment in attachments)


def dm_worker_seen_key(args: Args, marker: Marker, worker_target: str, attachments: Sequence[SourceAttachment]) -> str:
    payload = f"{marker.block_text}\n{readable_attachment_payload(attachments)}"
    dm = int(marker_is_dm(marker, attachments))
    dm_only = int(marker_is_dm_only(marker, attachments))
    return f"{args.root}:{marker.file}:{marker.line}:{marker.digest}:dm-worker:{canonical_target(worker_target)}:{attachment_payload_digest(payload)}:dm:{dm}:dm-only:{dm_only}"


def attachment_error_seen_key(args: Args, marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    errors = "|".join(f"{attachment.source}:{attachment.error}" for attachment in attachments if attachment.error)
    return f"{args.root}:{marker.file}:{marker.line}:{marker.digest}:attachment-error:{attachment_payload_digest(errors)}"


def marker_seen_key(args: Args, marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    key = f"{args.root}:{marker.file}:{marker.line}:{marker.digest}"
    if not attachments:
        return key
    if any(attachment.error for attachment in attachments):
        return key
    payload = readable_attachment_payload(attachments)
    dm = int(marker_is_dm(marker, attachments))
    dm_only = int(marker_is_dm_only(marker, attachments))
    for_manager = int(marker_is_for_manager(marker, attachments))
    return f"{key}:files:{attachment_payload_digest(payload)}:dm:{dm}:dm-only:{dm_only}:for-manager:{for_manager}"


def is_vl_task_file(path: Path) -> bool:
    return path.name.startswith("vl_") or "/vl_" in path.as_posix()


def marker_manager_target(args: Args, marker: Marker) -> str:
    """Choose the manager pane that owns this marker."""

    metadata = read_task_metadata(args.root / marker.file)
    if metadata is not None:
        if metadata.is_manager:
            return metadata.runat
        return metadata.managerat or args.manager_target
    if is_main_manager_task_file(marker.file):
        return args.manager_target
    return args.manager_target


def marker_for_manager_target(args: Args, marker: Marker) -> str:
    metadata = read_task_metadata(args.root / marker.file)
    if metadata is not None:
        return metadata.managerat or args.manager_target
    return args.manager_target


def blocked_reason_before_pending(lines: list[str], pending_line: int) -> str:
    """Attach the latest blocked reason before a pending block as context."""

    for line in reversed(lines[: pending_line - 1]):
        match = STATUS_DETAIL_RE.match(line.strip())
        if match is None:
            continue
        if match.group(1) != "blocked":
            return ""
        return (match.group(2) or match.group(3) or "blocked with no reason in latest status line").strip()
    return ""


def blocked_reason_for_marker(path: Path, lines: list[str], pending_line: int) -> str:
    metadata = read_task_metadata(path)
    if metadata is not None:
        return metadata.blocked_on if metadata.status == "blocked" else ""
    return blocked_reason_before_pending(lines, pending_line) if is_main_manager_task_file(path) else ""


def line_count(text: str) -> int:
    return len(text.splitlines()) or 1


def display_pending_tail(text: str) -> str:
    """Use the compact agent pointer form in delivered snippets."""

    lines = []
    for line in text.splitlines():
        match = AGENT_POINTER_WITH_TARGET_RE.match(line.strip())
        lines.append(f"(from agent {match.group(2)})" if match is not None else line)
    return "\n".join(lines)


def snippet_section(source: str, start_line: int, end_line: int, body: str, limit: int) -> str:
    file_ref = html.escape(f"{source}:{start_line}-{end_line}", quote=True)
    return f"<snippet file=\"{file_ref}\">\n{truncate_content(body, limit)}\n</snippet>"


def source_error_section(source: str, error: str) -> str:
    source_attr = html.escape(source, quote=True)
    return f"<source-error file=\"{source_attr}\">{html.escape(error)}</source-error>"


def blocked_status_section(marker: Marker) -> str:
    reason = html.escape(marker.blocked_reason)
    return f"<status>blocked\n<blocked_on>{reason}</blocked_on>\n</status>"


def source_attachment(root: Path, source: str) -> SourceAttachment:
    raw_path = Path(source).expanduser()
    path = raw_path
    root_resolved = root.resolve(strict=False)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        return SourceAttachment(source, "", error=f"cannot resolve source: {exc}")
    if raw_path.is_absolute():
        if AGENT_MESSAGE_DIR_RE.match(resolved.as_posix()) is None:
            return SourceAttachment(source, "", error="absolute source is not an agent message file")
    elif resolved != root_resolved and root_resolved not in resolved.parents:
        return SourceAttachment(source, "", error="relative source escapes root")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SourceAttachment(source, "", error=f"cannot read source: {exc}")
    return SourceAttachment(source, text, 1, line_count(text))


def unquoted_pending_content(text: str) -> str:
    """Return pending text that is active request content.

    Markdown/email quote lines are old context. They must not add attachments or
    convert a manager-routed request into a direct worker message. Pending-block
    checks ignore the marker line so `DM: ...` directly under `(pending)` counts
    as a leading DM marker.
    """

    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() in PENDING_MARKERS:
        lines = lines[1:]
    return "\n".join(lines)


def trim_edge_marker_punctuation(text: str) -> str:
    value = unquoted_pending_content(text).strip()
    while value and unicodedata.category(value[0]).startswith("P"):
        value = value[1:].lstrip()
    while value and unicodedata.category(value[-1]).startswith("P"):
        value = value[:-1].rstrip()
    return value


def edge_marker_char_is_word(text: str, index: int) -> bool:
    return text[index].isalnum() or text[index] == "_"


def text_has_edge_marker(text: str, marker: str, *, case_sensitive: bool = True) -> bool:
    """Check active text for a standalone marker at either outer edge."""

    value = trim_edge_marker_punctuation(text)
    haystack = value if case_sensitive else value.casefold()
    needle = marker if case_sensitive else marker.casefold()
    if haystack.startswith(needle) and (len(value) == len(marker) or not edge_marker_char_is_word(value, len(marker))):
        return True
    if haystack.endswith(needle) and (len(value) == len(marker) or not edge_marker_char_is_word(value, -len(marker) - 1)):
        return True
    return False


def text_marks_dm(text: str) -> bool:
    return text_has_edge_marker(text, "DM", case_sensitive=False)


def text_marks_dm_only(text: str) -> bool:
    return text_has_edge_marker(text, "DM only", case_sensitive=False)


def text_marks_for_manager(text: str) -> bool:
    return text_has_edge_marker(text, FOR_MANAGER_MARKER, case_sensitive=False)


def strip_edge_marker(text: str, marker: str) -> str:
    """Remove one routing marker from the active text edge."""

    value = strip_start_edge_marker(text, marker)
    if value != text.strip():
        return value
    return strip_end_edge_marker(text, marker)


def strip_start_edge_marker(text: str, marker: str) -> str:
    """Remove one routing marker from the start of active text."""

    value = text.strip()
    folded = value.casefold()
    marker_folded = marker.casefold()
    start = 0
    while start < len(value) and (value[start].isspace() or unicodedata.category(value[start]).startswith("P")):
        start += 1
    after = start + len(marker)
    if folded.startswith(marker_folded, start) and (after == len(value) or not edge_marker_char_is_word(value, after)):
        while after < len(value):
            if value[after].isspace() or value[after] in DIRECT_MARKER_SEPARATOR_CHARS or value[after] in DIRECT_MARKER_WRAPPER_CLOSERS:
                after += 1
                continue
            break
        return value[after:].strip()
    return value


def strip_end_edge_marker(text: str, marker: str) -> str:
    """Remove one routing marker from the end of active text."""

    value = text.strip()
    folded = value.casefold()
    marker_folded = marker.casefold()
    end = len(value)
    wrapper_closers: list[str] = []
    while end > 0:
        if value[end - 1].isspace() or value[end - 1] in DIRECT_MARKER_SEPARATOR_CHARS:
            end -= 1
            continue
        if value[end - 1] in DIRECT_MARKER_WRAPPER_CLOSERS:
            wrapper_closers.append(value[end - 1])
            end -= 1
            continue
        break
    marker_start = end - len(marker)
    if marker_start >= 0 and folded[marker_start:end] == marker_folded and (marker_start == 0 or not edge_marker_char_is_word(value, marker_start - 1)):
        prefix_end = marker_start
        while prefix_end > 0 and value[prefix_end - 1] in {" ", "\t"}:
            prefix_end -= 1
        while prefix_end > 0 and wrapper_closers and value[prefix_end - 1] == DIRECT_MARKER_WRAPPER_CLOSERS[wrapper_closers[-1]]:
            wrapper_closers.pop()
            prefix_end -= 1
            while prefix_end > 0 and value[prefix_end - 1] in {" ", "\t"}:
                prefix_end -= 1
        return value[:prefix_end].strip()
    return value


def strip_pending_marker_line(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        del lines[0]
    if lines and lines[0].strip() in PENDING_MARKERS:
        del lines[0]
    return "\n".join(lines)


def active_content_edge_indices(lines: Sequence[str]) -> tuple[int, int] | None:
    active = [idx for idx, line in enumerate(lines) if line.strip() and not line.lstrip().startswith(">")]
    if not active:
        return None
    return active[0], active[-1]


def strip_active_edge_marker_once(text: str, marker: str) -> tuple[str, bool]:
    lines = text.splitlines()
    edge_indices = active_content_edge_indices(lines)
    if edge_indices is None:
        return "\n".join(lines).strip(), False
    for idx in edge_indices:
        if text_has_edge_marker(lines[idx], marker, case_sensitive=False):
            before = lines[idx]
            if edge_indices[0] == edge_indices[1]:
                lines[idx] = strip_edge_marker(lines[idx], marker)
            elif idx == edge_indices[0]:
                lines[idx] = strip_start_edge_marker(lines[idx], marker)
            else:
                lines[idx] = strip_end_edge_marker(lines[idx], marker)
            if lines[idx] == before:
                continue
            if not lines[idx]:
                del lines[idx]
            return "\n".join(lines).strip(), True
    return "\n".join(lines).strip(), False


def strip_direct_markers(text: str) -> str:
    """Remove worker-routing markers from active request text."""

    value = strip_pending_marker_line(text)
    while True:
        changed = False
        for marker in ("DM only", "DM"):
            value, changed = strip_active_edge_marker_once(value, marker)
            if changed:
                break
        if not changed:
            return value


def manager_pending_instruction(marker: Marker) -> str:
    if marker.origin == "human":
        return "Immediately record every pending item, then ack human, then remove `(pending)` in below file, then dispatch the task:"
    return "Immediately record every pending item, then don't ack human, then remove `(pending)` in below file, then dispatch the task:"


def marker_snippet_parts(marker: Marker, attachments: Sequence[SourceAttachment]) -> list[str]:
    parts = [snippet_section(str(marker.file), marker.line, marker.file_lines, display_pending_tail(marker.pending_tail), PENDING_CONTENT_CHAR_LIMIT)]
    for attachment in attachments:
        if attachment.error:
            parts.append(source_error_section(attachment.source, attachment.error))
        else:
            parts.append(snippet_section(attachment.source, attachment.start_line, attachment.end_line, attachment.text, EMAIL_CONTENT_CHAR_LIMIT))
    if marker.blocked_reason:
        parts.append(blocked_status_section(marker))
    return parts


def attachment_snippet_parts(attachments: Sequence[SourceAttachment]) -> list[str]:
    parts: list[str] = []
    for attachment in attachments:
        if attachment.error:
            parts.append(source_error_section(attachment.source, attachment.error))
        else:
            parts.append(snippet_section(attachment.source, attachment.start_line, attachment.end_line, attachment.text, EMAIL_CONTENT_CHAR_LIMIT))
    return parts


def bare_source_pointer_text(line: str) -> str:
    """Return a line's file pointer when no other request text is present."""

    value = line.strip()
    while True:
        next_value = LIST_POINTER_PREFIX_RE.sub("", value, count=1).strip()
        next_value = CHECKBOX_POINTER_PREFIX_RE.sub("", next_value, count=1).strip()
        if next_value == value:
            break
        value = next_value
    while len(value) >= 2:
        if match := MARKDOWN_POINTER_LINK_RE.match(value):
            return match.group(1)
        if POINTER_WRAPPER_PAIRS.get(value[0]) != value[-1]:
            break
        value = value[1:-1].strip()
    if match := MARKDOWN_POINTER_LINK_RE.match(value):
        return match.group(1)
    return value


def is_standalone_source_pointer(line: str, sources: set[str]) -> bool:
    return bare_source_pointer_text(line) in sources


def clean_direct_message_lines(text: str) -> str:
    """Remove routing source metadata from direct worker text."""

    lines = []
    for line in strip_direct_markers(text).splitlines():
        stripped = line.strip()
        if stripped.startswith(EMAIL_SOURCE_PREFIXES) or stripped.startswith(AGENT_SOURCE_PREFIXES):
            continue
        lines.append(line)
    return strip_direct_markers("\n".join(lines))


def direct_message_block_text(marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    """Return only human request text from the pending block."""

    attachment_sources = {attachment.source for attachment in attachments if not attachment.error}
    lines = []
    for line in clean_direct_message_lines(marker.block_text).splitlines():
        if is_standalone_source_pointer(line, attachment_sources):
            continue
        lines.append(line)
    return display_pending_tail("\n".join(lines)).strip()


def agent_report_message_text(text: str) -> str:
    """Extract the body from `omo_report.sh` agent report artifacts."""

    lines = text.splitlines()
    if not any(line.startswith(AGENT_SOURCE_PREFIXES) or line.startswith("(sent from agent via omo_report.sh ") for line in lines[:8]):
        return text
    for idx, line in enumerate(lines):
        if line.strip() == "message:":
            return "\n".join(lines[idx + 1 :]).strip()
    return text


def direct_attachment_text(attachment: SourceAttachment) -> str:
    """Return only readable linked-message text for worker DMs."""

    if attachment.error:
        return ""
    return clean_direct_message_lines(agent_report_message_text(attachment.text))


def direct_message_text(marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    """Build a worker DM with no manager instructions or metadata wrappers."""

    parts: list[str] = []
    block_text = direct_message_block_text(marker, attachments)
    if block_text:
        parts.append(block_text)
    for attachment in attachments:
        attachment_text = direct_attachment_text(attachment)
        if attachment_text:
            parts.append(attachment_text)
    return truncate_content("\n\n".join(parts), PENDING_CONTENT_CHAR_LIMIT)


def marker_delivery_text(marker: Marker, attachments: Sequence[SourceAttachment] = (), prefix: str = "") -> str:
    parts = [manager_pending_instruction(marker)]
    if prefix:
        parts.append(prefix)
    parts.extend(marker_snippet_parts(marker, attachments))
    text = "\n".join(parts)
    return text


def marker_worker_dm_text(marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    return direct_message_text(marker, attachments)


def marker_fyi_text(marker: Marker, attachments: Sequence[SourceAttachment]) -> str:
    ack = "ack human" if marker.origin == "human" else "don't ack human"
    parts = [f"Immediately record every pending item, then {ack}, then remove `(pending)` in below file; this message is already dispatched to the agent, this is FYI:"]
    parts.extend(marker_snippet_parts(marker, attachments))
    return "\n".join(parts)


def find_markers(root: Path, files: list[Path]) -> list[Marker]:
    """Find unresolved `(pending)` blocks outside Markdown code fences."""

    markers: list[Marker] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        in_fence = False
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith(FENCE_PREFIXES):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if stripped not in PENDING_MARKERS:
                continue
            next_line = lines[idx].strip() if idx < len(lines) else ""
            rel = path.relative_to(root)
            if next_line.startswith(ROUTED_PREFIXES):
                continue
            end_idx = len(lines)
            for block_idx in range(idx, len(lines)):
                if block_idx != idx - 1 and lines[block_idx].strip() in PENDING_MARKERS:
                    end_idx = block_idx
                    break
            block_lines = lines[idx - 1 : end_idx]
            pending_tail = "\n".join(lines[idx - 1 :])
            origin, source = marker_origin_source(block_lines)
            digest = pending_tail_digest(rel, idx, pending_tail)
            markers.append(
                Marker(
                    file=rel,
                    line=idx,
                    digest=digest,
                    origin=origin,
                    source=source,
                    delegate_source=delegate_source(block_lines),
                    block_text="\n".join(block_lines),
                    pending_tail=pending_tail,
                    file_lines=len(lines),
                    blocked_reason=blocked_reason_for_marker(path, lines, idx),
                )
            )
    return markers


def with_manager_policy_reminder(args: Args, text: str, reminders: Sequence[str] = MANAGER_POLICY_REMINDERS) -> str:
    if args.reminder_random is None or args.reminder_random() >= MANAGER_POLICY_REMINDER_RATE:
        return text
    return f"{text}\n{args.reminder_choice(reminders)}"


def pending_marker_present(root: Path, pending_file: Path, pending_line: int, pending_digest: str = "") -> bool:
    path = root / pending_file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    idx = pending_line - 1
    if idx < 0 or idx >= len(lines) or lines[idx].strip() != "(pending)":
        return False
    if not pending_digest:
        return True
    pending_tail = "\n".join(lines[idx:])
    return pending_tail_digest(pending_file, pending_line, pending_tail) == pending_digest


def clear_pending_marker_if_current(root: Path, marker: Marker) -> bool:
    """Remove one delivered `DM only` marker after verifying the pending tail."""

    path = root / marker.file
    tmp_path: Path | None = None
    try:
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        idx = marker.line - 1
        if idx < 0 or idx >= len(lines) or lines[idx].strip() != "(pending)":
            return False
        pending_tail = "\n".join(lines[idx:])
        if pending_tail_digest(marker.file, marker.line, pending_tail) != marker.digest:
            return False
        del lines[idx]
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            _ = handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        tmp_path.chmod(before.st_mode & 0o7777)
        after = path.stat()
        if after.st_dev != before.st_dev or after.st_ino != before.st_ino or after.st_mtime_ns != before.st_mtime_ns or after.st_size != before.st_size:
            return False
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except OSError as exc:
        print(f"omo_pending_watch: failed to clear delivered DM-only pending marker in {marker.file}: {exc}", file=sys.stderr)
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def push_marker_delivery(args: Args, marker: Marker, text: str, manager_target: str, success_event: DeliverySuccessEvent | None = None) -> DeliveryResult:
    if args.dry_run:
        print(text)
        return DeliveryResult(0)
    if manager_target:
        return try_send_delivery_text(
            "pending delivery",
            text,
            manager_target,
            root=args.root,
            pending_file=marker.file,
            pending_line=marker.line,
            pending_digest=marker.digest,
            success_event=success_event,
        )
    return DeliveryResult(1, "missing delivery target")


def push_marker_text(args: Args, marker: Marker, text: str, manager_target: str, success_event: DeliverySuccessEvent | None = None) -> int:
    return push_marker_delivery(args, marker, text, manager_target, success_event).status


def target_unavailable(result: DeliveryResult) -> bool:
    error = result.error.lower()
    return any(fragment in error for fragment in ("not a codex pane", "status=not_codex", "can't find", "cannot find", "no such window", "no such pane", "does not exist"))


def main_manager_fallback_target(args: Args, failed_target: str) -> str:
    if not args.manager_target or canonical_target(args.manager_target) == canonical_target(failed_target):
        return ""
    return args.manager_target


def with_failed_target_escalation(text: str, failed_target: str, result: DeliveryResult) -> str:
    detail = result.error or f"status {result.status}"
    prefix = f"Delivery to resolved target `{failed_target}` failed: {detail}. Main manager action required: fix the stale task target or restart the owning manager, then handle this message."
    first, sep, rest = text.partition("\n")
    if not sep:
        return f"{text}\n{prefix}"
    return f"{first}\n{prefix}\n{rest}"


def push_marker_text_or_escalate(args: Args, marker: Marker, text: str, manager_target: str, success_event: DeliverySuccessEvent | None = None) -> int:
    result = push_marker_delivery(args, marker, text, manager_target, success_event)
    if result.status in {0, ASYNC_DELIVERY_STARTED} or not target_unavailable(result):
        return result.status
    fallback_target = main_manager_fallback_target(args, manager_target)
    if not fallback_target:
        return result.status
    return push_marker_delivery(args, marker, with_failed_target_escalation(text, manager_target, result), fallback_target, success_event).status


def push_dm_ref(args: Args, seen: dict[str, float], now_s: float, marker: Marker, attachments: Sequence[SourceAttachment], manager_target: str, *, copy_manager: bool = True) -> int:
    worker_target = marker_worker_target(args, marker, manager_target)
    reminders = MANAGER_EMAIL_POLICY_REMINDERS if marker.source == "email" else MANAGER_POLICY_REMINDERS
    marker_name = "DM" if copy_manager else "DM only"
    marker_key = marker_seen_key(args, marker, attachments)
    if not worker_target:
        text = marker_delivery_text(
            marker,
            attachments,
            f"direct-message fallback: pending block or linked file starts or ends with `{marker_name}`, but no safe worker `runat:` target was found; delivering to the manager for routing.",
        )
        return push_marker_text_or_escalate(
            args,
            marker,
            with_manager_policy_reminder(args, text, reminders),
            manager_target,
            DeliverySuccessEvent(seen_keys=(marker_key,), seen_at_s=now_s),
        )
    worker_text = marker_worker_dm_text(marker, attachments)
    worker_key = dm_worker_seen_key(args, marker, worker_target, attachments)
    worker_seen = seen_contains(seen, worker_key, now_s)
    worker_status = 0
    if not worker_seen:
        worker_status = push_marker_text(
            args,
            marker,
            worker_text,
            worker_target,
            DeliverySuccessEvent(
                seen_keys=(worker_key,),
                seen_after_clear_keys=(marker_key,) if not copy_manager else (),
                seen_at_s=now_s,
                clear_root=args.root if not copy_manager else None,
                clear_marker=marker if not copy_manager else None,
            ),
        )
        if worker_status not in {0, ASYNC_DELIVERY_STARTED}:
            text = marker_delivery_text(
                marker,
                attachments,
                f"direct-message fallback: direct worker delivery to {worker_target} failed with status {worker_status}; manager action required to route or help with the human request.",
            )
            return push_marker_text_or_escalate(
                args,
                marker,
                with_manager_policy_reminder(args, text, reminders),
                manager_target,
                DeliverySuccessEvent(seen_keys=(marker_key,), seen_at_s=now_s),
            )
    if not copy_manager:
        if worker_status == ASYNC_DELIVERY_STARTED:
            return ASYNC_DELIVERY_STARTED
        if worker_status == 0:
            remember_seen(seen, worker_key, now_s)
        if args.dry_run or clear_pending_marker_if_current(args.root, marker):
            return 0
        return 1
    if not worker_seen and worker_status == 0:
        remember_seen(seen, worker_key, now_s)
    manager_text = marker_fyi_text(marker, attachments)
    manager_status = push_manager_text_to_target(
        args,
        manager_text,
        manager_target,
        DeliverySuccessEvent(seen_keys=(marker_key,), seen_at_s=now_s),
    )
    if manager_status == 0 and worker_status == ASYNC_DELIVERY_STARTED:
        return ASYNC_DELIVERY_STARTED
    return manager_status


def push_ref(args: Args, seen: dict[str, float], now_s: float, marker: Marker, attachments: Sequence[SourceAttachment]) -> int:
    """Deliver one pending marker, guarded by its current file position."""

    reminders = MANAGER_EMAIL_POLICY_REMINDERS if marker.source == "email" else MANAGER_POLICY_REMINDERS
    marker_key = marker_seen_key(args, marker, attachments)
    for_manager = marker_is_for_manager(marker, attachments)
    manager_target = marker_for_manager_target(args, marker) if for_manager else marker_manager_target(args, marker)
    if not args.dry_run and not manager_target:
        print("omo_pending_watch: a frontmatter `managerat` or OMO_MANAGER_TMUX_TARGET is required outside --dry-run", file=sys.stderr)
        return 1
    if not for_manager and marker_is_dm_only(marker, attachments):
        return push_dm_ref(args, seen, now_s, marker, attachments, manager_target, copy_manager=False)
    if not for_manager and marker_is_dm(marker, attachments):
        return push_dm_ref(args, seen, now_s, marker, attachments, manager_target)
    if any(attachment.error for attachment in attachments):
        error_key = attachment_error_seen_key(args, marker, attachments)
        if seen_contains(seen, error_key, now_s):
            return 1
        text = marker_delivery_text(marker, attachments)
        status = push_marker_text_or_escalate(
            args,
            marker,
            with_manager_policy_reminder(args, text, reminders),
            manager_target,
            DeliverySuccessEvent(seen_keys=(error_key,), seen_at_s=now_s),
        )
        if status == 0:
            remember_seen(seen, error_key, now_s)
        return status if status not in {0, 2} else 1
    text = marker_delivery_text(marker, attachments)
    return push_marker_text_or_escalate(
        args,
        marker,
        with_manager_policy_reminder(args, text, reminders),
        manager_target,
        DeliverySuccessEvent(seen_keys=(marker_key,), seen_at_s=now_s),
    )


def push_manager_text(args: Args, text: str, success_event: DeliverySuccessEvent | None = None) -> int:
    if args.dry_run:
        print(text)
        return 0
    if not args.manager_target:
        print("omo_pending_watch: OMO_MANAGER_TMUX_TARGET is required outside --dry-run", file=sys.stderr)
        return 1
    return try_send_delivery_text("manager delivery", text, args.manager_target, success_event=success_event).status


def push_manager_text_to_target(args: Args, text: str, manager_target: str, success_event: DeliverySuccessEvent | None = None) -> int:
    scoped_args = replace(args, manager_target=manager_target)
    if scoped_args.dry_run:
        print(text)
        return 0
    if not scoped_args.manager_target:
        print("omo_pending_watch: OMO_MANAGER_TMUX_TARGET is required outside --dry-run", file=sys.stderr)
        return 1
    result = try_send_delivery_text("manager delivery", text, scoped_args.manager_target, success_event=success_event)
    if result.status in {0, ASYNC_DELIVERY_STARTED} or not target_unavailable(result):
        return result.status
    fallback_target = main_manager_fallback_target(args, scoped_args.manager_target)
    if not fallback_target:
        return result.status
    return try_send_delivery_text("manager delivery", with_failed_target_escalation(text, scoped_args.manager_target, result), fallback_target, success_event=success_event).status


def send_delivery_text(
    name: str,
    text: str,
    target: str,
    *,
    root: Path | None = None,
    pending_file: Path | None = None,
    pending_line: int = 0,
    pending_digest: str = "",
    submit_verify_timeout_s: float = DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S,
) -> int:
    return try_send_delivery_text(name, text, target, root=root, pending_file=pending_file, pending_line=pending_line, pending_digest=pending_digest, submit_verify_timeout_s=submit_verify_timeout_s).status


def try_send_delivery_text(
    name: str,
    text: str,
    target: str,
    *,
    root: Path | None = None,
    pending_file: Path | None = None,
    pending_line: int = 0,
    pending_digest: str = "",
    submit_verify_timeout_s: float = DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S,
    success_event: DeliverySuccessEvent | None = None,
) -> DeliveryResult:
    if root is not None and pending_file is not None and not pending_marker_present(root, pending_file, pending_line, pending_digest):
        print(f"omo_pending_watch: {name} skipped; pending marker cleared before tmux paste", file=sys.stderr)
        return DeliveryResult(1, "pending marker cleared before tmux paste")
    pending_guard = PendingGuard(root, pending_file, pending_line, pending_digest) if root is not None and pending_file is not None else None
    try:
        async_job = send_to_codex(
            target,
            text,
            CodexSendOptions(
                DEFAULT_TMUX_ENTER_COUNT,
                0.15,
                False,
                submit_verify_timeout_s,
                True,
            ),
            pending_guard=pending_guard,
            success_event=success_event,
        )
    except subprocess.CalledProcessError as exc:
        print(f"omo_pending_watch: {name} failed: {exc}", file=sys.stderr)
        return DeliveryResult(exc.returncode or 1, str(exc))
    except Exception as exc:
        print(f"omo_pending_watch: {name} failed: {exc}", file=sys.stderr)
        return DeliveryResult(1, str(exc))
    return DeliveryResult(ASYNC_DELIVERY_STARTED if async_job is not None else 0)


def maybe_push_idle_status(args: Args, last_activity_s: float, now_s: float) -> bool:
    if now_s - last_activity_s < args.idle_status_interval_s:
        return False
    text = manager_task_state_reminder_text(args.root, args.manager_target)
    if not text:
        return False
    return delivery_accepted(push_manager_text(args, text))


def manager_task_state_reminder_text(root: Path, manager_target: str = "") -> str:
    todo = root / "TODO.md"
    rows: list[str] = []
    seen: set[str] = set()
    for task in parse_task_lines(todo):
        if task.task_file == "TODO.md" or task.task_file in seen or task.section not in MANAGER_TASK_STATE_LIVE_SECTIONS:
            continue
        seen.add(task.task_file)
        state_path = resolve_task_path(root, task.task_file)
        if not reminder_task_owned_by_manager(root, task, manager_target, state_path):
            continue
        if state_path is None:
            rows.append(f"task-state: task={task.task_file} status=missing-file")
            continue
        if find_markers(root, [state_path]):
            rows.append(f"task-state: task={task.task_file} status=pending")
            continue
        state = scan_task_state(state_path)
        if state is not None and state.status in MANAGER_TASK_STATE_OK:
            continue
        status = state.status if state is not None else "missing-status"
        rows.append(f"task-state: task={task.task_file} status={status}")
    if not rows:
        return ""
    visible_rows = rows[:MANAGER_TASK_STATE_REMINDER_LIMIT]
    if len(rows) > MANAGER_TASK_STATE_REMINDER_LIMIT:
        visible_rows.append(f"task-state: omitted={len(rows) - MANAGER_TASK_STATE_REMINDER_LIMIT}")
    return "\n".join([MANAGER_TASK_STATE_REMINDER_HEADER, *visible_rows])


def reminder_task_owned_by_manager(root: Path, task: TaskLine, manager_target: str, state_path: Path | None) -> bool:
    owner = effective_owner_target(root, task, state_path)
    if not owner:
        return True
    return bool(manager_target and same_tmux_target(owner, manager_target))


def worktree_line_value(line: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=(.*?)(?=\s+\w+=|$)", line)
    return match.group(1).strip() if match is not None else ""


def manager_worktree_reminder_header(repo: Path | str) -> str:
    return f"omo_pending_watch detected {repo} is dirty. Clean it up, let every agent commit their changes."


def manager_worktree_reminder_from_output(output: str, root: Path | None = None) -> str:
    dirty_rows = [
        line
        for line in output.splitlines()
        if line.strip() and not line.startswith("clean: ") and not (line.startswith("repo-error: ") and "not a git repository" in line)
    ]
    if not dirty_rows:
        return ""
    repo = worktree_line_value(dirty_rows[0], "repo") or str(root or "manager PWD")
    visible_rows = [worktree_line_value(line, "file") or worktree_line_value(line, "path") or line for line in dirty_rows[:MANAGER_WORKTREE_REMINDER_LIMIT]]
    if len(dirty_rows) > MANAGER_WORKTREE_REMINDER_LIMIT:
        visible_rows.append(f"worktree: omitted={len(dirty_rows) - MANAGER_WORKTREE_REMINDER_LIMIT}")
    return "\n".join([manager_worktree_reminder_header(repo), *visible_rows])


def manager_worktree_reminder_text(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    checker = Path(__file__).resolve().with_name("omo_worktree_check.py")
    try:
        result = subprocess.run(
            [sys.executable, str(checker), "--repo", str(root)],
            capture_output=True,
            text=True,
            timeout=MANAGER_WORKTREE_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "\n".join([manager_worktree_reminder_header(root), f"worktree-check failed: {exc}"])
    output = result.stdout.strip()
    if result.returncode != 0:
        details = output or result.stderr.strip() or f"status={result.returncode}"
        if "not a git repository" in details:
            return ""
        return "\n".join([manager_worktree_reminder_header(root), f"worktree-check: {details}"])
    return manager_worktree_reminder_from_output(output, root)


def worktree_check_command(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    checker = Path(__file__).resolve().with_name("omo_worktree_check.py")
    return [sys.executable, str(checker), "--repo", str(root)]


def worktree_reminder_text_from_result(result: CommandOutput, root: Path | None = None) -> str:
    if result.timed_out:
        return "\n".join([manager_worktree_reminder_header(root or "manager PWD"), "worktree-check failed: timed out"])
    output = result.stdout.strip()
    if result.returncode != 0:
        details = output or result.stderr.strip() or f"status={result.returncode}"
        if "not a git repository" in details:
            return ""
        return "\n".join([manager_worktree_reminder_header(root or "manager PWD"), f"worktree-check: {details}"])
    return manager_worktree_reminder_from_output(output, root)


def periodic_status_text(args: Args, result: CommandOutput) -> str | None:
    if result.timed_out:
        print("omo_pending_watch: idle status check timed out", file=sys.stderr)
        return None
    if result.returncode not in {0, 3}:
        print(f"omo_pending_watch: idle status check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return manager_task_state_reminder_text(args.root, args.manager_target) or None


def update_idle_status_check(args: Args, last_check_s: float, now_s: float, status_run: CommandRun | None) -> tuple[float, CommandRun | None]:
    if now_s - last_check_s < args.idle_status_interval_s:
        return last_check_s, status_run
    if status_run is not None:
        return last_check_s, status_run
    if args.dry_run:
        _ = maybe_push_idle_status(args, last_check_s, now_s)
        return now_s, None
    run = start_command("idle status check", status_command(args), 30)
    return now_s, run


def status_command(args: Args, problems_only: bool = False) -> list[str]:
    command = [str(args.status_script), "--root", str(args.root)]
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
    if problems_only:
        command.append("--problems-only")
    return command


def agent_problem_count_line(lines: list[str]) -> str:
    counts = {"not_codex": 0, "blocked_idle": 0, "error": 0, "manager_compaction": 0, "manager_waiting_subagent": 0, "ready": 0, "stuck_input": 0, "untracked_agent": 0, "done-registry-stale": 0}
    for line in lines:
        problem_match = re.match(r"^(not_codex|blocked_idle|error|manager_compaction|manager_waiting_subagent|ready|stuck_input|untracked_agent): ", line)
        if problem_match is not None:
            counts[problem_match.group(1)] += 1
        elif line.startswith("done-stale: "):
            counts["done-registry-stale"] += 1
    parts = [f"{status}={counts[status]}" for status in ("not_codex", "blocked_idle", "error", "manager_compaction", "manager_waiting_subagent", "ready", "stuck_input", "untracked_agent") if counts[status]]
    if counts["done-registry-stale"]:
        parts.append(f"done-registry-stale={counts['done-registry-stale']}")
    return f"agent-problems: {' '.join(parts)}" if parts else ""


def count_line_value(count_line: str, name: str) -> int:
    match = re.search(rf"\b{re.escape(name)}=(\d+)", count_line)
    return int(match.group(1)) if match is not None else 0


def agent_status_count_line(lines: list[str], count_line: str) -> str:
    counts = {"not_codex": 0, "running": 0, "blocked_idle": 0, "error": 0, "ready": 0, "stuck_input": 0, "human_request": 0}
    for line in lines:
        match = re.match(r"^(not_codex|running|blocked_idle|error|ready|stuck_input|human_request): ", line)
        if match is not None:
            counts[match.group(1)] += 1
    parts = [f"{status}={counts[status]}" for status in ("not_codex", "running", "blocked_idle", "error", "ready", "stuck_input", "human_request")]
    return f"agent-status: {' '.join(parts)} done-registry-stale={count_line_value(count_line, 'done-registry-stale')} pruned={count_line_value(count_line, 'pruned')}"


def problem_line_owner_target(line: str) -> str:
    match = re.search(r"\bowner_target=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b", line)
    return match.group(1) if match is not None else ""


def problem_line_target(line: str) -> str:
    match = re.search(r"\bevidence=target=(\S+)", line)
    if match is not None:
        return match.group(1)
    match = re.search(r"\btarget=(\S+)", line)
    return match.group(1) if match is not None else ""


def problem_line_task(line: str) -> str:
    match = re.match(r"^(?:not_codex|blocked_idle|error|human_request|manager_compaction|manager_waiting_subagent|ready|stuck_input|untracked_agent|done-stale): task=(\S+)", line)
    return match.group(1) if match is not None else ""


def problem_line_status(line: str) -> str:
    match = re.match(r"^(not_codex|blocked_idle|error|human_request|manager_compaction|manager_waiting_subagent|ready|stuck_input|untracked_agent|done-stale): ", line)
    return match.group(1) if match is not None else ""


def problem_line_unstick(line: str) -> str:
    match = re.search(r"\bunstick=(\S+)", line)
    return match.group(1) if match is not None else ""


def problem_line_value(line: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}=(.*?)(?=\s+(?:output_tail|role|persistent_role|task_status|idle_status|reason|pending_item|interrupt|unstick|owner_target)=|$)", line)
    return match.group(1).strip() if match is not None else ""


def parse_problem_row(line: str) -> ProblemRow | None:
    status = problem_line_status(line)
    if not status:
        return None
    task = problem_line_task(line)
    return ProblemRow(
        status=status,
        task=task,
        target=problem_line_target(line),
        output=problem_line_value(line, "output"),
        input_text=problem_line_value(line, "input") or problem_line_value(line, "output"),
        reason=problem_line_value(line, "reason"),
        pending_item=problem_line_value(line, "pending_item"),
        owner_target=problem_line_owner_target(line),
        unstick=problem_line_unstick(line),
        main_manager=bool(task == "manager" and re.search(r"\brole=manager\b", line)),
    )


def enter_attempt_prefix(args: Args, target: str) -> str:
    return f"agent-problem-enter-attempt:{args.root}:{canonical_target(target)}:"


def enter_attempt_count(seen: dict[str, float], args: Args, target: str, now_wall_s: float) -> int:
    prune_seen(seen, now_wall_s)
    prefix = enter_attempt_prefix(args, target)
    return sum(1 for key in seen if key.startswith(prefix))


def remember_enter_attempt(seen: dict[str, float], args: Args, target: str, now_wall_s: float) -> int:
    count = enter_attempt_count(seen, args, target, now_wall_s)
    if count < 3:
        seen[f"{enter_attempt_prefix(args, target)}{count + 1}"] = now_wall_s
        count += 1
    return count


def suppress_enter_attempt_row(args: Args, seen: dict[str, float], line: str, now_wall_s: float) -> bool:
    if problem_line_unstick(line) == "already_sent":
        return True
    if problem_line_unstick(line) != "sent_enter":
        return False
    target = problem_line_target(line)
    if not target:
        return False
    return remember_enter_attempt(seen, args, target, now_wall_s) < 3


def clear_resolved_enter_attempts(args: Args, seen: dict[str, float], lines: list[str], now_wall_s: float) -> None:
    current_stuck = {
        canonical_target(target)
        for line in lines
        if (problem_line_status(line) == "stuck_input" or problem_line_status(line) == "untracked_agent" and problem_line_unstick(line)) and (target := problem_line_target(line))
    }
    prune_seen(seen, now_wall_s)
    prefix = f"agent-problem-enter-attempt:{args.root}:"
    for key in list(seen):
        if not key.startswith(prefix):
            continue
        target = key[len(prefix) :].rsplit(":", 1)[0]
        if target not in current_stuck:
            del seen[key]


def clear_all_enter_attempts(args: Args, seen: dict[str, float]) -> bool:
    prefix = f"agent-problem-enter-attempt:{args.root}:"
    removed = False
    for key in list(seen):
        if key.startswith(prefix):
            del seen[key]
            removed = True
    return removed


def blocked_idle_backoff_prefix(args: Args, owner_target: str, line: str) -> str:
    digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]
    owner = canonical_target(owner_target) or "default"
    return f"blocked-idle-backoff:{args.root}:{owner}:{digest}:"


def prune_blocked_idle_backoff(seen: dict[str, float], prefix: str, now_wall_s: float) -> None:
    for key, expires_s in list(seen.items()):
        if key.startswith(f"{prefix}count:") and now_wall_s >= expires_s:
            del seen[key]


def blocked_idle_backoff_count(seen: dict[str, float], prefix: str, now_wall_s: float) -> int:
    prune_blocked_idle_backoff(seen, prefix, now_wall_s)
    return sum(1 for key in seen if key.startswith(f"{prefix}count:"))


def blocked_idle_report_due(args: Args, seen: dict[str, float], owner_target: str, line: str, now_wall_s: float) -> bool:
    if problem_line_status(line) != "blocked_idle":
        return True
    prefix = blocked_idle_backoff_prefix(args, owner_target, line)
    return now_wall_s >= seen.get(f"{prefix}next", 0.0)


def remember_blocked_idle_report(args: Args, seen: dict[str, float], owner_target: str, line: str, now_wall_s: float) -> None:
    prefix = blocked_idle_backoff_prefix(args, owner_target, line)
    count = blocked_idle_backoff_count(seen, prefix, now_wall_s)
    delay_s = BLOCKED_IDLE_BACKOFF_INITIAL_S * (BLOCKED_IDLE_BACKOFF_MULTIPLIER**count)
    seen[f"{prefix}count:{count + 1}"] = now_wall_s + BLOCKED_IDLE_BACKOFF_STATE_TTL_S
    seen[f"{prefix}next"] = now_wall_s + delay_s


def manager_actions_for_problem_lines(lines: list[str]) -> list[str]:
    count_line = agent_problem_count_line(lines)
    actions: list[str] = []
    if re.search(r"\bblocked_idle=\d+", count_line):
        actions.append("manager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker")
    if re.search(r"\bdone-registry-stale=\d+", count_line):
        actions.append("manager-action: done-registry-stale>0 close agents marked done but still open, or correct the task status")
    if re.search(r"\bmanager_compaction=\d+", count_line):
        actions.append("manager-action: manager_compaction>0 reread MANAGER.md after compaction unless the compaction summary already included it")
    return actions


def task_target_label(row: ProblemRow) -> str:
    note = " (this is the main manager)" if row.main_manager else ""
    if row.task == f"tmux:{row.target}":
        return f"{row.target}{note}"
    if row.target and row.target not in {row.task, row.task.removeprefix("tmux:")}:
        return f"{row.task}{note} {row.target}"
    return f"{row.task or row.target}{note}"


def tagged_text(tag: str, text: str) -> str:
    if not text:
        return f"<{tag}></{tag}>"
    return f"<{tag}>{html.escape(truncate_content(text, PENDING_CONTENT_CHAR_LIMIT))}</{tag}>"


def problem_row_line(row: ProblemRow) -> str:
    label = html.escape(task_target_label(row))
    if row.status == "stuck_input" and row.input_text:
        return f"{label} {tagged_text('input', row.input_text)}"
    if row.status == "untracked_agent" and row.unstick and row.input_text:
        return f"{label} {tagged_text('input', row.input_text)}"
    if row.status == "untracked_agent":
        return f"{label} {tagged_text('output', row.output)}"
    if row.status in {"not_codex", "error", "ready", "manager_compaction", "manager_waiting_subagent"}:
        return f"{label} {tagged_text('output', row.output)}"
    if row.status == "blocked_idle":
        reason = row.reason or row.output
        return f"{label} {tagged_text('blocked_on', reason)}"
    if row.status == "done-stale":
        return label
    return label


def problem_section(status: str, rows: list[ProblemRow]) -> list[str]:
    if not rows:
        return []
    headings = {
        "not_codex": f"{len(rows)} not codex; check if agent failed to launch:",
        "blocked_idle": f"{len(rows)} blocked agents are ready; if they are not actually blocked, correct their status, otherwise make sure whatever is blocking them is being resolved:",
        "error": f"{len(rows)} have visible errors; inspect the pane, fix the error, or restart them:",
        "manager_compaction": f"{len(rows)} are compacting; reread MANAGER.md after compaction unless the summary already included it:",
        "manager_waiting_subagent": f"{len(rows)} managers are waiting on a subagent and could not be interrupted automatically; inspect or interrupt them:",
        "ready": f"{len(rows)} ready and not blocked; consider resuming or closing them:",
        "stuck_input": f"{len(rows)} have their input being stuck; unstick or restart them:",
        "untracked_agent": f"{len(rows)} not tracked in any task file; ask them what their task is, or consider resuming or closing them:",
        "done-stale": f"{len(rows)} are marked `done` but remain open; either close the agents or correct the task status:",
    }
    lines = ["", headings[status]]
    lines.extend(problem_row_line(row) for row in sorted(rows, key=lambda item: task_target_label(item)))
    return lines


def format_agent_problem_report(lines: list[str]) -> str:
    rows = [row for line in lines if (row := parse_problem_row(line)) is not None and row.status != "human_request"]
    if not rows:
        return ""
    order = ("not_codex", "untracked_agent", "blocked_idle", "error", "manager_compaction", "manager_waiting_subagent", "ready", "stuck_input", "done-stale")
    parts = [AGENT_PROBLEM_HEADER]
    for status in order:
        parts.extend(problem_section(status, [row for row in rows if row.status == status]))
    return "\n".join(parts).strip()


def agent_problem_output_by_owner(args: Args, seen: dict[str, float], output: str, now_wall_s: float, backoff_owner_target: str = "") -> dict[str, AgentProblemDispatch]:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return {}
    clear_resolved_enter_attempts(args, seen, lines[1:], now_wall_s)
    groups: dict[str, list[str]] = {}
    digest_groups: dict[str, list[str]] = {}
    blocked_idle_lines: dict[str, list[tuple[str, str]]] = {}
    quiet_blocked_owners: set[str] = set()
    for line in lines[1:]:
        if line.startswith("manager-action: "):
            continue
        if line.startswith("unstuck: "):
            continue
        if suppress_enter_attempt_row(args, seen, line, now_wall_s):
            continue
        line_owner = problem_line_owner_target(line)
        owner = backoff_owner_target or line_owner
        backoff_owner = backoff_owner_target or line_owner
        digest_groups.setdefault(owner, []).append(line)
        if not blocked_idle_report_due(args, seen, backoff_owner, line, now_wall_s):
            quiet_blocked_owners.add(owner)
            continue
        groups.setdefault(owner, []).append(line)
        if problem_line_status(line) == "blocked_idle":
            blocked_idle_lines.setdefault(owner, []).append((backoff_owner, line))
    outputs: dict[str, AgentProblemDispatch] = {}
    for owner, body_lines in groups.items():
        if owner in quiet_blocked_owners and owner not in blocked_idle_lines:
            continue
        text = format_agent_problem_report(body_lines)
        digest_text = format_agent_problem_report(digest_groups.get(owner, body_lines))
        if text:
            outputs[owner] = AgentProblemDispatch(text, digest_text or text, tuple(blocked_idle_lines.get(owner, ())))
    return outputs


def filtered_problem_output(body_lines: list[str], *, suppress_message: str = "") -> str | None:
    count_line = agent_problem_count_line(body_lines)
    if not count_line:
        if suppress_message:
            print(suppress_message, flush=True)
        return None
    return "\n".join([count_line, *manager_actions_for_problem_lines(body_lines), *body_lines])


def filter_vl_problem_output(output: str, owner_target: str = "") -> str:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return ""
    kept: list[str] = []
    kept_targets: set[str] = set()
    for line in lines[1:]:
        if line.startswith("manager-action: "):
            continue
        if re.match(r"^(?:not_codex|blocked_idle|error|manager_compaction|ready|stuck_input|untracked_agent): task=(?:vl_|[^ ]*/vl_|tmux:vl:)", line):
            kept.append(line)
            target = problem_line_target(line)
            if target:
                kept_targets.add(canonical_target(target))
            continue
        if re.match(r"^(?:not_codex|blocked_idle|error|manager_compaction|ready|stuck_input|untracked_agent): task=\S+ evidence=.*\btarget=vl:", line):
            kept.append(line)
            target = problem_line_target(line)
            if target:
                kept_targets.add(canonical_target(target))
            continue
        if owner_target and problem_line_owner_target(line) and same_tmux_target(problem_line_owner_target(line), owner_target):
            kept.append(line)
            target = problem_line_target(line)
            if target:
                kept_targets.add(canonical_target(target))
            continue
        if line.startswith("done-stale: task=vl_") or line.startswith("done-stale: task=") and "/vl_" in line:
            kept.append(line)
    for line in lines[1:]:
        if not line.startswith("unstuck: "):
            continue
        target = canonical_target(problem_line_target(line))
        if target_session(target) == "vl" or target in kept_targets:
            kept.append(line)
    text = filtered_problem_output(kept)
    if text is None:
        return ""
    return text


def maybe_push_vl_agent_problems(args: Args, seen: dict[str, float], now_wall_s: float) -> bool:
    """Route VL-owned status problems to the active VL submanager."""

    if not args.manager_target:
        return False
    vl_target = active_vl_submanager_target(args.root)
    if not vl_target or same_tmux_target(vl_target, args.manager_target):
        return False
    vl_args = replace(args, manager_target=vl_target)
    try:
        result = subprocess.run(status_command(vl_args, True), capture_output=True, text=True, timeout=DEFAULT_AGENT_PROBLEM_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: VL agent problem check failed: {exc}", file=sys.stderr)
        return False
    if result.returncode == 0:
        return False
    if result.returncode != 3:
        print(f"omo_pending_watch: VL agent problem check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return False
    output = filter_vl_problem_output(result.stdout.strip(), vl_target)
    if not output:
        return False
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    has_unstuck = any(line.startswith("unstuck: ") for line in output.splitlines())
    owner_outputs = agent_problem_output_by_owner(vl_args, seen, output, now_wall_s, backoff_owner_target=vl_target)
    dispatch = owner_outputs.get(vl_target) or next(iter(owner_outputs.values()), None)
    if dispatch is None:
        return False
    digest = hashlib.sha256(f"{vl_target}\n{dispatch.digest_text}".encode("utf-8")).hexdigest()[:16]
    key = f"vl-agent-problem:{digest}"
    if not dispatch.blocked_idle_lines and not has_unstuck and now_wall_s - seen_get(seen, key, now_s=now_wall_s) < args.agent_problem_repeat_s:
        return False
    text = dispatch.text
    event = DeliverySuccessEvent(
        seen_keys=(key,),
        blocked_idle_lines=dispatch.blocked_idle_lines,
        seen_at_s=now_wall_s,
    )
    status = push_manager_text(vl_args, text, event)
    if not delivery_accepted(status):
        return False
    if status == 0:
        for backoff_owner, line in dispatch.blocked_idle_lines:
            remember_blocked_idle_report(vl_args, seen, backoff_owner, line, now_wall_s)
        remember_seen(seen, key, now_wall_s)
    return True


def target_aliases(target: str) -> set[str]:
    return {target, target[:-2] if target.endswith(".0") else f"{target}.0"} if target else set()


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def target_session(target: str) -> str:
    return target.split(":", 1)[0] if ":" in target else ""


def target_window(target: str) -> str:
    return target.split(":", 1)[1].split(".", 1)[0] if ":" in target else ""


def target_has_explicit_pane(target: str) -> bool:
    return "." in target.split(":", 1)[1] if ":" in target else False


def same_tmux_window_unless_both_panes(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if target_session(left) != target_session(right) or target_window(left) != target_window(right):
        return False
    return not (target_has_explicit_pane(left) and target_has_explicit_pane(right))


def same_tmux_target(left: str, right: str) -> bool:
    return bool(target_aliases(left) & target_aliases(right))


def evidence_target(line: str) -> str:
    match = re.search(r"\bevidence=target=(\S+)", line)
    return match.group(1) if match is not None else ""


def manager_self_status_line(line: str, manager_target: str = "") -> bool:
    if re.match(r"^(?:not_codex|running|blocked_idle|error|ready|stuck_input|human_request): task=\S+ evidence=target=", line) is None:
        return False
    target = evidence_target(line)
    return bool(target and same_tmux_target(target, manager_target))


def filter_manager_self_status_output(output: str, manager_target: str = "") -> str:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-status:"):
        return output
    kept = [line for line in lines[1:] if not manager_self_status_line(line, manager_target)]
    if len(kept) == len(lines) - 1:
        return output
    if not kept and count_line_value(lines[0], "done-registry-stale") == 0 and count_line_value(lines[0], "pruned") == 0:
        print("omo_pending_watch: suppressed manager self-status report", flush=True)
        return ""
    return "\n".join([agent_status_count_line(kept, lines[0]), *kept])


def manager_self_problem_line(line: str, manager_target: str = "") -> bool:
    if re.match(r"^(?:blocked_idle|error|manager_waiting_subagent|not_codex|ready|stuck_input): task=manager evidence=.*\brole=manager\b", line):
        return True
    if re.match(r"^(?:blocked_idle|error|manager_waiting_subagent|not_codex|ready|stuck_input): task=\S+ evidence=target=", line) is None:
        return False
    return same_tmux_target(evidence_target(line), manager_target)


def manager_human_email_problem_line(line: str, manager_target: str = "") -> bool:
    if line.startswith("stuck_input: "):
        unstick_match = re.search(r"\bunstick=(\S+)$", line)
        if unstick_match is not None and not unstick_match.group(1).startswith("not_safe:"):
            return False
    if re.match(r"^(?:error|manager_waiting_subagent|not_codex|stuck_input): task=manager evidence=.*\brole=manager\b", line):
        return True
    if re.match(r"^(?:error|manager_waiting_subagent|not_codex|stuck_input): task=\S+ evidence=target=", line) is None:
        return False
    return same_tmux_target(evidence_target(line), manager_target)


def manager_self_unstuck_line(line: str, manager_target: str = "") -> bool:
    if re.match(r"^unstuck: target=\S+ task=manager action=sent_enter$", line):
        return True
    match = re.match(r"^unstuck: target=(\S+) task=\S+ action=sent_enter$", line)
    return bool(match is not None and same_tmux_target(match.group(1), manager_target))


def manager_compaction_line(line: str, manager_target: str = "") -> bool:
    if re.match(r"^manager_compaction: task=manager evidence=.*\brole=manager\b", line):
        return True
    match = re.match(r"^manager_compaction: task=\S+ evidence=target=(\S+)", line)
    return bool(match is not None and same_tmux_target(match.group(1), manager_target))


def manager_compaction_active_key(args: Args) -> str:
    return f"manager-compaction-active:{args.root}:{args.manager_target or 'unset'}"


def clear_manager_compaction_active(args: Args, seen: dict[str, float]) -> bool:
    return seen.pop(manager_compaction_active_key(args), None) is not None


def maybe_push_manager_compaction_reminder(args: Args, seen: dict[str, float], output: str, now_wall_s: float) -> bool:
    """Send one reminder while manager compaction is visibly active."""

    lines = output.splitlines()
    if not any(manager_compaction_line(line, args.manager_target) for line in lines[1:]):
        return clear_manager_compaction_active(args, seen)
    key = manager_compaction_active_key(args)
    if seen_contains(seen, key, now_wall_s):
        return False
    status = push_manager_text(args, MANAGER_COMPACTION_REMINDER, DeliverySuccessEvent(seen_keys=(key,), seen_at_s=now_wall_s))
    if not delivery_accepted(status):
        return False
    if status == 0:
        remember_seen(seen, key, now_wall_s)
    return True


def filter_manager_compaction_output(output: str, manager_target: str = "") -> str | None:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return output
    kept = [line for line in lines[1:] if problem_line_status(line) != "human_request" and not manager_compaction_line(line, manager_target) and not line.startswith("manager-action: ")]
    if len(kept) == len(lines) - 1:
        return output
    return filtered_problem_output(kept)


def filter_manager_self_problem_output(output: str, manager_target: str = "") -> str | None:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return output
    kept = [line for line in lines[1:] if problem_line_status(line) != "human_request" and not manager_self_problem_line(line, manager_target) and not manager_self_unstuck_line(line, manager_target) and not line.startswith("manager-action: ")]
    if len(kept) == len(lines) - 1:
        return output
    return filtered_problem_output(kept, suppress_message="omo_pending_watch: suppressed manager self-problem report")


def manager_human_email_problem_output(output: str, manager_target: str = "") -> str:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return ""
    kept = [line for line in lines[1:] if manager_human_email_problem_line(line, manager_target)]
    if not kept:
        return ""
    return filtered_problem_output(kept) or ""


def manager_problem_targets(output: str, manager_target: str = "") -> set[str]:
    targets = {manager_target} if manager_target else set()
    for line in output.splitlines()[1:]:
        target = problem_line_target(line)
        if target:
            targets.add(target)
    return targets


def active_manager_problem_targets(root: Path, output: str, manager_target: str = "") -> list[str]:
    seen_files: set[str] = set()
    seen_targets: set[str] = set()
    targets: list[str] = []
    problem_targets = manager_problem_targets(output, manager_target)
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in seen_files or task.section not in MANAGER_TASK_STATE_LIVE_SECTIONS:
            continue
        seen_files.add(task.task_file)
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path) if state_path is not None else None
        if state is None or state.status != "running" or not state.is_manager or not state.target:
            continue
        if any(same_tmux_target(state.target, problem_target) for problem_target in problem_targets):
            continue
        target_key = canonical_target(state.target)
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        targets.append(state.target)
    return targets


def manager_problem_route_text(args: Args, output: str) -> str:
    lines = output.splitlines()
    body_lines = [line for line in lines[1:] if not line.startswith("manager-action: ")] if lines and lines[0].startswith("agent-problems:") else lines
    return format_agent_problem_report(body_lines)


def route_or_email_manager_problem(args: Args, output: str) -> bool:
    if not output:
        return False
    targets = active_manager_problem_targets(args.root, output, args.manager_target)
    if not targets:
        return email_human_manager_problem(args, output)
    route_target = args.reminder_choice(targets)
    targets = [route_target, *(target for target in targets if target != route_target)]
    text = manager_problem_route_text(args, output)
    for target in targets:
        if args.dry_run:
            print(f"manager problem route due: target={target}\n{text}", flush=True)
            return True
        result = try_send_delivery_text("manager problem routing", text, target)
        if delivery_accepted(result.status):
            return True
    return email_human_manager_problem(args, output)


def email_human_manager_problem(args: Args, output: str) -> bool:
    if not output:
        return False
    subject = "manager watcher detected manager error"
    body = (
        "The manager watcher detected a manager pane problem that may prevent normal manager delivery.\n\n"
        f"root: {args.root}\n"
        f"manager_target: {args.manager_target or 'unset'}\n\n"
        f"{output}\n"
    )
    if args.dry_run:
        print(f"manager human email due: {subject}\n{body}", flush=True)
        return True
    try:
        tmp_path = Path(tempfile.mkdtemp(prefix="omo-manager-problem-email."))
        tmp_path.chmod(0o700)
        subject_file = tmp_path / "subject.txt"
        body_file = tmp_path / "body.md"
        subject_file.write_text(subject + "\n", encoding="utf-8")
        body_file.write_text(body, encoding="utf-8")
        command = [str(DEFAULT_HUMAN_EMAIL_HELPER), "--manager-human", "--subject-file", str(subject_file), "--message-file", str(body_file)]
        if args.manager_target:
            command.extend(("--sender-tmux-target", args.manager_target))
        _ = subprocess.Popen(
            cleanup_after_email_command(tmp_path, command),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"omo_pending_watch: manager problem human email launch failed: {exc}", file=sys.stderr)
        return False
    return True


def cleanup_after_email_command(tmp_path: Path, command: list[str]) -> list[str]:
    cleanup_code = (
        "import shutil, subprocess, sys\n"
        "tmp = sys.argv[1]\n"
        "command = sys.argv[2:]\n"
        "rc = 1\n"
        "try:\n"
        "    rc = subprocess.call(command)\n"
        "finally:\n"
        "    shutil.rmtree(tmp, ignore_errors=True)\n"
        "raise SystemExit(rc)\n"
    )
    return [sys.executable, "-c", cleanup_code, str(tmp_path), *command]


def maybe_push_agent_problems(args: Args, seen: dict[str, float], now_wall_s: float) -> bool:
    command = status_command(args, True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=DEFAULT_AGENT_PROBLEM_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: agent problem check failed: {exc}", file=sys.stderr)
        return False
    changed = maybe_push_vl_agent_problems(args, seen, now_wall_s)
    return handle_agent_problem_result(args, seen, CommandOutput("agent-problems", result.returncode, result.stdout, result.stderr), now_wall_s) or changed


def handle_agent_problem_result(args: Args, seen: dict[str, float], result: CommandOutput, now_wall_s: float) -> bool:
    """Filter, throttle, and route status-problem output."""

    if result.timed_out:
        print("omo_pending_watch: agent problem check timed out", file=sys.stderr)
        return False
    if result.returncode == 0:
        enter_changed = clear_all_enter_attempts(args, seen)
        return clear_manager_compaction_active(args, seen) or enter_changed
    if result.returncode != 3:
        print(f"omo_pending_watch: agent problem check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return False
    output = result.stdout.strip()
    if not output:
        return False
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    compaction_changed = maybe_push_manager_compaction_reminder(args, seen, output, now_wall_s)
    output = filter_manager_compaction_output(output, args.manager_target) or ""
    if not output:
        return compaction_changed
    manager_problem_output = manager_human_email_problem_output(output, args.manager_target)
    manager_problem_sent = route_or_email_manager_problem(args, manager_problem_output)
    output = filter_manager_self_problem_output(output, args.manager_target) or ""
    if not output:
        return manager_problem_sent or compaction_changed
    changed = manager_problem_sent or compaction_changed
    for owner_target, dispatch in agent_problem_output_by_owner(args, seen, output, now_wall_s).items():
        digest = hashlib.sha256(f"{owner_target}\n{dispatch.digest_text}".encode("utf-8")).hexdigest()[:16]
        key = f"agent-problem:{digest}"
        if not dispatch.blocked_idle_lines and now_wall_s - seen_get(seen, key, now_s=now_wall_s) < args.agent_problem_repeat_s:
            continue
        text = with_manager_policy_reminder(args, dispatch.text)
        target = owner_target or args.manager_target
        event = DeliverySuccessEvent(
            seen_keys=(key,),
            blocked_idle_lines=dispatch.blocked_idle_lines,
            seen_at_s=now_wall_s,
        )
        status = push_manager_text_to_target(args, text, target, event)
        if not delivery_accepted(status):
            continue
        if status == 0:
            for backoff_owner, line in dispatch.blocked_idle_lines:
                remember_blocked_idle_report(args, seen, backoff_owner, line, now_wall_s)
            remember_seen(seen, key, now_wall_s)
        changed = True
    return changed


def start_command(name: str, command: list[str], timeout_s: float, cwd: Path | None = None, env: dict[str, str] | None = None) -> CommandRun | None:
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        print(f"omo_pending_watch: {name} start failed: {exc}", file=sys.stderr)
        return None
    return CommandRun(name, command, process, time.time(), timeout_s)


def poll_command(run: CommandRun, now_wall_s: float) -> CommandOutput | None:
    timed_out = now_wall_s - run.started_wall_s >= run.timeout_s
    if run.process.poll() is None and not timed_out:
        return None
    if timed_out and run.process.poll() is None:
        run.process.kill()
    try:
        stdout, stderr = run.process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        run.process.kill()
        stdout, stderr = run.process.communicate(timeout=1)
        timed_out = True
    return CommandOutput(run.name, run.process.returncode or 0, stdout, stderr, timed_out)


def latest_mail_mtime_s(mail_dir: Path) -> float | None:
    try:
        paths = list(mail_dir.glob("*.txt"))
    except OSError:
        return None
    latest_s: float | None = None
    for path in paths:
        try:
            mtime_s = path.stat().st_mtime
        except OSError:
            continue
        if latest_s is None or mtime_s > latest_s:
            latest_s = mtime_s
    return latest_s


def maybe_deliver_idle_digest(args: Args, fallback_mail_activity_s: float, now_wall_s: float) -> bool:
    mail_dir = args.mail_dir or args.root / "manager_mail"
    digest_script = args.digest_script or args.root / "scripts" / "manager-digest"
    last_mail_s = latest_mail_mtime_s(mail_dir) or fallback_mail_activity_s
    if now_wall_s - last_mail_s < args.digest_idle_after_s:
        return False
    queue = args.root / "manager_digest.md"
    try:
        if not queue.read_text(encoding="utf-8").strip():
            return False
    except OSError:
        return False
    if args.dry_run:
        print(f"manager digest idle delivery due: no human email for {int(now_wall_s - last_mail_s)}s")
        return True
    try:
        result = subprocess.run([str(digest_script), "deliver"], cwd=args.root, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: digest delivery failed: {exc}", file=sys.stderr)
        return False
    return result.returncode == 0


def idle_digest_due(args: Args, fallback_mail_activity_s: float, now_wall_s: float) -> bool:
    mail_dir = args.mail_dir or args.root / "manager_mail"
    last_mail_s = latest_mail_mtime_s(mail_dir) or fallback_mail_activity_s
    if now_wall_s - last_mail_s < args.digest_idle_after_s:
        return False
    queue = args.root / "manager_digest.md"
    try:
        return bool(queue.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def scan_once(args: Args, seen: dict[str, float], files: list[Path]) -> bool:
    """Scan changed Markdown files and deliver newly observed pending refs once."""

    now_s = time.time()
    changed = drain_delivery_successes(args, seen, now_s)
    todo = args.root / "TODO.md"
    if todo in files or not files:
        n_todo_lines = markdown_line_count(todo)
        key = f"{args.root}:TODO.md:line-warning"
        if n_todo_lines <= TODO_LINE_WARNING_THRESHOLD and seen_contains(seen, key, now_s):
            del seen[key]
            changed = True
        elif n_todo_lines > TODO_LINE_WARNING_THRESHOLD and not seen_contains(seen, key, now_s):
            status = push_manager_text(
                args,
                TODO_LENGTH_REMINDER.format(n_lines=n_todo_lines),
                DeliverySuccessEvent(seen_keys=(key,), seen_at_s=now_s),
            )
            if delivery_accepted(status):
                if status == 0:
                    remember_seen(seen, key, now_s)
                changed = True
    for marker in find_markers(args.root, files):
        attachments = marker_attachments(args, marker)
        key = marker_seen_key(args, marker, attachments)
        if seen_contains(seen, key, now_s):
            continue
        status = push_ref(args, seen, now_s, marker, attachments)
        if status == 0:
            remember_seen(seen, key, now_s)
            changed = True
        elif status == ASYNC_DELIVERY_STARTED:
            changed = True
    return drain_delivery_successes(args, seen, time.time()) or changed


def markdown_line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    seen = new_seen_cache()
    if args.once:
        _ = scan_once(args, seen, markdown_files(args.root))
        _ = wait_for_delivery_successes(args, seen, max(10.0, DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S + 5.0))
        return 0
    watcher = MarkdownChangeWatcher.open(args.root)
    if watcher is None:
        print("omo_pending_watch: using mtime polling fallback", file=sys.stderr)
    file_state = FileState(mtimes_ns={})
    _ = mtime_changed_markdown_files(args.root, file_state)
    next_full_s = time.monotonic() + args.full_scan_interval_s
    next_poll_s = time.monotonic() + args.poll_backstop_interval_s
    pending_files = markdown_files(args.root)
    fallback_mail_activity_s = time.time()
    last_digest_check_s = 0.0
    last_agent_problem_check_s = 0.0
    last_idle_status_check_s = time.monotonic()
    agent_problem_run: CommandRun | None = None
    idle_status_run: CommandRun | None = None
    digest_run: CommandRun | None = None
    worktree_run: CommandRun | None = None
    while True:
        now_s = time.monotonic()
        now_wall_s = time.time()
        _ = drain_delivery_successes(args, seen, now_wall_s)
        if now_s >= next_full_s:
            next_full_s = now_s + args.full_scan_interval_s
            next_poll_s = now_s + args.poll_backstop_interval_s
            pending_files = markdown_files(args.root)
            _ = mtime_changed_markdown_files(args.root, file_state)
        elif watcher is not None and now_s >= next_poll_s:
            next_poll_s = now_s + args.poll_backstop_interval_s
            pending_files = mtime_changed_markdown_files(args.root, file_state)
        if pending_files:
            _ = scan_once(args, seen, pending_files)
            pending_files = []
        if agent_problem_run is None and now_s - last_agent_problem_check_s >= args.agent_problem_interval_s:
            agent_problem_run = start_command("agent problem check", status_command(args, True), DEFAULT_AGENT_PROBLEM_TIMEOUT_S)
            last_agent_problem_check_s = now_s
        if agent_problem_run is not None:
            result = poll_command(agent_problem_run, now_wall_s)
            if result is not None:
                _ = handle_agent_problem_result(args, seen, result, now_wall_s)
                agent_problem_run = None
        last_idle_status_check_s, idle_status_run = update_idle_status_check(args, last_idle_status_check_s, now_s, idle_status_run)
        if idle_status_run is not None:
            result = poll_command(idle_status_run, now_wall_s)
            if result is not None:
                text = periodic_status_text(args, result)
                if text is not None and args.manager_target:
                    _ = send_delivery_text("idle status delivery", text, args.manager_target)
                if worktree_run is None and (worktree_command := worktree_check_command(args.root)) is not None:
                    worktree_run = start_command("worktree check", worktree_command, MANAGER_WORKTREE_CHECK_TIMEOUT_S)
                idle_status_run = None
        if worktree_run is not None:
            result = poll_command(worktree_run, now_wall_s)
            if result is not None:
                text = worktree_reminder_text_from_result(result, args.root)
                if text and args.manager_target:
                    _ = send_delivery_text("worktree reminder delivery", text, args.manager_target)
                worktree_run = None
        if now_s - last_digest_check_s >= min(args.digest_idle_after_s, 60.0):
            if args.dry_run and maybe_deliver_idle_digest(args, fallback_mail_activity_s, now_wall_s):
                fallback_mail_activity_s = now_wall_s
            elif digest_run is None and idle_digest_due(args, fallback_mail_activity_s, now_wall_s):
                digest_script = args.digest_script or args.root / "scripts" / "manager-digest"
                digest_run = start_command("digest delivery", [str(digest_script), "deliver"], 180, args.root)
            last_digest_check_s = now_s
        if digest_run is not None:
            result = poll_command(digest_run, now_wall_s)
            if result is not None:
                if result.timed_out:
                    print("omo_pending_watch: digest delivery timed out", file=sys.stderr)
                elif result.returncode == 0:
                    fallback_mail_activity_s = now_wall_s
                elif result.stderr.strip():
                    print(f"omo_pending_watch: digest delivery exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
                digest_run = None
        deadlines = [
            next_full_s,
            last_agent_problem_check_s + args.agent_problem_interval_s,
            last_idle_status_check_s + args.idle_status_interval_s,
            last_digest_check_s + min(args.digest_idle_after_s, 60.0),
        ]
        if watcher is not None:
            deadlines.append(next_poll_s)
        if agent_problem_run is not None or idle_status_run is not None or digest_run is not None or worktree_run is not None:
            deadlines.append(now_s + 0.2)
        timeout_s = args.interval_s if watcher is None else max(0.0, min(deadlines) - now_s)
        if watcher is None:
            time.sleep(timeout_s)
            now_s = time.monotonic()
            pending_files = markdown_files(args.root) if now_s >= next_full_s else mtime_changed_markdown_files(args.root, file_state)
            continue
        event_files, full_scan, notified = watcher.wait(timeout_s)
        if notified:
            next_poll_s = time.monotonic() + args.poll_backstop_interval_s
        if full_scan:
            pending_files = markdown_files(args.root)
            next_full_s = time.monotonic() + args.full_scan_interval_s
            next_poll_s = time.monotonic() + args.poll_backstop_interval_s
        else:
            pending_files = event_files


def crash_email_sender_target(argv: list[str]) -> str:
    del argv
    return DEFAULT_MANAGER_TARGET if TMUX_TARGET_RE.fullmatch(DEFAULT_MANAGER_TARGET) else ""


def email_human_watcher_crash(argv: list[str], exc: BaseException) -> None:
    subject = "pending watcher crashed"
    body = (
        "The pending watcher crashed unexpectedly.\n\n"
        f"argv: {' '.join(argv) or '(none)'}\n\n"
        f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="omo-pending-watch-crash-email.") as tmp:
            tmp_path = Path(tmp)
            subject_file = tmp_path / "subject.txt"
            body_file = tmp_path / "body.md"
            subject_file.write_text(subject + "\n", encoding="utf-8")
            body_file.write_text(body, encoding="utf-8")
            command = [str(DEFAULT_HUMAN_EMAIL_HELPER), "--manager-human", "--subject-file", str(subject_file), "--message-file", str(body_file)]
            if sender_target := crash_email_sender_target(argv):
                command.extend(("--sender-tmux-target", sender_target))
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as email_exc:
        print(f"omo_pending_watch: crash human email failed: {email_exc}", file=sys.stderr)
        return
    if result.returncode != 0:
        print(f"omo_pending_watch: crash human email exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)


def cli(argv: list[str]) -> int:
    try:
        return main(argv)
    except Exception as exc:
        email_human_watcher_crash(argv, exc)
        raise


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
