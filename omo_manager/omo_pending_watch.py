#!/usr/bin/env python3
"""Watch Markdown files for pending markers and push file-line refs."""
from __future__ import annotations

import argparse
import os
import hashlib
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
PENDING_MARKERS = {"(pending)"}
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
EMAIL_SOURCE_PREFIXES = ("(from email ", "[source: email ")
AGENT_SOURCE_PREFIXES = ("[omo-message-source: origin=agent ", "(from agent ")
IGNORE_PARTS = {".git", ".venv", "__pycache__"}
FENCE_PREFIXES = ("```", "~~~")


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


@dataclass
class FileState:
    mtimes_ns: dict[Path, int]


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


def git_changed_markdown_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--", "*.md"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    files: list[Path] = []
    for line in out.stdout.splitlines():
        rel = Path(line.strip())
        path = root / rel
        if path.is_file() and path.suffix == ".md" and not is_ignored(rel):
            files.append(path)
    return files


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
    command = ["omo_push_to_manager.py", text, "--root", str(args.root), "--submit"]
    if args.manager_target:
        command.extend(["--manager-target", args.manager_target])
    if args.manager_url:
        command.extend(["--manager-url", args.manager_url])
    return subprocess.run(command, check=False).returncode


def maybe_push_idle_status(args: Args, last_activity_s: float, now_s: float) -> bool:
    if now_s - last_activity_s < args.idle_status_interval_s:
        return False
    command = [str(args.status_script), "--root", str(args.root), "--problems-only"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: idle status check failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 3:
        return False
    output = result.stdout.strip()
    if result.stderr.strip():
        output = f"{output}\nstderr:\n{result.stderr.strip()}".strip()
    text = f"manager agent problem: idle watcher found a running task marker needing attention.\n{output}"
    return push_manager_text(args, text) in {0, 2}


def maybe_push_agent_problems(args: Args, seen: dict[str, float], now_wall_s: float) -> bool:
    command = [str(args.status_script), "--root", str(args.root), "--problems-only"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omo_pending_watch: agent problem check failed: {exc}", file=sys.stderr)
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
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
    key = f"agent-problem:{digest}"
    if now_wall_s - seen.get(key, 0.0) < args.agent_problem_repeat_s:
        return False
    text = f"manager agent problem: running task marker needs attention.\n{output}"
    if push_manager_text(args, text) not in {0, 2}:
        return False
    seen[key] = now_wall_s
    return True


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
    file_state = FileState(mtimes_ns={})
    next_full_s = 0.0
    fallback_mail_activity_s = time.time()
    last_digest_check_s = 0.0
    last_agent_problem_check_s = 0.0
    while True:
        now_s = time.monotonic()
        now_wall_s = time.time()
        full = now_s >= next_full_s
        if full:
            next_full_s = now_s + args.full_scan_interval_s
        files = markdown_files(args.root) if full else mtime_changed_markdown_files(args.root, file_state)
        if not full:
            files.extend(path for path in git_changed_markdown_files(args.root) if path not in files)
        changed = scan_once(args, seen, files)
        if now_s - last_agent_problem_check_s >= args.agent_problem_interval_s:
            changed = maybe_push_agent_problems(args, seen, now_wall_s) or changed
            last_agent_problem_check_s = now_s
        seen = expire_seen(seen, time.time())
        if changed:
            save_seen(args.state, seen)
        if now_s - last_digest_check_s >= min(args.digest_idle_after_s, 60.0):
            if maybe_deliver_idle_digest(args, fallback_mail_activity_s, now_wall_s):
                fallback_mail_activity_s = now_wall_s
            last_digest_check_s = now_s
        time.sleep(args.interval_s)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
