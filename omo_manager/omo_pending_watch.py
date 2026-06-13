#!/usr/bin/env python3
"""Watch Markdown files for pending markers and push file-line refs."""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import os
import hashlib
import select
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_MANAGER_URL = os.environ.get("OMO_MANAGER_URL", "")
DEFAULT_MANAGER_TARGET = os.environ.get("OMO_MANAGER_TMUX_TARGET", "")
DEFAULT_STATE = Path(os.environ.get("OMO_MANAGER_PENDING_SEEN", default_state_dir() / "pending-seen.tsv"))
DEFAULT_DIGEST_IDLE_AFTER_S = float(os.environ.get("OMO_MANAGER_DIGEST_IDLE_AFTER_S", "3600"))
DEFAULT_AGENT_PROBLEM_INTERVAL_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_INTERVAL_S", "300"))
DEFAULT_AGENT_PROBLEM_REPEAT_S = float(os.environ.get("OMO_MANAGER_AGENT_PROBLEM_REPEAT_S", "1800"))
DEFAULT_POLL_BACKSTOP_INTERVAL_S = float(os.environ.get("OMO_MANAGER_POLL_BACKSTOP_INTERVAL_S", "30"))
DEFAULT_TMUX_READY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_TMUX_READY_TIMEOUT_S", os.environ.get("OMO_DISPATCH_TMUX_READY_TIMEOUT_S", "300")))
DEFAULT_TMUX_SUBMIT_VERIFY_TIMEOUT_S = float(os.environ.get("OMO_MANAGER_TMUX_SUBMIT_VERIFY_TIMEOUT_S", "5"))
PENDING_MARKERS = {"(pending)"}
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
EMAIL_SOURCE_PREFIXES = ("(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
AGENT_PROBLEM_SOURCE_LINE = "[omo-message-source: origin=agent source=agent action=no-human-ack agent=omo_pending_watch via=omo_pending_watch.py status=agent-problem]"
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


@dataclass(frozen=True)
class Marker:
    file: Path
    line: int
    digest: str
    origin: str
    source: str

    @property
    def ref(self) -> str:
        return f"pending: file={self.file} line={self.line} origin={self.origin} source={self.source} action={self.action}"

    @property
    def action(self) -> str:
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
    )


def load_seen(path: Path) -> dict[str, float]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    seen: dict[str, float] = {}
    for line in lines:
        timestamp_s, sep, key = line.partition("\t")
        if sep:
            try:
                seen[key] = float(timestamp_s)
            except ValueError:
                continue
    return seen


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
    stripped_lines = [line.strip() for line in block_lines]
    if any(line.startswith(AGENT_SOURCE_PREFIXES) for line in stripped_lines):
        return "agent", "agent"
    if any(line.startswith(EMAIL_SOURCE_PREFIXES) for line in stripped_lines):
        return "human", "email"
    return "human", "manual"


def find_markers(root: Path, files: list[Path]) -> list[Marker]:
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
            origin, source = marker_origin_source(lines[idx - 1 : end_idx])
            digest = hashlib.sha256(f"{rel}:{idx}:{next_line}".encode("utf-8")).hexdigest()[:16]
            markers.append(Marker(file=rel, line=idx, digest=digest, origin=origin, source=source))
    return markers


def push_ref(args: Args, marker: Marker) -> int:
    text = marker.ref
    if args.dry_run:
        print(text)
        return 0
    if not args.manager_url and not args.manager_target:
        print("omo_pending_watch: --manager-target or --manager-url is required outside --dry-run", file=sys.stderr)
        return 1
    command = ["omo_push_to_manager.py", text, "--root", str(args.root), "--submit"]
    command.extend(["--pending-file", str(marker.file), "--pending-line", str(marker.line), "--pending-digest", marker.digest])
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
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
    text = f"manager agent status: periodic running-agent status.\n{output}"
    return push_manager_text(args, text) in {0, 2}


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
    return f"manager agent status: periodic running-agent status.\n{output}"


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


def maybe_push_agent_problems(args: Args, seen: dict[str, float], now_wall_s: float) -> bool:
    command = status_command(args, True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: agent problem check failed: {exc}", file=sys.stderr)
        return False
    return handle_agent_problem_result(args, seen, CommandOutput("agent-problems", result.returncode, result.stdout, result.stderr), now_wall_s)


def handle_agent_problem_result(args: Args, seen: dict[str, float], result: CommandOutput, now_wall_s: float) -> bool:
    if result.timed_out:
        print("omo_pending_watch: agent problem check timed out", file=sys.stderr)
        return False
    if result.returncode == 0:
        return False
    if result.returncode != 3:
        print(f"omo_pending_watch: agent problem check exited status={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return False
    output = result.stdout.strip()
    if not output:
        return False
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    has_unstuck = any(line.startswith("unstuck: ") for line in output.splitlines())
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
    key = f"agent-problem:{digest}"
    if not has_unstuck and now_wall_s - seen.get(key, 0.0) < args.agent_problem_repeat_s:
        return False
    text = f"{AGENT_PROBLEM_SOURCE_LINE}\nmanager agent problem: running task marker needs attention.\n{output}"
    if push_manager_text(args, text) not in {0, 2}:
        return False
    seen[key] = now_wall_s
    return True


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
    changed = False
    now_s = time.time()
    for marker in find_markers(args.root, files):
        key = f"{args.root}:{marker.file}:{marker.line}:{marker.digest}"
        if key in seen:
            continue
        if push_ref(args, marker) in {0, 2}:
            seen[key] = now_s
            changed = True
    return changed


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    seen = {} if args.dry_run else expire_seen(load_seen(args.state), time.time())
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
            agent_problem_run = start_command("agent problem check", status_command(args, True), 30)
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
