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
PENDING_MARKERS = {"(pending)"}
ROUTED_PREFIXES = ("(manager handled:", "(manager routed:")
EMAIL_SOURCE_PREFIXES = ("(from email ", "[source: email ")
IGNORE_PARTS = {".git", ".venv", "__pycache__"}
FENCE_PREFIXES = ("```", "~~~")


@dataclass(frozen=True)
class Marker:
    file: Path
    line: int
    digest: str
    source: str

    @property
    def ref(self) -> str:
        return f"pending: file={self.file} line={self.line} source={self.source} action={self.action}"

    @property
    def action(self) -> str:
        return "ack-human" if self.source == "email" else "no-human-ack"


@dataclass(frozen=True)
class Args:
    root: Path
    manager_url: str
    state: Path
    interval_s: float
    full_scan_interval_s: float
    once: bool
    dry_run: bool
    manager_target: str = ""


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
    once: bool = False
    dry_run: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--manager-url", default=DEFAULT_MANAGER_URL)
    _ = parser.add_argument("--manager-target", default=DEFAULT_MANAGER_TARGET)
    _ = parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    _ = parser.add_argument("--interval-s", type=float, default=2.0)
    _ = parser.add_argument("--full-scan-interval-s", type=float, default=300.0)
    _ = parser.add_argument("--once", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.root.resolve(), parsed.manager_url.rstrip("/"), parsed.state, parsed.interval_s, parsed.full_scan_interval_s, parsed.once, parsed.dry_run, parsed.manager_target)


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


def marker_source(next_line: str) -> str:
    return "email" if next_line.startswith(EMAIL_SOURCE_PREFIXES) else "non-email"


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
            digest = hashlib.sha256(f"{rel}:{idx}:{next_line}".encode("utf-8")).hexdigest()[:16]
            markers.append(Marker(file=rel, line=idx, digest=digest, source=marker_source(next_line)))
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
    while True:
        now_s = time.monotonic()
        full = now_s >= next_full_s
        if full:
            next_full_s = now_s + args.full_scan_interval_s
        files = markdown_files(args.root) if full else mtime_changed_markdown_files(args.root, file_state)
        if not full:
            files.extend(path for path in git_changed_markdown_files(args.root) if path not in files)
        changed = scan_once(args, seen, files)
        seen = expire_seen(seen, time.time())
        if changed:
            save_seen(args.state, seen)
        time.sleep(args.interval_s)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
