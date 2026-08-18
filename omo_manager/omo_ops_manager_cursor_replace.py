#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Replace only `ops_manager.md` at `wl:3` from Codex to Cursor in the same pane.

This helper is pinned to that one non-human manager task. It keeps the existing
task record, pending queue, `managerat: wl:18`, and child ownership. It requires
the exact approved human source line, the exact task and run target, and a fresh
pre-action revalidation. It fails closed on target drift, ambiguous ownership,
pending delivery, dirty unknown state, or any `h*` target. It does not launch a
replacement pane. Invoke it from a different pane than `wl:3`.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_agent_status import parse_task_lines, read_task_metadata, resolve_task_path, same_tmux_target, task_has_pending_marker
from omo_manager.omo_codex_start import Pane, StartError, resolve_pane, respawn_codex
from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import inspect, pane_has_exact_cursor_process
from omo_manager.omo_manager_rotate import PaneIdentity, RotationError, default_state_dir, ensure_private_directory, invocation_is_target, is_codex_launch_argv, process_is_under, read_processes, resolve_exact_pane, write_private
from omo_manager.omo_task import DEFAULT_WORKER_INSTRUCTIONS, codex_cmd, replace_frontmatter_fields
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskFrontmatterError, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import replace_if_unchanged, tracked_dirty_state

TASK_NAME = "ops_manager.md"
REQUIRED_TARGET = "wl:3"
REQUIRED_MANAGERAT = "wl:18"
AUTHORITY_RELATIVE = "manager_mail/85c5dff58359-741.txt"
AUTHORITY_LINES = (17, 17)
AUTHORITY_TEXT = "Replace wl:3 with Cursor"
CURSOR_MODEL = "cursor-grok-4.6"
CURSOR_EFFORT = "xhigh"
MAX_AUTHORITY_BYTES = 1_000_000
SUCCESS_STATUSES = {"ready", "running"}
ALLOWED_PRE_STATUSES = {"ready", "running", "error"}
UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
LINE_RANGE_RE = re.compile(r"^([1-9]\d*)-([1-9]\d*)$")
CONTINUATION = """Continue the existing operations manager role for ops_manager.md in this same pane.
The task record, pending queue, managerat wl:18, and child ownership are unchanged.
This pane now runs Cursor Agent. Do not relaunch children or alter human-owned h sessions.
"""


class ReplaceError(RuntimeError):
    """A pinned ops-manager Cursor replacement safety gate failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    target: str
    authority_file: Path
    authority_lines: tuple[int, int]
    state_dir: Path
    startup_timeout_s: float
    poll_interval_s: float
    dry_run: bool


@dataclass(frozen=True)
class Authority:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str
    excerpt: str


@dataclass(frozen=True)
class ChildSnapshot:
    path: Path
    managerat: str
    runat: str
    sha256: str


@dataclass(frozen=True)
class Snapshot:
    pane: Pane
    task_path: Path
    task_text: str
    task_stat: os.stat_result
    metadata: TaskMetadata
    children: tuple[ChildSnapshot, ...]
    authority: Authority


class ParsedArgs(argparse.Namespace):
    root: Path
    task_file: str
    target: str
    authority_file: Path
    authority_lines: tuple[int, int]
    state_dir: Path
    startup_timeout_s: float
    poll_interval_s: float
    dry_run: bool


def parse_line_range(value: str) -> tuple[int, int]:
    match = LINE_RANGE_RE.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("must be START-END with positive, inclusive line numbers")
    start, end = (int(part) for part in match.groups())
    if start > end:
        raise argparse.ArgumentTypeError("START must not exceed END")
    return start, end


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--task-file", required=True, help="Must be ops_manager.md; confirmation of the pinned manager task.")
    _ = parser.add_argument("--target", required=True, help="Must be wl:3 or wl:3.0; confirmation of the pinned run target. Human-owned h* targets are refused.")
    _ = parser.add_argument("--authority-file", type=Path, required=True, help="Must be manager_mail/85c5dff58359-741.txt under --root.")
    _ = parser.add_argument("--authority-lines", type=parse_line_range, required=True, help="Must be 17-17, the exact approved human source line.")
    _ = parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    _ = parser.add_argument("--startup-timeout-s", type=float, default=45.0)
    _ = parser.add_argument("--poll-interval-s", type=float, default=0.5)
    _ = parser.add_argument("--dry-run", action="store_true", help="Run every non-pane-replacement gate. Does not respawn wl:3 or edit the task record.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.task_file != TASK_NAME:
        parser.error("this helper is pinned to ops_manager.md and refuses any other task file")
    if parsed.target.partition(":")[0].startswith("h"):
        parser.error("refusing human-owned h* target")
    if not same_tmux_target(parsed.target, REQUIRED_TARGET):
        parser.error("this helper is pinned to wl:3")
    if parsed.authority_lines != AUTHORITY_LINES:
        parser.error("authority lines must be the exact approved 17-17 range")
    authority_file = parsed.authority_file
    if not authority_file.is_absolute() and authority_file.as_posix() != AUTHORITY_RELATIVE:
        parser.error("authority file must be manager_mail/85c5dff58359-741.txt")
    if parsed.startup_timeout_s <= 0 or parsed.poll_interval_s <= 0:
        parser.error("timeout and poll interval must be positive")
    return Args(
        parsed.root.expanduser().resolve(strict=False),
        parsed.task_file,
        parsed.target,
        parsed.authority_file.expanduser(),
        parsed.authority_lines,
        parsed.state_dir.expanduser().resolve(strict=False),
        parsed.startup_timeout_s,
        parsed.poll_interval_s,
        parsed.dry_run,
    )


def file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def owner_private_directory(path: Path, label: str) -> None:
    try:
        value = path.stat()
    except OSError as error:
        raise ReplaceError(f"{label} is unavailable: {error}") from error
    if path.is_symlink() or not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise ReplaceError(f"{label} must be one owner-private real directory")


def reject_h_target(target: str) -> None:
    if target.partition(":")[0].startswith("h"):
        raise ReplaceError("refusing human-owned h* target")


def read_authority(args: Args) -> Authority:
    try:
        root = args.root.resolve(strict=True)
        mail_root = (root / "manager_mail").resolve(strict=True)
    except OSError as error:
        raise ReplaceError(f"work-log root or manager-mail directory is unavailable: {error}") from error
    owner_private_directory(mail_root, "manager-mail directory")
    candidate = args.authority_file if args.authority_file.is_absolute() else root / args.authority_file
    try:
        source = candidate.resolve(strict=True)
    except OSError as error:
        raise ReplaceError(f"authority source is unavailable: {error}") from error
    if source.parent != mail_root or source.relative_to(root).as_posix() != AUTHORITY_RELATIVE:
        raise ReplaceError("authority source must be the exact approved manager-mail file")
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ReplaceError(f"authority source cannot be opened safely: {error}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
            raise ReplaceError("authority source must be one owner-private regular file")
        chunks: list[bytes] = []
        remaining = MAX_AUTHORITY_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = source.stat()
    except OSError as error:
        raise ReplaceError(f"authority source disappeared while it was read: {error}") from error
    if file_identity(before) != file_identity(after) or file_identity(after) != file_identity(current):
        raise ReplaceError("authority source changed while it was read")
    if len(data) > MAX_AUTHORITY_BYTES:
        raise ReplaceError("authority source exceeds the bounded size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReplaceError("authority source is not UTF-8") from error
    lines = text.splitlines()
    start, end = args.authority_lines
    if end > len(lines):
        raise ReplaceError("authority source line range exceeds the file")
    excerpt = "\n".join(lines[start - 1 : end]).strip()
    if excerpt != AUTHORITY_TEXT:
        raise ReplaceError("authority excerpt is not the exact approved human line")
    return Authority(source, before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(data).hexdigest(), excerpt)


def verify_authority(args: Args, expected: Authority) -> None:
    if read_authority(args) != expected:
        raise ReplaceError("authority source changed or no longer matches before replacement")


def candidate_task_paths(root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for task in parse_task_lines(root / "TODO.md"):
        resolved = resolve_task_path(root, task.task_file)
        if resolved is not None:
            paths.add(resolved)
    for path in root.glob("*.md"):
        if path.is_file() and not path.is_symlink():
            try:
                paths.add(path.resolve(strict=True))
            except OSError:
                continue
    return tuple(sorted(paths))


def require_unique_task(root: Path, task_path: Path) -> tuple[TaskMetadata, tuple[ChildSnapshot, ...]]:
    todo_matches = [task for task in parse_task_lines(root / "TODO.md") if Path(task.task_file).name == TASK_NAME]
    if len(todo_matches) != 1:
        raise ReplaceError("ambiguous ownership: TODO.md does not uniquely list ops_manager.md")
    if todo_matches[0].target and not same_tmux_target(todo_matches[0].target, REQUIRED_TARGET):
        raise ReplaceError("TODO.md target for ops_manager.md drifted from wl:3")
    text = task_path.read_text(encoding="utf-8")
    try:
        metadata = parse_task_metadata(text, root)
    except TaskFrontmatterError as error:
        raise ReplaceError(f"ops_manager.md frontmatter is invalid: {error}") from error
    if metadata is None:
        raise ReplaceError("ops_manager.md requires valid frontmatter")
    if metadata.version != TASK_FRONTMATTER_V1:
        raise ReplaceError("ops_manager.md must keep v1.0.0 frontmatter")
    if not metadata.is_manager:
        raise ReplaceError("ops_manager.md is not a manager task")
    if metadata.status == "done":
        raise ReplaceError("ops_manager.md is not an active manager task")
    if metadata.tool != "codex":
        raise ReplaceError("ops_manager.md is not the Codex manager this helper may replace")
    if not same_tmux_target(metadata.runat, REQUIRED_TARGET):
        raise ReplaceError("ops_manager.md runat drifted from wl:3")
    if not same_tmux_target(metadata.managerat, REQUIRED_MANAGERAT):
        raise ReplaceError("ops_manager.md managerat drifted from wl:18")
    if task_has_pending_marker(task_path):
        raise ReplaceError("pending delivery: ops_manager.md still has a live (pending) marker")
    claimants: list[Path] = []
    children: list[ChildSnapshot] = []
    for path in candidate_task_paths(root):
        other = read_task_metadata(path, root)
        if other is None or other.status == "done" or other.runat == "retired":
            continue
        if same_tmux_target(other.runat, REQUIRED_TARGET):
            claimants.append(path)
        if path != task_path and same_tmux_target(other.managerat, REQUIRED_TARGET):
            children.append(ChildSnapshot(path, other.managerat, other.runat, hashlib.sha256(path.read_bytes()).hexdigest()))
    if claimants != [task_path]:
        names = ", ".join(path.name for path in claimants) or "none"
        raise ReplaceError(f"ambiguous ownership: active runat wl:3 claimants are {names}")
    return metadata, tuple(children)


def require_known_dirty_owners(root: Path, task_path: Path) -> None:
    inside = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ReplaceError("work-log tracked dirty state is unknown")
    toplevel = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if toplevel.returncode != 0:
        raise ReplaceError("work-log tracked dirty state is unknown")
    try:
        if Path(toplevel.stdout.strip()).resolve(strict=True) != root.resolve(strict=True):
            raise ReplaceError("work-log root is not the Git worktree root")
        _payload, dirty = tracked_dirty_state(root)
    except (OSError, subprocess.CalledProcessError, TaskFrontmatterError) as error:
        raise ReplaceError("work-log tracked dirty state is unknown") from error
    for relative in dirty:
        path = (root / relative).resolve()
        if path == task_path:
            continue
        metadata = read_task_metadata(path, root)
        if metadata is None:
            raise ReplaceError(f"dirty unknown state: {relative}")
        if same_tmux_target(metadata.runat, REQUIRED_TARGET) and path != task_path:
            raise ReplaceError(f"ambiguous ownership: dirty {relative} also claims wl:3")


def require_live_codex(pane: Pane) -> None:
    processes = read_processes()
    launches = [process for process in processes.values() if process.state != "Z" and process_is_under(process.pid, pane.pane_pid, processes) and is_codex_launch_argv(process.argv)]
    if len(launches) != 1:
        raise ReplaceError("ops_manager pane does not have exactly one live Codex launch")
    report = inspect(StatusArgs(pane.target, 80))
    if report.status not in ALLOWED_PRE_STATUSES:
        raise ReplaceError(f"ops_manager pane state is dirty or unknown: {report.status}")


def resolve_pinned_pane(target: str, root: Path) -> Pane:
    reject_h_target(target)
    try:
        identity = resolve_exact_pane(target)
    except RotationError as error:
        raise ReplaceError(str(error)) from error
    reject_h_target(identity.canonical_target)
    if not same_tmux_target(identity.canonical_target, REQUIRED_TARGET):
        raise ReplaceError("resolved pane drifted from wl:3")
    try:
        pane = resolve_pane(identity.canonical_target)
    except StartError as error:
        raise ReplaceError(str(error)) from error
    reject_h_target(pane.target)
    if (pane.pane_id, pane.window_id, pane.pane_pid, pane.workdir) != (identity.pane_id, identity.window_id, identity.pane_pid, identity.working_directory):
        raise ReplaceError("target drift between exact-pane and start-helper identity")
    if pane.workdir.resolve(strict=False) != root.resolve(strict=False):
        raise ReplaceError("wl:3 working directory drifted from the work-log root")
    return pane


def invocation_from_target(pane: Pane) -> bool:
    identity = PaneIdentity(pane.target, pane.pane_id, pane.window_id, pane.pane_pid, pane.workdir)
    return invocation_is_target(identity, read_processes())


def bind(args: Args) -> Snapshot:
    authority = read_authority(args)
    pane = resolve_pinned_pane(args.target, args.root)
    if invocation_from_target(pane):
        raise ReplaceError("this helper must run from a different pane than wl:3")
    task_path = args.root / TASK_NAME
    try:
        resolved = task_path.resolve(strict=True)
    except OSError as error:
        raise ReplaceError("ops_manager.md must be one direct file under --root") from error
    if resolved.parent != args.root.resolve(strict=False):
        raise ReplaceError("ops_manager.md must be one direct file under --root")
    metadata, children = require_unique_task(args.root, resolved)
    require_known_dirty_owners(args.root, resolved)
    require_live_codex(pane)
    if shutil.which("agent") is None:
        raise ReplaceError("Cursor Agent CLI `agent` is not available on PATH")
    if not DEFAULT_WORKER_INSTRUCTIONS.is_file() or not (args.root / "MANAGER.md").is_file():
        raise ReplaceError("worker defaults or MANAGER.md is missing")
    text = resolved.read_text(encoding="utf-8")
    return Snapshot(pane, resolved, text, resolved.stat(), metadata, children, authority)


def task_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def verify_snapshot(args: Args, expected: Snapshot) -> Snapshot:
    verify_authority(args, expected.authority)
    current = bind(args)
    if current.pane.pane_id != expected.pane.pane_id or current.pane.window_id != expected.pane.window_id or current.pane.pane_pid != expected.pane.pane_pid:
        raise ReplaceError("target drift before replacement")
    if task_digest(current.task_text) != task_digest(expected.task_text):
        raise ReplaceError("ops_manager.md changed before replacement")
    if current.metadata.pending_task_items != expected.metadata.pending_task_items:
        raise ReplaceError("pending queue changed before replacement")
    if current.metadata.managerat != expected.metadata.managerat or current.children != expected.children:
        raise ReplaceError("managerat or child ownership changed before replacement")
    return current


def cursor_command(pane: Pane, root: Path, prompt_path: Path) -> str:
    command = codex_cmd(
        tool="cursor",
        model=CURSOR_MODEL,
        reasoning_effort=CURSOR_EFFORT,
        workdir=pane.workdir,
        prompt_file=prompt_path,
        manager_file=root / "MANAGER.md",
    )
    rendered = f"export OMO_AGENT_TMUX_TARGET={shlex.quote(pane.target)} OMO_WORK_LOGS_ROOT={shlex.quote(str(root))} && exec {command}"
    if "resume" in rendered.casefold() or UUID_RE.search(rendered) is not None:
        raise ReplaceError("Cursor launch command unexpectedly contains resume or a session UUID")
    return rendered


def wait_for_cursor(pane: Pane, timeout_s: float, poll_interval_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    time.sleep(min(poll_interval_s, timeout_s))
    while time.monotonic() < deadline:
        current = resolve_pane(pane.target)
        if current.pane_id != pane.pane_id or current.window_id != pane.window_id:
            raise ReplaceError("target drift after respawn")
        if pane_has_exact_cursor_process(current.target, current.pane_id):
            report = inspect(StatusArgs(current.target, 80))
            if report.status in SUCCESS_STATUSES:
                return report.status
            if report.status == "error":
                raise ReplaceError("Cursor startup classified as error")
        time.sleep(min(poll_interval_s, max(0.01, deadline - time.monotonic())))
    raise ReplaceError("timed out waiting for Cursor Agent to become running or ready")


def verify_children(root: Path, expected: tuple[ChildSnapshot, ...]) -> None:
    for child in expected:
        current = hashlib.sha256(child.path.read_bytes()).hexdigest()
        metadata = read_task_metadata(child.path, root)
        if metadata is None or current != child.sha256 or metadata.managerat != child.managerat or metadata.runat != child.runat:
            raise ReplaceError(f"child ownership changed: {child.path.name}")


def replace_manager(args: Args) -> str:
    reject_h_target(args.target)
    with task_target_lock(args.root, REQUIRED_TARGET):
        snapshot = bind(args)
        if args.dry_run:
            _ = verify_snapshot(args, snapshot)
            return "dry-run"
        ensure_private_directory(args.state_dir)
        prompt_dir = args.state_dir / "ops-manager-cursor-replace"
        ensure_private_directory(prompt_dir)
        prompt_path = prompt_dir / "continuation.txt"
        write_private(prompt_path, CONTINUATION, replace=True)
        snapshot = verify_snapshot(args, snapshot)
        command = cursor_command(snapshot.pane, args.root, prompt_path)
        replaced = False
        try:
            respawn_codex(snapshot.pane, command)
            replaced = True
            _status = wait_for_cursor(snapshot.pane, args.startup_timeout_s, args.poll_interval_s)
            current = resolve_pane(snapshot.pane.target)
            if current.pane_id != snapshot.pane.pane_id or current.window_id != snapshot.pane.window_id:
                raise ReplaceError("target drift after Cursor startup")
            if current.pane_pid == snapshot.pane.pane_pid:
                raise ReplaceError("pane process identity did not change during replacement")
            updated = replace_frontmatter_fields(snapshot.task_text, {"tool": "cursor"})
            replace_if_unchanged(snapshot.task_path, updated, snapshot.task_stat)
            metadata = parse_task_metadata(snapshot.task_path.read_text(encoding="utf-8"), args.root)
            if metadata is None or metadata.tool != "cursor":
                raise ReplaceError("ops_manager.md tool did not become cursor")
            if metadata.pending_task_items != snapshot.metadata.pending_task_items:
                raise ReplaceError("pending queue was not preserved")
            if not same_tmux_target(metadata.managerat, REQUIRED_MANAGERAT) or not same_tmux_target(metadata.runat, REQUIRED_TARGET) or not metadata.is_manager:
                raise ReplaceError("manager record identity was not preserved")
            verify_children(args.root, snapshot.children)
        except (ReplaceError, StartError, OSError, TaskFrontmatterError) as error:
            message = str(error)
            if replaced:
                raise ReplaceError(f"completion-unknown after pane replacement: {message}") from error
            if isinstance(error, ReplaceError):
                raise
            raise ReplaceError(message) from error
        return f"replaced: {TASK_NAME} tool is now cursor at {snapshot.pane.target}; pending queue and managerat preserved"


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        print(replace_manager(args))
    except ReplaceError as error:
        print(f"omo_ops_manager_cursor_replace.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
