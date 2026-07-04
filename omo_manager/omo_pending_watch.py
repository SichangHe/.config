#!/usr/bin/env python3
"""Watch Markdown files for pending markers and push file-line refs."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
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
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskLine
from omo_manager.omo_agent_status import active_vl_submanager_target
from omo_manager.omo_agent_status import effective_owner_target
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_agent_status import resolve_task_path
from omo_manager.omo_agent_status import scan_task_state


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "")
DEFAULT_MANAGER_TARGET = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
DEFAULT_STATE = Path(os.environ.get("OMO_MANAGER_PENDING_SEEN", default_state_dir() / "pending-seen.tsv"))
DEFAULT_DIGEST_IDLE_AFTER_S = float(os.environ.get("OMO_MANAGER_DIGEST_IDLE_AFTER_S", "3600"))
DEFAULT_AGENT_PROBLEM_INTERVAL_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_INTERVAL_S", "300"))
DEFAULT_AGENT_PROBLEM_REPEAT_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_REPEAT_S", "1800"))
DEFAULT_AGENT_PROBLEM_TIMEOUT_S = float(
    os.environ.get(
        "OMO_MANAGER_AGENT_PROBLEM_TIMEOUT_S",
        str(max(30.0, float(os.environ.get("OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S", "300")) + 15.0)),
    )
)
DEFAULT_POLL_BACKSTOP_INTERVAL_S = float(os.environ.get("OMO_MANAGER_POLL_BACKSTOP_INTERVAL_S", "30"))
DEFAULT_TMUX_READY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_TMUX_READY_TIMEOUT_S", os.environ.get("OMO_DISPATCH_TMUX_READY_TIMEOUT_S", "300")))
DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_TMUX_SUBMIT_VERIFY_TIMEOUT_S", "5"))
DEFAULT_HUMAN_EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
PENDING_MARKERS = {"(pending)"}
TASK_FILE_LINE_WARNING_THRESHOLD = 2000
TODO_LINE_WARNING_THRESHOLD = 200
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
EMAIL_SOURCE_PREFIXES = ("(record and delegate ", "(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
STATUS_DETAIL_RE = re.compile(r"^\((pending|running|done|blocked)(?::\s*([^)]*))?\)(?:\s+\(([^)]*)\))?$")
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
RUNAT_RE = re.compile(r"^runat:\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
AGENT_PROBLEM_SOURCE_LINE = "[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=agent-problem]"
MANAGER_COMPACTION_SOURCE_LINE = "[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=manager-compaction-reread]"
MANAGER_COMPACTION_REMINDER = "Manager compaction observed. Reread MANAGER.md now unless the compaction summary already included it, then continue from the current manager task state."
TODO_LENGTH_REMINDER = "TODO.md length reminder: TODO.md has {n_lines} lines. Move done material to YYYYMM/old_todos.md and keep TODO.md under 200 lines."
MANAGER_TASK_STATE_REMINDER_HEADER = (
    "manager task-state reminder: MANAGER.md lines 49-51 require each manager-owned task to be `(running)`, `(done)`, or `(blocked)` while the manager is idle. "
    "Start/resume the task, mark it done, or block it with a reason. Single-tag enforcement is intentionally not checked."
)
MANAGER_TASK_STATE_OK = {"running", "done", "blocked"}
MANAGER_TASK_STATE_REMINDER_LIMIT = 20
MANAGER_TASK_STATE_LIVE_SECTIONS = {"todo:current", "todo:human pending", "todo:low priority"}
MANAGER_WORKTREE_REMINDER_LIMIT = 20
MANAGER_WORKTREE_CHECK_TIMEOUT_S = 10
MANAGER_WORKTREE_REMINDER_HEADER = (
    "manager PWD cleanliness reminder: dirty manager-owned changes are present. "
    "Route cleanup to manager-ops or compact/resume with this dirty-state summary before the main manager goes idle."
)
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


def positive_int_env(name: str, default: int) -> int:
    """Read a positive integer env var or use the default."""

    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DEFAULT_SEEN_CACHE_SIZE = positive_int_env("OMO_MANAGER_SEEN_CACHE_SIZE", 50000)


@dataclass(frozen=True)
class Marker:
    """A single unresolved `(pending)` block found in Markdown."""

    file: Path
    line: int
    digest: str
    origin: str
    source: str
    delegate_source: str
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
class CommandOutput:
    name: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


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
                if path.suffix == ".md" and not is_ignored(path.relative_to(self.root)):
                    changed.add(path)
        return sorted(changed), full_scan, True


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    manager_url: str = DEFAULT_MANAGER_URL
    manager_target: str = DEFAULT_MANAGER_TARGET
    state: Path = DEFAULT_STATE
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


SeenCache = OrderedDict[str, float]


def touch_seen(seen: dict[str, float], key: str) -> None:
    """Refresh LRU order for ordered dict-like caches."""

    if isinstance(seen, OrderedDict):
        seen.move_to_end(key)
        return
    seen[key] = seen.pop(key)


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--manager-url", default=DEFAULT_MANAGER_URL)
    _ = parser.add_argument("--manager-target", default=DEFAULT_MANAGER_TARGET)
    _ = parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
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
        parsed.manager_url.rstrip("/"),
        parsed.state,
        parsed.interval_s,
        parsed.full_scan_interval_s,
        parsed.idle_status_interval_s,
        parsed.status_script,
        parsed.once,
        parsed.dry_run,
        parsed.manager_target,
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

    return OrderedDict(initial or {})


def seen_contains(seen: dict[str, float], key: str) -> bool:
    """Check a key and refresh LRU order when possible."""

    if key not in seen:
        return False
    touch_seen(seen, key)
    return True


def seen_get(seen: dict[str, float], key: str, default: float = 0.0) -> float:
    """Read a timestamp and refresh LRU order when possible."""

    if key not in seen:
        return default
    touch_seen(seen, key)
    return seen[key]


def remember_seen(seen: dict[str, float], key: str, timestamp_s: float, limit: int = DEFAULT_SEEN_CACHE_SIZE) -> None:
    """Record a delivery/throttle key and evict oldest keys over the limit."""

    seen[key] = timestamp_s
    touch_seen(seen, key)
    while len(seen) > limit:
        oldest_key = next(iter(seen))
        del seen[oldest_key]


def load_seen(path: Path) -> SeenCache:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return new_seen_cache()
    seen: dict[str, float] = {}
    for line in lines:
        timestamp_s, sep, key = line.partition("\t")
        if sep:
            try:
                seen[key] = float(timestamp_s)
            except ValueError:
                continue
    return new_seen_cache(seen)


def save_seen(path: Path, seen: dict[str, float]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != Path("/tmp"):
        path.parent.chmod(0o700)
    body = "".join(f"{timestamp_s}\t{key}\n" for key, timestamp_s in sorted(seen.items()))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        _ = handle.write(body)


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


def directive_target(path: Path, name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == f"{name}:" and TMUX_TARGET_RE.fullmatch(parts[1]):
            return parts[1]
    return ""


def is_vl_task_file(path: Path) -> bool:
    return path.name.startswith("vl_") or "/vl_" in path.as_posix()


def marker_manager_target(args: Args, marker: Marker) -> str:
    """Choose the manager pane that owns delivery for this marker."""

    target = directive_target(args.root / marker.file, "managerat")
    if target:
        return target
    if is_vl_task_file(marker.file):
        target = active_vl_submanager_target(args.root)
        if target:
            return target
    return target or args.manager_target


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
            origin, source = marker_origin_source(block_lines)
            digest = hashlib.sha256(f"{rel}:{idx}:{next_line}".encode("utf-8")).hexdigest()[:16]
            markers.append(
                Marker(
                    file=rel,
                    line=idx,
                    digest=digest,
                    origin=origin,
                    source=source,
                    delegate_source=delegate_source(block_lines),
                    file_lines=len(lines),
                    blocked_reason=blocked_reason_before_pending(lines, idx),
                )
            )
    return markers


def with_manager_policy_reminder(args: Args, text: str, reminders: Sequence[str] = MANAGER_POLICY_REMINDERS) -> str:
    if args.reminder_random is None or args.reminder_random() >= MANAGER_POLICY_REMINDER_RATE:
        return text
    return f"{text}\n{args.reminder_choice(reminders)}"


def push_ref(args: Args, marker: Marker) -> int:
    """Deliver one pending marker reference, guarded by its current file position."""

    reminders = MANAGER_EMAIL_POLICY_REMINDERS if marker.source == "email" else MANAGER_POLICY_REMINDERS
    text = with_manager_policy_reminder(args, marker.ref, reminders)
    if args.dry_run:
        print(text)
        return 0
    if not args.manager_url and not args.manager_target:
        print("omo_pending_watch: --manager-target or --manager-url is required outside --dry-run", file=sys.stderr)
        return 1
    manager_target = marker_manager_target(args, marker)
    command = ["omo_push_to_manager.py", text, "--root", str(args.root), "--submit"]
    command.extend(["--pending-file", str(marker.file), "--pending-line", str(marker.line), "--pending-digest", marker.digest])
    if manager_target:
        command.extend(["--manager-target", manager_target])
    if args.manager_url:
        command.extend(["--manager-url", args.manager_url])
    return subprocess.run(command, check=False).returncode


def push_manager_text(args: Args, text: str) -> int:
    if args.dry_run:
        print(text)
        return 0
    if not args.manager_url and not args.manager_target:
        print("omo_pending_watch: --manager-target or --manager-url is required outside --dry-run", file=sys.stderr)
        return 1
    return subprocess.run(push_manager_command(args, text), check=False).returncode


def push_manager_text_to_target(args: Args, text: str, manager_target: str) -> int:
    return push_manager_text(replace(args, manager_target=manager_target), text)


def push_manager_command(args: Args, text: str) -> list[str]:
    command = ["omo_push_to_manager.py", text, "--root", str(args.root), "--submit"]
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
    if args.manager_url:
        command.extend(["--manager-url", args.manager_url])
    return command


def maybe_push_idle_status(args: Args, last_activity_s: float, now_s: float) -> bool:
    if now_s - last_activity_s < args.idle_status_interval_s:
        return False
    command = status_command(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: idle status check failed: {exc}", file=sys.stderr)
        return False
    if result.returncode not in {0, 3}:
        print(f"omo_pending_watch: idle status check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return False
    output = result.stdout.strip()
    if not output:
        return False
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    text = idle_status_text(args, output)
    return push_manager_text(args, text) in {0, 2}


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


def manager_worktree_reminder_from_output(output: str) -> str:
    dirty_rows = [
        line
        for line in output.splitlines()
        if line.strip() and not line.startswith("clean: ") and not (line.startswith("repo-error: ") and "not a git repository" in line)
    ]
    if not dirty_rows:
        return ""
    visible_rows = dirty_rows[:MANAGER_WORKTREE_REMINDER_LIMIT]
    if len(dirty_rows) > MANAGER_WORKTREE_REMINDER_LIMIT:
        visible_rows.append(f"worktree: omitted={len(dirty_rows) - MANAGER_WORKTREE_REMINDER_LIMIT}")
    return "\n".join([MANAGER_WORKTREE_REMINDER_HEADER, *visible_rows])


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
        return "\n".join(
            [
                MANAGER_WORKTREE_REMINDER_HEADER,
                f"worktree-check: status=failed error={exc}",
            ]
        )
    output = result.stdout.strip()
    if result.returncode != 0:
        details = output or result.stderr.strip() or f"status={result.returncode}"
        if "not a git repository" in details:
            return ""
        return "\n".join([MANAGER_WORKTREE_REMINDER_HEADER, f"worktree-check: {details}"])
    return manager_worktree_reminder_from_output(output)


def idle_status_text(args: Args, output: str) -> str:
    text = f"manager agent status: periodic running-agent status.\n{output}"
    reminder = manager_task_state_reminder_text(args.root, args.manager_target)
    if reminder:
        text = f"{text}\n{reminder}"
    worktree_reminder = manager_worktree_reminder_text(args.root)
    if worktree_reminder:
        text = f"{text}\n{worktree_reminder}"
    return text


def periodic_status_text(args: Args, result: CommandOutput) -> str | None:
    if result.timed_out:
        print("omo_pending_watch: idle status check timed out", file=sys.stderr)
        return None
    if result.returncode not in {0, 3}:
        print(f"omo_pending_watch: idle status check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    output = result.stdout.strip()
    if not output:
        return None
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    return idle_status_text(args, output)


def manager_push_timeout_s() -> float:
    return DEFAULT_TMUX_READY_TIMEOUT_S + (2 * DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S) + 15


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
    counts = {"not_codex": 0, "blocked_idle": 0, "error": 0, "human_request": 0, "manager_compaction": 0, "ready": 0, "stuck_input": 0, "done-registry-stale": 0}
    for line in lines:
        problem_match = re.match(r"^(not_codex|blocked_idle|error|human_request|manager_compaction|ready|stuck_input): ", line)
        if problem_match is not None:
            counts[problem_match.group(1)] += 1
        elif line.startswith("done-stale: "):
            counts["done-registry-stale"] += 1
    parts = [f"{status}={counts[status]}" for status in ("not_codex", "blocked_idle", "error", "human_request", "manager_compaction", "ready", "stuck_input") if counts[status]]
    if counts["done-registry-stale"]:
        parts.append(f"done-registry-stale={counts['done-registry-stale']}")
    return f"agent-problems: {' '.join(parts)}" if parts else ""


def problem_line_owner_target(line: str) -> str:
    match = re.search(r"\bowner_target=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b", line)
    return match.group(1) if match is not None else ""


def problem_line_target(line: str) -> str:
    match = re.search(r"\bevidence=target=(\S+)", line)
    if match is not None:
        return match.group(1)
    match = re.search(r"\btarget=(\S+)", line)
    return match.group(1) if match is not None else ""


def manager_actions_for_problem_lines(lines: list[str]) -> list[str]:
    count_line = agent_problem_count_line(lines)
    actions: list[str] = []
    if re.search(r"\bblocked_idle=\d+", count_line):
        actions.append("manager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker")
    if re.search(r"\bdone-registry-stale=\d+", count_line):
        actions.append("manager-action: done-registry-stale>0 close stale idle agents with omo_codex_stop.py or prune stale registry rows")
    if re.search(r"\bmanager_compaction=\d+", count_line):
        actions.append("manager-action: manager_compaction>0 reread MANAGER.md after compaction unless the compaction summary already included it")
    return actions


def agent_problem_output_by_owner(output: str) -> dict[str, str]:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return {}
    groups: dict[str, list[str]] = {}
    owner_by_target: dict[str, str] = {}
    for line in lines[1:]:
        if line.startswith("manager-action: "):
            continue
        if line.startswith("unstuck: "):
            continue
        owner = problem_line_owner_target(line)
        groups.setdefault(owner, []).append(line)
        target = problem_line_target(line)
        if target:
            owner_by_target.setdefault(canonical_target(target), owner)
    for line in lines[1:]:
        if not line.startswith("unstuck: "):
            continue
        owner = owner_by_target.get(canonical_target(problem_line_target(line)), "")
        groups.setdefault(owner, []).append(line)
    outputs: dict[str, str] = {}
    for owner, body_lines in groups.items():
        count_line = agent_problem_count_line(body_lines)
        if count_line:
            outputs[owner] = "\n".join([count_line, *manager_actions_for_problem_lines(body_lines), *body_lines])
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
        if re.match(r"^(?:not_codex|blocked_idle|error|human_request|manager_compaction|ready|stuck_input): task=(?:vl_|[^ ]*/vl_|tmux:vl:)", line):
            kept.append(line)
            target = problem_line_target(line)
            if target:
                kept_targets.add(canonical_target(target))
            continue
        if re.match(r"^(?:not_codex|blocked_idle|error|human_request|manager_compaction|ready|stuck_input): task=\S+ evidence=.*\btarget=vl:", line):
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
    digest = hashlib.sha256(f"{vl_target}\n{output}".encode("utf-8")).hexdigest()[:16]
    key = f"vl-agent-problem:{digest}"
    has_unstuck = any(line.startswith("unstuck: ") for line in output.splitlines())
    if not has_unstuck and now_wall_s - seen_get(seen, key) < args.agent_problem_repeat_s:
        return False
    text = f"{AGENT_PROBLEM_SOURCE_LINE}\nmanager agent problem: running task marker needs attention.\n{output}"
    if push_manager_text(vl_args, text) not in {0, 2}:
        return False
    remember_seen(seen, key, now_wall_s)
    return True


def target_aliases(target: str) -> set[str]:
    return {target, target[:-2] if target.endswith(".0") else f"{target}.0"} if target else set()


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def target_session(target: str) -> str:
    return target.split(":", 1)[0] if ":" in target else ""


def same_tmux_target(left: str, right: str) -> bool:
    return bool(target_aliases(left) & target_aliases(right))


def evidence_target(line: str) -> str:
    match = re.search(r"\bevidence=target=(\S+)", line)
    return match.group(1) if match is not None else ""


def manager_self_problem_line(line: str, manager_target: str = "") -> bool:
    if re.match(r"^(?:blocked_idle|error|not_codex|ready|stuck_input): task=manager evidence=.*\brole=manager\b", line):
        return True
    if re.match(r"^(?:blocked_idle|error|not_codex|ready|stuck_input): task=\S+ evidence=target=", line) is None:
        return False
    return same_tmux_target(evidence_target(line), manager_target)


def manager_human_email_problem_line(line: str, manager_target: str = "") -> bool:
    if line.startswith("stuck_input: "):
        unstick_match = re.search(r"\bunstick=(\S+)$", line)
        if unstick_match is not None and not unstick_match.group(1).startswith("not_safe:"):
            return False
    if re.match(r"^(?:error|not_codex|stuck_input): task=manager evidence=.*\brole=manager\b", line):
        return True
    if re.match(r"^(?:error|not_codex|stuck_input): task=\S+ evidence=target=", line) is None:
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
    if seen_contains(seen, key):
        return False
    text = f"{MANAGER_COMPACTION_SOURCE_LINE}\n{MANAGER_COMPACTION_REMINDER}"
    if push_manager_text(args, text) not in {0, 2}:
        return False
    remember_seen(seen, key, now_wall_s)
    return True


def filter_manager_compaction_output(output: str, manager_target: str = "") -> str | None:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return output
    kept = [line for line in lines[1:] if not manager_compaction_line(line, manager_target) and not line.startswith("manager-action: ")]
    if len(kept) == len(lines) - 1:
        return output
    return filtered_problem_output(kept)


def filter_manager_self_problem_output(output: str, manager_target: str = "") -> str | None:
    lines = output.splitlines()
    if not lines or not lines[0].startswith("agent-problems:"):
        return output
    kept = [line for line in lines[1:] if not manager_self_problem_line(line, manager_target) and not manager_self_unstuck_line(line, manager_target) and not line.startswith("manager-action: ")]
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
    counts = {"not_codex": 0, "error": 0, "stuck_input": 0}
    for line in kept:
        problem_match = re.match(r"^(not_codex|error|stuck_input): ", line)
        if problem_match is not None:
            counts[problem_match.group(1)] += 1
    parts = [f"{status}={counts[status]}" for status in ("not_codex", "error", "stuck_input") if counts[status]]
    return "\n".join([f"manager-problems: {' '.join(parts)}", *kept])


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
        with tempfile.TemporaryDirectory(prefix="omo-manager-problem-email.") as tmp:
            tmp_path = Path(tmp)
            subject_file = tmp_path / "subject.txt"
            body_file = tmp_path / "body.md"
            subject_file.write_text(subject + "\n", encoding="utf-8")
            body_file.write_text(body, encoding="utf-8")
            command = [str(DEFAULT_HUMAN_EMAIL_HELPER), "--manager-human", "--subject-file", str(subject_file), "--message-file", str(body_file)]
            if args.manager_target:
                command.extend(("--sender-tmux-target", args.manager_target))
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: manager problem human email failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"omo_pending_watch: manager problem human email exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


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
        return clear_manager_compaction_active(args, seen)
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
    manager_email_output = manager_human_email_problem_output(output, args.manager_target)
    manager_email_sent = email_human_manager_problem(args, manager_email_output)
    output = filter_manager_self_problem_output(output, args.manager_target) or ""
    if not output:
        return manager_email_sent or compaction_changed
    changed = manager_email_sent or compaction_changed
    for owner_target, owner_output in agent_problem_output_by_owner(output).items():
        has_unstuck = any(line.startswith("unstuck: ") for line in owner_output.splitlines())
        digest = hashlib.sha256(f"{owner_target}\n{owner_output}".encode("utf-8")).hexdigest()[:16]
        key = f"agent-problem:{digest}"
        if not has_unstuck and now_wall_s - seen_get(seen, key) < args.agent_problem_repeat_s:
            continue
        text = with_manager_policy_reminder(args, f"{AGENT_PROBLEM_SOURCE_LINE}\nmanager agent problem: running task marker needs attention.\n{owner_output}")
        target = owner_target or args.manager_target
        if push_manager_text_to_target(args, text, target) not in {0, 2}:
            continue
        remember_seen(seen, key, now_wall_s)
        changed = True
    return changed


def start_command(name: str, command: list[str], timeout_s: float, cwd: Path | None = None) -> CommandRun | None:
    try:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def expire_seen(seen: dict[str, float], now_s: float) -> dict[str, float]:
    return seen


def scan_once(args: Args, seen: dict[str, float], files: list[Path]) -> bool:
    """Scan changed Markdown files and deliver newly observed pending refs once."""

    changed = False
    now_s = time.time()
    todo = args.root / "TODO.md"
    if todo in files or not files:
        n_todo_lines = markdown_line_count(todo)
        key = f"{args.root}:TODO.md:line-warning"
        if n_todo_lines <= TODO_LINE_WARNING_THRESHOLD and seen_contains(seen, key):
            del seen[key]
            changed = True
        elif n_todo_lines > TODO_LINE_WARNING_THRESHOLD and not seen_contains(seen, key):
            if push_manager_text(args, TODO_LENGTH_REMINDER.format(n_lines=n_todo_lines)) in {0, 2}:
                remember_seen(seen, key, now_s)
                changed = True
    for marker in find_markers(args.root, files):
        key = f"{args.root}:{marker.file}:{marker.line}:{marker.digest}"
        if seen_contains(seen, key):
            continue
        if push_ref(args, marker) in {0, 2}:
            remember_seen(seen, key, now_s)
            changed = True
    return changed


def markdown_line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    seen = new_seen_cache() if args.dry_run else expire_seen(load_seen(args.state), time.time())
    if args.once:
        changed = scan_once(args, seen, markdown_files(args.root))
        if changed and not args.dry_run:
            save_seen(args.state, seen)
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
    idle_status_push_run: CommandRun | None = None
    digest_run: CommandRun | None = None
    while True:
        now_s = time.monotonic()
        now_wall_s = time.time()
        changed = False
        if now_s >= next_full_s:
            next_full_s = now_s + args.full_scan_interval_s
            next_poll_s = now_s + args.poll_backstop_interval_s
            pending_files = markdown_files(args.root)
            _ = mtime_changed_markdown_files(args.root, file_state)
        elif watcher is not None and now_s >= next_poll_s:
            next_poll_s = now_s + args.poll_backstop_interval_s
            pending_files = mtime_changed_markdown_files(args.root, file_state)
        if pending_files:
            changed = scan_once(args, seen, pending_files)
            pending_files = []
        if agent_problem_run is None and now_s - last_agent_problem_check_s >= args.agent_problem_interval_s:
            agent_problem_run = start_command("agent problem check", status_command(args, True), DEFAULT_AGENT_PROBLEM_TIMEOUT_S)
            last_agent_problem_check_s = now_s
        if agent_problem_run is not None:
            result = poll_command(agent_problem_run, now_wall_s)
            if result is not None:
                changed = handle_agent_problem_result(args, seen, result, now_wall_s) or changed
                agent_problem_run = None
        last_idle_status_check_s, idle_status_run = update_idle_status_check(args, last_idle_status_check_s, now_s, idle_status_run)
        if idle_status_run is not None:
            result = poll_command(idle_status_run, now_wall_s)
            if result is not None:
                text = periodic_status_text(args, result)
                if text is not None and idle_status_push_run is None:
                    idle_status_push_run = start_command("idle status delivery", push_manager_command(args, text), manager_push_timeout_s())
                idle_status_run = None
        if idle_status_push_run is not None:
            result = poll_command(idle_status_push_run, now_wall_s)
            if result is not None:
                if result.timed_out:
                    print("omo_pending_watch: idle status delivery timed out", file=sys.stderr)
                elif result.returncode not in {0, 2} and result.stderr.strip():
                    print(f"omo_pending_watch: idle status delivery exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
                idle_status_push_run = None
        seen = expire_seen(seen, time.time())
        if changed:
            save_seen(args.state, seen)
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
        if agent_problem_run is not None or idle_status_run is not None or idle_status_push_run is not None or digest_run is not None:
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
