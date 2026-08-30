#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Prepare one ordinary worker successor without launching it.

This helper serializes supported lifecycle writers.  It does not claim
atomicity against arbitrary processes that ignore the work-log locks.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import pwd
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_hees_final_artifact_replace import ReplaceError as HeesReplaceError
from omo_manager.omo_hees_final_artifact_replace import active_owners, has_pending_marker
from omo_manager.omo_manager_replace import (
    ReplaceError as ManagerReplaceError,
    Snapshot,
    canonical_target,
    create_snapshot,
    digest,
    markdown_paths,
    metadata,
    pane_inventory,
    read_snapshot,
    replace_snapshot,
    replace_v1_fields,
    task_path,
    target_session,
    todo_replacement,
)
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths, root_membership_lock, update_frontmatter_status

VERSION = "v1.0.0"
OPERATION = "ordinary-worker-successor"
BLOCKER = "prepared successor awaiting exact digest-bound supported launch"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^[A-Za-z0-9_./-]+\.md$")
TOOL_RE = re.compile(r"^(?:codex|pcodx|cursor)$")
JOURNAL_RE = re.compile(r"^\.omo-worker-successor-[0-9a-f]{16,64}\.transaction$")
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
CRASH_PHASES = ("prepared", "old", "todo", "successor", "committed")
LAUNCH_MANIFEST_VERSION = "v1.0.0"
DEFAULT_WORKER_INSTRUCTIONS = Path(__file__).with_name("WORKER_DEFAULTS.md")


class SuccessorError(RuntimeError):
    """The prepared-successor operation failed closed."""


@dataclass(frozen=True)
class Args:
    root: Path
    old_task: str
    successor_task: str
    target: str
    manager_target: str
    tool: str
    old_sha256: str
    todo_sha256: str
    expected_pending_items: tuple[str, ...]
    queue_sha256: str
    prompt_file: Path
    prompt_sha256: str
    protected_targets: tuple[str, ...]
    protected_sha256: str
    journal: Path
    launch_manifest: Path
    launch_manifest_sha256: str


@dataclass(frozen=True)
class Plan:
    old: Snapshot
    todo: Snapshot
    prompt: Snapshot
    launch_manifest: Snapshot
    successor_path: Path
    old_after: bytes
    todo_after: bytes
    successor_data: bytes
    initial_markdown_paths: tuple[Path, ...]


@dataclass(frozen=True)
class Binding:
    journal: Snapshot
    root: Path
    old_path: Path
    successor_path: Path
    todo_path: Path
    target: str
    manager_target: str
    tool: str
    prompt_path: Path
    prompt_data: bytes
    prompt_sha256: str
    queue: tuple[str, ...]
    queue_sha256: str
    protected_targets: tuple[str, ...]
    protected_sha256: str
    old_before: bytes
    old_after: bytes
    todo_before: bytes
    todo_after: bytes
    successor_data: bytes
    launch_manifest_path: Path
    launch_manifest_data: bytes
    launch_manifest_sha256: str
    launch_config: dict[str, object]
    phase: str


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    old_task: str = ""
    successor_task: str = ""
    target: str = ""
    manager_target: str = ""
    tool: str = ""
    old_sha256: str = ""
    todo_sha256: str = ""
    expected_pending_item: list[str] = []
    queue_sha256: str = ""
    prompt_file: Path = Path()
    prompt_sha256: str = ""
    protected_target: list[str] = []
    protected_sha256: str = ""
    journal: Path = Path()
    launch_manifest: Path = Path()
    launch_manifest_sha256: str = ""


def queue_digest(items: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(items).encode()).hexdigest()


def protected_digest(targets: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(targets).encode()).hexdigest()


def read_frozen_prompt(path: Path) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SuccessorError(f"frozen successor prompt is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            raise SuccessorError("frozen successor prompt must be an owner-owned regular file with no group/world write")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after:
        raise SuccessorError("frozen successor prompt changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def read_launch_manifest(path: Path) -> Snapshot:
    snapshot = read_frozen_prompt(path)
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or snapshot.state.st_uid != os.getuid():
        raise SuccessorError("launch manifest must be owner-owned regular mode 0600")
    return snapshot


def read_pinned_system_executable(path: Path, label: str) -> Snapshot:
    resolved = path.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise SuccessorError(f"pinned {label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.getuid()}
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
        ):
            raise SuccessorError(f"pinned {label} must be a root/owner-owned non-writable executable regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_identity != after_identity:
        raise SuccessorError(f"pinned {label} changed while it was read")
    return Snapshot(resolved, b"".join(chunks), after)


def cursor_runtime_identity() -> dict[str, object]:
    """Return the exact installed Cursor launcher/runtime identity used by prepared launches."""

    account = pwd.getpwuid(os.getuid())
    launcher = Path(account.pw_dir) / ".local/bin/agent"
    try:
        resolved = launcher.resolve(strict=True)
    except OSError as exc:
        raise SuccessorError(f"installed Cursor Agent CLI cannot be resolved: {exc}") from exc
    node = resolved.with_name("node")
    index = resolved.with_name("index.js")
    for path, label, executable in (
        (resolved, "Cursor launcher", True),
        (node, "Cursor Node runtime", True),
        (index, "Cursor program", False),
    ):
        try:
            snapshot = read_frozen_prompt(path)
        except OSError as exc:
            raise SuccessorError(f"installed {label} is unavailable: {exc}") from exc
        if executable and not os.access(path, os.X_OK):
            raise SuccessorError(f"installed {label} is not one owner-owned usable regular file")
        if not snapshot.data:
            raise SuccessorError(f"installed {label} is empty")
    return {
        "launcher_path": str(launcher),
        "launcher_resolved": str(resolved),
        "launcher_sha256": digest(read_frozen_prompt(resolved).data),
        "node_path": str(node),
        "node_sha256": digest(read_frozen_prompt(node).data),
        "index_path": str(index),
        "index_sha256": digest(read_frozen_prompt(index).data),
        "version": resolved.parent.name,
    }


def minimal_launch_environment() -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    return {
        "HOME": account.pw_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LOGNAME": account.pw_name,
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "USER": account.pw_name,
    }


def pinned_shell_identity() -> dict[str, str]:
    shell = Path("/bin/bash").resolve(strict=True)
    env = Path("/usr/bin/env").resolve(strict=True)
    shell_snapshot = read_pinned_system_executable(shell, "bash")
    env_snapshot = read_pinned_system_executable(env, "env")
    return {
        "bash_path": str(shell),
        "bash_sha256": digest(shell_snapshot.data),
        "env_path": str(env),
        "env_sha256": digest(env_snapshot.data),
    }


def pinned_tmux_identity() -> dict[str, str]:
    """Bind the installed tmux client without consulting caller-controlled PATH."""

    account = pwd.getpwuid(os.getuid())
    launcher = Path(account.pw_dir) / ".nix-profile/bin/tmux"
    try:
        resolved = launcher.resolve(strict=True)
    except OSError as exc:
        raise SuccessorError(f"installed tmux client cannot be resolved: {exc}") from exc
    snapshot = read_pinned_system_executable(resolved, "tmux client")
    return {
        "tmux_path": str(resolved),
        "tmux_sha256": digest(snapshot.data),
    }


def minimal_tmux_environment() -> dict[str, str]:
    """Return the exact environment allowed to reach prepared tmux clients."""

    environment = minimal_launch_environment()
    environment["PWD"] = "/"
    return environment


def launch_manifest_bytes(
    *,
    root: Path,
    task_file: str,
    target: str,
    manager_target: str,
    tool: str,
    workdir: Path,
    model: str,
    reasoning_effort: str,
    window_name: str = "",
    codex_flags: tuple[str, ...] = (),
    amh_caller_agent: str = "",
) -> bytes:
    """Build canonical bytes binding every supported prepared-launch input."""

    canonical_root = root.resolve(strict=True)
    canonical_workdir = workdir.resolve(strict=True)
    canonical_worker = canonical_target(target)
    session, window_pane = canonical_worker.split(":", 1)
    window, _dot, pane = window_pane.partition(".")
    if pane != "0":
        raise SuccessorError("prepared successor launch requires pane zero of one exact tmux window")
    if tool != "cursor":
        raise SuccessorError("prepared successor launch currently requires the pinned installed Cursor runtime")
    defaults = read_frozen_prompt(DEFAULT_WORKER_INSTRUCTIONS)
    value = {
        "version": LAUNCH_MANIFEST_VERSION,
        "root": str(canonical_root),
        "task_file": task_file,
        "target": canonical_worker,
        "manager_target": canonical_target(manager_target),
        "tool": tool,
        "tmux_session": session,
        "tmux_window": window,
        "workdir": str(canonical_workdir),
        "window_name": window_name,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "codex_flags": list(codex_flags),
        "amh_caller_agent": amh_caller_agent,
        "prelaunch_source": None,
        "environment": minimal_launch_environment(),
        "shell_runtime": pinned_shell_identity(),
        "tmux_environment": minimal_tmux_environment(),
        "tmux_runtime": pinned_tmux_identity(),
        "worker_defaults_path": str(DEFAULT_WORKER_INSTRUCTIONS.resolve(strict=True)),
        "worker_defaults_sha256": digest(defaults.data),
        "cursor_runtime": cursor_runtime_identity(),
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validated_launch_config(
    data: bytes,
    *,
    expected_sha256: str,
    root: Path,
    task_file: str,
    target: str,
    manager_target: str,
    tool: str,
) -> dict[str, object]:
    if digest(data) != expected_sha256:
        raise SuccessorError("launch manifest digest changed")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuccessorError(f"launch manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SuccessorError("launch manifest must contain one object")
    try:
        expected_bytes = launch_manifest_bytes(
            root=root,
            task_file=task_file,
            target=target,
            manager_target=manager_target,
            tool=tool,
            workdir=Path(str(value.get("workdir", ""))),
            model=str(value.get("model", "")),
            reasoning_effort=str(value.get("reasoning_effort", "")),
            window_name=str(value.get("window_name", "")),
            codex_flags=tuple(value.get("codex_flags", ())) if isinstance(value.get("codex_flags"), list) else ("<invalid>",),
            amh_caller_agent=str(value.get("amh_caller_agent", "")),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, SuccessorError):
            raise
        raise SuccessorError(f"launch manifest contains an unusable path or value: {exc}") from exc
    if data != expected_bytes:
        raise SuccessorError("launch manifest is not the canonical exact supported launch binding")
    workdir = Path(str(value["workdir"]))
    if workdir.resolve(strict=True) != workdir or not workdir.is_dir() or workdir.is_symlink():
        raise SuccessorError("launch workdir must be one existing canonical non-symlink directory")
    if not str(value["model"]) or not str(value["reasoning_effort"]):
        raise SuccessorError("launch model and reasoning effort must be nonempty")
    if value["codex_flags"]:
        raise SuccessorError("prepared Cursor launch rejects caller-controlled Codex flags")
    caller = str(value["amh_caller_agent"])
    if caller and re.fullmatch(r"[A-Za-z0-9._-]{1,255}", caller) is None:
        raise SuccessorError("launch AMH caller id is invalid")
    return value


def parse_launch_manifest(snapshot: Snapshot, args: Args) -> dict[str, object]:
    return validated_launch_config(
        snapshot.data,
        expected_sha256=args.launch_manifest_sha256,
        root=args.root,
        task_file=args.successor_task,
        target=args.target,
        manager_target=args.manager_target,
        tool=args.tool,
    )


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--old-task", required=True)
    _ = parser.add_argument("--successor-task", required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--manager-target", required=True)
    _ = parser.add_argument("--tool", required=True)
    _ = parser.add_argument("--old-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--expected-pending-item", action="append", required=True)
    _ = parser.add_argument("--queue-sha256", required=True)
    _ = parser.add_argument("--prompt-file", type=Path, required=True)
    _ = parser.add_argument("--prompt-sha256", required=True)
    _ = parser.add_argument("--protected-target", action="append", required=True)
    _ = parser.add_argument("--protected-sha256", required=True)
    _ = parser.add_argument("--journal", type=Path, required=True)
    _ = parser.add_argument("--launch-manifest", type=Path, required=True)
    _ = parser.add_argument("--launch-manifest-sha256", required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    for value, label in (
        (parsed.old_sha256, "old task"),
        (parsed.todo_sha256, "TODO"),
        (parsed.queue_sha256, "queue"),
        (parsed.prompt_sha256, "prompt"),
        (parsed.protected_sha256, "protected target set"),
        (parsed.launch_manifest_sha256, "launch manifest"),
    ):
        if SHA256_RE.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    if TASK_RE.fullmatch(parsed.old_task) is None or TASK_RE.fullmatch(parsed.successor_task) is None:
        parser.error("old and successor tasks must be safe Markdown task references")
    if parsed.old_task == parsed.successor_task:
        parser.error("old and successor tasks must differ")
    if TOOL_RE.fullmatch(parsed.tool) is None:
        parser.error("--tool must be codex, pcodx, or cursor")
    if not parsed.expected_pending_item or any(not item or "\0" in item for item in parsed.expected_pending_item):
        parser.error("the exact ordered queue must be nonempty and contain no NUL")
    queue = tuple(parsed.expected_pending_item)
    if queue_digest(queue) != parsed.queue_sha256:
        parser.error("ordered queue does not match --queue-sha256")
    try:
        target = canonical_target(parsed.target)
        manager_target = canonical_target(parsed.manager_target)
        protected = tuple(canonical_target(item) for item in parsed.protected_target)
    except SuccessorError:
        raise
    except Exception as exc:
        parser.error(str(exc))
    if target == manager_target:
        parser.error("worker target and manager target must differ")
    if target_session(target).startswith("h") or target_session(manager_target).startswith("h"):
        parser.error("prepared worker successors cannot use human-owned h* targets")
    if len(set(protected)) != len(protected) or tuple(sorted(protected)) != protected:
        parser.error("protected targets must be unique and supplied in canonical sorted order")
    if target in protected:
        parser.error("worker target aliases the protected set")
    if protected_digest(protected) != parsed.protected_sha256:
        parser.error("protected targets do not match --protected-sha256")
    root = parsed.root.expanduser().resolve(strict=False)
    prompt = Path(os.path.abspath(parsed.prompt_file.expanduser()))
    journal = Path(os.path.abspath(parsed.journal.expanduser()))
    launch_manifest = Path(os.path.abspath(parsed.launch_manifest.expanduser()))
    if journal.parent != root or JOURNAL_RE.fullmatch(journal.name) is None:
        parser.error("journal must be a canonical .omo-worker-successor-HEX.transaction child of ROOT")
    if launch_manifest == journal or launch_manifest.parent != root:
        parser.error("launch manifest must be a distinct canonical direct child of ROOT")
    return Args(
        root,
        parsed.old_task,
        parsed.successor_task,
        target,
        manager_target,
        parsed.tool,
        parsed.old_sha256,
        parsed.todo_sha256,
        queue,
        parsed.queue_sha256,
        prompt,
        parsed.prompt_sha256,
        protected,
        parsed.protected_sha256,
        journal,
        launch_manifest,
        parsed.launch_manifest_sha256,
    )


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise SuccessorError(f"journal {label} is not text")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise SuccessorError(f"journal {label} is not canonical base64") from exc
    if encoded(data) != value:
        raise SuccessorError(f"journal {label} is not canonical base64")
    return data


def task_body(data: bytes) -> tuple[str, str]:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise SuccessorError(f"task is not UTF-8: {exc}") from exc
    lines = text.splitlines(keepends=True)
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise SuccessorError("task frontmatter is unterminated") from exc
    return text, "".join(lines[closing + 1 :])


def successor_text(old_data: bytes, old_metadata: TaskMetadata, args: Args, prompt: bytes) -> tuple[bytes, bytes]:
    old_text, body = task_body(old_data)
    cleared = render_pending_items(old_text, ())
    old_after = update_frontmatter_status(cleared, "done", "", args.root)
    successor = replace_v1_fields(old_text, status="blocked", runat=args.target, blocked_on=BLOCKER, remove_session=True)
    successor = render_pending_items(successor, args.expected_pending_items)
    try:
        prompt_text = prompt.decode()
    except UnicodeDecodeError as exc:
        raise SuccessorError(f"prompt is not UTF-8: {exc}") from exc
    if "</manager_delegation>" in prompt_text.casefold():
        raise SuccessorError("prompt contains a reserved manager-delegation closing tag")
    marker = (
        f'<prepared_worker_successor version="{VERSION}" prompt-sha256="{args.prompt_sha256}" '
        f'queue-sha256="{args.queue_sha256}" manager="{html.escape(args.manager_target, quote=True)}" />'
    )
    delegation = (
        f'<manager_delegation from="{html.escape(args.manager_target, quote=True)}">\n'
        f"{prompt_text.rstrip()}\n"
        "</manager_delegation>"
    )
    ending = "" if successor.endswith("\n") else "\n"
    successor = f"{successor}{ending}{marker}\n{delegation}\n"
    old_after_metadata = parse_task_metadata(old_after, args.root)
    successor_metadata = parse_task_metadata(successor, args.root)
    if old_after_metadata is None or old_after_metadata.status != "done" or old_after_metadata.pending_task_items:
        raise SuccessorError("old task did not become one empty done record")
    if (
        successor_metadata is None
        or successor_metadata.status != "blocked"
        or successor_metadata.blocked_on != BLOCKER
        or successor_metadata.pending_task_items != args.expected_pending_items
        or successor_metadata.runat != args.target
        or successor_metadata.managerat != args.manager_target
        or successor_metadata.tool != args.tool
        or successor_metadata.is_manager
        or successor_metadata.session_id
    ):
        raise SuccessorError("successor construction lost its exact lifecycle binding")
    if old_metadata.pending_task_items != args.expected_pending_items:
        raise SuccessorError("old task queue drifted")
    return old_after.encode(), successor.encode()


def require_root(args: Args) -> None:
    if args.root.resolve(strict=True) != args.root or not args.root.is_dir() or args.root.is_symlink():
        raise SuccessorError("root must be one existing canonical non-symlink directory")
    if args.journal.parent != args.root:
        raise SuccessorError("journal escaped the canonical root")
    if args.launch_manifest.parent != args.root or args.launch_manifest == args.journal:
        raise SuccessorError("launch manifest escaped the canonical root or aliases the journal")


def prepare(args: Args) -> Plan:
    require_root(args)
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    todo_path = args.root / "TODO.md"
    if successor_path.exists() or successor_path.is_symlink():
        raise SuccessorError("successor task already exists; no-replace publication is required")
    old = read_snapshot(old_path, "old worker task")
    todo = read_snapshot(todo_path, "TODO")
    prompt = read_frozen_prompt(args.prompt_file)
    launch_manifest = read_launch_manifest(args.launch_manifest)
    if digest(old.data) != args.old_sha256 or digest(todo.data) != args.todo_sha256:
        raise SuccessorError("old task or TODO digest changed")
    if digest(prompt.data) != args.prompt_sha256:
        raise SuccessorError("frozen prompt digest changed")
    _ = parse_launch_manifest(launch_manifest, args)
    old_metadata = metadata(old.data, args.root, "old worker task")
    if (
        old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status == "done"
        or old_metadata.is_manager
        or canonical_target(old_metadata.runat) != args.target
        or canonical_target(old_metadata.managerat) != args.manager_target
        or old_metadata.tool != args.tool
        or old_metadata.pending_task_items != args.expected_pending_items
        or queue_digest(old_metadata.pending_task_items) != args.queue_sha256
    ):
        raise SuccessorError("old worker does not match the exact role, owner, tool, target, and ordered queue binding")
    try:
        old_text = old.data.decode()
    except UnicodeDecodeError as exc:
        raise SuccessorError(f"old worker task is not UTF-8: {exc}") from exc
    if has_pending_marker(old_text):
        raise SuccessorError("old worker has a live pending-delivery marker")
    if pane_inventory().get(args.target) is not None:
        raise SuccessorError("worker target is live; preparation requires an already-stopped owner")
    if (
        authoritative_active_target_task_paths(args.root, args.target) != (old_path.resolve(),)
        or active_owners(args.root, args.target, {}) != (old_path.resolve(),)
    ):
        raise SuccessorError("old worker is not the sole authoritative active owner")
    old_after, successor_data = successor_text(old.data, old_metadata, args, prompt.data)
    todo_after = todo_replacement(todo.data, args.root, old_path, successor_path, args.target, args.target)
    overrides = {old_path.resolve(): old_after, successor_path.resolve(strict=False): successor_data}
    owners = active_owners(args.root, args.target, overrides)
    if owners != (successor_path.resolve(strict=False),):
        raise SuccessorError("candidate state does not have exactly one prepared successor owner")
    return Plan(old, todo, prompt, launch_manifest, successor_path, old_after, todo_after, successor_data, markdown_paths(args.root))


def binding_fields(args: Args) -> dict[str, object]:
    return {
        "version": VERSION,
        "operation": OPERATION,
        "root": str(args.root),
        "old_task": args.old_task,
        "successor_task": args.successor_task,
        "target": args.target,
        "manager_target": args.manager_target,
        "tool": args.tool,
        "old_sha256": args.old_sha256,
        "todo_sha256": args.todo_sha256,
        "queue": list(args.expected_pending_items),
        "queue_sha256": args.queue_sha256,
        "prompt_path": str(args.prompt_file),
        "prompt_sha256": args.prompt_sha256,
        "protected_targets": list(args.protected_targets),
        "protected_sha256": args.protected_sha256,
        "journal": str(args.journal),
        "launch_manifest_path": str(args.launch_manifest),
        "launch_manifest_sha256": args.launch_manifest_sha256,
    }


def journal_record(args: Args, plan: Plan, phase: str) -> dict[str, object]:
    record = {
        **binding_fields(args),
        "phase": phase,
        "old_before": encoded(plan.old.data),
        "old_after": encoded(plan.old_after),
        "todo_before": encoded(plan.todo.data),
        "todo_after": encoded(plan.todo_after),
        "successor_data": encoded(plan.successor_data),
        "prompt_data": encoded(plan.prompt.data),
        "launch_manifest_data": encoded(plan.launch_manifest.data),
        "old_mode": stat.S_IMODE(plan.old.state.st_mode),
        "old_gid": plan.old.state.st_gid,
        "todo_mode": stat.S_IMODE(plan.todo.state.st_mode),
        "todo_gid": plan.todo.state.st_gid,
        "initial_markdown_paths": [str(path) for path in plan.initial_markdown_paths],
    }
    record["commitment_sha256"] = digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    return record


def serialized(record: dict[str, object]) -> bytes:
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_JOURNAL_BYTES:
        raise SuccessorError("transaction journal exceeds the size bound")
    return data


def parse_record(snapshot: Snapshot, args: Args) -> dict[str, object]:
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or snapshot.state.st_uid != os.getuid():
        raise SuccessorError("journal must be owner-private mode 0600")
    try:
        value = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuccessorError(f"journal is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SuccessorError("journal must contain one object")
    expected = binding_fields(args)
    if any(value.get(key) != item for key, item in expected.items()):
        raise SuccessorError("journal invocation binding changed")
    phase = value.get("phase")
    if phase not in CRASH_PHASES:
        raise SuccessorError("journal phase is invalid")
    commitment = value.get("commitment_sha256")
    unsigned = {key: item for key, item in value.items() if key != "commitment_sha256"}
    if not isinstance(commitment, str) or digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()) != commitment:
        raise SuccessorError("journal commitment does not match")
    return value


def plan_from_record(args: Args, snapshot: Snapshot, record: dict[str, object]) -> Plan:
    old_path = task_path(args.root, args.old_task)
    todo_path = args.root / "TODO.md"
    prompt_data = decoded(record.get("prompt_data"), "prompt_data")
    old_before = decoded(record.get("old_before"), "old_before")
    todo_before = decoded(record.get("todo_before"), "todo_before")
    old_state = os.stat_result((stat.S_IFREG | int(record["old_mode"]), 0, 0, 0, os.getuid(), int(record["old_gid"]), len(old_before), 0, 0, 0))
    todo_state = os.stat_result((stat.S_IFREG | int(record["todo_mode"]), 0, 0, 0, os.getuid(), int(record["todo_gid"]), len(todo_before), 0, 0, 0))
    paths_raw = record.get("initial_markdown_paths")
    if not isinstance(paths_raw, list) or not all(isinstance(path, str) for path in paths_raw):
        raise SuccessorError("journal Markdown inventory is invalid")
    plan = Plan(
        Snapshot(old_path, old_before, old_state),
        Snapshot(todo_path, todo_before, todo_state),
        Snapshot(args.prompt_file, prompt_data, snapshot.state),
        Snapshot(args.launch_manifest, decoded(record.get("launch_manifest_data"), "launch_manifest_data"), snapshot.state),
        task_path(args.root, args.successor_task),
        decoded(record.get("old_after"), "old_after"),
        decoded(record.get("todo_after"), "todo_after"),
        decoded(record.get("successor_data"), "successor_data"),
        tuple(Path(path) for path in paths_raw),
    )
    old_metadata = metadata(old_before, args.root, "journaled old worker task")
    canonical_old_after, canonical_successor = successor_text(old_before, old_metadata, args, prompt_data)
    canonical_todo_after = todo_replacement(todo_before, args.root, old_path, plan.successor_path, args.target, args.target)
    if (
        plan.old_after != canonical_old_after
        or plan.successor_data != canonical_successor
        or plan.todo_after != canonical_todo_after
        or digest(old_before) != args.old_sha256
        or digest(todo_before) != args.todo_sha256
        or digest(prompt_data) != args.prompt_sha256
        or digest(plan.launch_manifest.data) != args.launch_manifest_sha256
    ):
        raise SuccessorError("journal after-state is not the canonical reconstruction from its bound before-state")
    _ = parse_launch_manifest(plan.launch_manifest, args)
    return plan


def transition(snapshot: Snapshot, args: Args, plan: Plan, phase: str) -> Snapshot:
    return replace_snapshot(snapshot, serialized(journal_record(args, plan, phase)), "worker-successor journal")


def maybe_crash(phase: str) -> None:
    if os.environ.get("OMO_WORKER_SUCCESSOR_CRASH_AFTER") == phase:
        os._exit(86)


def current_bytes(path: Path, absent_ok: bool = False) -> bytes | None:
    if absent_ok and not path.exists() and not path.is_symlink():
        return None
    return read_snapshot(path, path.name).data


def prove_final(args: Args, plan: Plan, journal: Snapshot) -> None:
    old = read_snapshot(plan.old.path, "final old worker")
    todo = read_snapshot(plan.todo.path, "final TODO")
    successor = read_snapshot(plan.successor_path, "final successor")
    if old.data != plan.old_after or todo.data != plan.todo_after or successor.data != plan.successor_data:
        raise SuccessorError("final lifecycle bytes do not match the journal")
    if read_frozen_prompt(args.prompt_file).data != plan.prompt.data:
        raise SuccessorError("frozen prompt changed before final successor proof")
    if read_launch_manifest(args.launch_manifest).data != plan.launch_manifest.data:
        raise SuccessorError("frozen launch manifest changed before final successor proof")
    record = parse_record(journal, args)
    if record["phase"] != "committed":
        raise SuccessorError("final journal is not committed")
    successor_metadata = metadata(successor.data, args.root, "final successor")
    if successor_metadata.status != "blocked" or successor_metadata.pending_task_items != args.expected_pending_items:
        raise SuccessorError("final successor is not blocked with the exact nonempty queue")
    if queue_digest(successor_metadata.pending_task_items) != args.queue_sha256:
        raise SuccessorError("final successor queue digest changed")
    if (
        authoritative_active_target_task_paths(args.root, args.target) != (plan.successor_path.resolve(),)
        or active_owners(args.root, args.target, {}) != (plan.successor_path.resolve(),)
    ):
        raise SuccessorError("final state does not have exactly one authoritative successor owner")
    if pane_inventory().get(args.target) is not None:
        raise SuccessorError("successor target became live before prepared launch")
    expected_paths = tuple(sorted((*plan.initial_markdown_paths, plan.successor_path.resolve()), key=str))
    if markdown_paths(args.root) != expected_paths:
        raise SuccessorError("Markdown membership changed during successor preparation")


def apply_plan(args: Args, plan: Plan, journal: Snapshot) -> Snapshot:
    if read_frozen_prompt(args.prompt_file).data != plan.prompt.data or digest(plan.prompt.data) != args.prompt_sha256:
        raise SuccessorError("frozen prompt changed during transaction recovery")
    if read_launch_manifest(args.launch_manifest).data != plan.launch_manifest.data:
        raise SuccessorError("frozen launch manifest changed during transaction recovery")
    old_data = current_bytes(plan.old.path)
    todo_data = current_bytes(plan.todo.path)
    successor_data = current_bytes(plan.successor_path, absent_ok=True)
    if old_data not in {plan.old.data, plan.old_after}:
        raise SuccessorError("recovery found unknown old-task bytes")
    if todo_data not in {plan.todo.data, plan.todo_after}:
        raise SuccessorError("recovery found unknown TODO bytes")
    if successor_data not in {None, plan.successor_data}:
        raise SuccessorError("recovery found an unknown successor path")
    if pane_inventory().get(args.target) is not None:
        raise SuccessorError("target became live during prepared-successor recovery")
    if old_data == plan.old.data:
        updated_old = replace_snapshot(read_snapshot(plan.old.path, "old worker"), plan.old_after, "old worker")
        if updated_old.data != plan.old_after:
            raise SuccessorError("old worker replacement did not commit")
    journal = transition(journal, args, plan, "old")
    maybe_crash("old")
    if todo_data == plan.todo.data:
        updated_todo = replace_snapshot(read_snapshot(plan.todo.path, "TODO"), plan.todo_after, "TODO")
        if updated_todo.data != plan.todo_after:
            raise SuccessorError("TODO replacement did not commit")
    journal = transition(journal, args, plan, "todo")
    maybe_crash("todo")
    if successor_data is None:
        successor = create_snapshot(
            plan.successor_path,
            plan.successor_data,
            stat.S_IMODE(plan.old.state.st_mode),
            plan.old.state.st_gid,
        )
        if successor.data != plan.successor_data:
            raise SuccessorError("successor publication did not commit")
    journal = transition(journal, args, plan, "successor")
    maybe_crash("successor")
    journal = transition(journal, args, plan, "committed")
    maybe_crash("committed")
    prove_final(args, plan, journal)
    return journal


def prepare_successor(args: Args) -> str:
    require_root(args)
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    todo_path = args.root / "TODO.md"
    prompt_path = args.prompt_file
    with ExitStack() as locks:
        locks.enter_context(root_membership_lock(args.root))
        locks.enter_context(task_target_lock(args.root, args.target))
        for path in sorted((old_path, successor_path, todo_path, prompt_path, args.launch_manifest, args.journal), key=str):
            locks.enter_context(task_file_lock(path))
        if args.journal.exists() or args.journal.is_symlink():
            journal = read_snapshot(args.journal, "worker-successor journal")
            record = parse_record(journal, args)
            plan = plan_from_record(args, journal, record)
            if digest(plan.prompt.data) != args.prompt_sha256:
                raise SuccessorError("journal prompt bytes do not match their bound digest")
        else:
            plan = prepare(args)
            journal = create_snapshot(args.journal, serialized(journal_record(args, plan, "prepared")), 0o600)
        maybe_crash("prepared")
        journal = apply_plan(args, plan, journal)
    return (
        f"prepared blocked successor {args.successor_task} for {args.target}; "
        f"task-sha256={digest(plan.successor_data)}; prompt-sha256={args.prompt_sha256}; "
        f"queue-sha256={args.queue_sha256}; launch-manifest-sha256={args.launch_manifest_sha256}; "
        f"journal-sha256={digest(journal.data)}"
    )


def binding_from_committed_journal(
    journal_path: Path,
    *,
    expected_journal_sha256: str,
    expected_task_sha256: str,
    expected_prompt_sha256: str,
    expected_queue_sha256: str,
    expected_launch_manifest_sha256: str,
    verify_prelaunch_state: bool = True,
) -> Binding:
    for value, label in (
        (expected_journal_sha256, "journal"),
        (expected_task_sha256, "task"),
        (expected_prompt_sha256, "prompt"),
        (expected_queue_sha256, "queue"),
        (expected_launch_manifest_sha256, "launch manifest"),
    ):
        if SHA256_RE.fullmatch(value) is None:
            raise SuccessorError(f"expected {label} SHA-256 is invalid")
    journal = read_snapshot(journal_path, "committed worker-successor journal")
    if stat.S_IMODE(journal.state.st_mode) != 0o600 or journal.state.st_uid != os.getuid():
        raise SuccessorError("committed journal must remain owner-private mode 0600")
    if digest(journal.data) != expected_journal_sha256:
        raise SuccessorError("committed journal digest changed")
    try:
        record = json.loads(journal.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuccessorError(f"committed journal is invalid: {exc}") from exc
    if not isinstance(record, dict) or record.get("phase") != "committed" or record.get("version") != VERSION or record.get("operation") != OPERATION:
        raise SuccessorError("journal is not one committed worker-successor transaction")
    commitment = record.get("commitment_sha256")
    unsigned = {key: item for key, item in record.items() if key != "commitment_sha256"}
    if not isinstance(commitment, str) or digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()) != commitment:
        raise SuccessorError("committed journal integrity check failed")
    root = Path(str(record["root"]))
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise SuccessorError(f"committed journal root is unavailable: {exc}") from exc
    canonical_journal = journal_path.absolute()
    if (
        canonical_root != root
        or root.is_symlink()
        or not root.is_dir()
        or journal_path != canonical_journal
        or journal_path.is_symlink()
        or journal_path.parent != root
        or JOURNAL_RE.fullmatch(journal_path.name) is None
        or str(record.get("journal")) != str(journal_path)
    ):
        raise SuccessorError("journal and launcher must share one canonical root/direct-child lock namespace")
    old_path = task_path(root, str(record["old_task"]))
    successor_path = task_path(root, str(record["successor_task"]))
    todo_path = root / "TODO.md"
    successor_data = decoded(record.get("successor_data"), "successor_data")
    prompt_data = decoded(record.get("prompt_data"), "prompt_data")
    launch_manifest_data = decoded(record.get("launch_manifest_data"), "launch_manifest_data")
    queue_raw = record.get("queue")
    protected_raw = record.get("protected_targets")
    if not isinstance(queue_raw, list) or not queue_raw or not all(isinstance(item, str) and item for item in queue_raw):
        raise SuccessorError("committed journal queue is invalid")
    if not isinstance(protected_raw, list) or not all(isinstance(item, str) for item in protected_raw):
        raise SuccessorError("committed journal protected set is invalid")
    queue = tuple(queue_raw)
    protected = tuple(protected_raw)
    if (
        digest(successor_data) != expected_task_sha256
        or digest(prompt_data) != expected_prompt_sha256
        or queue_digest(queue) != expected_queue_sha256
        or record.get("prompt_sha256") != expected_prompt_sha256
        or record.get("queue_sha256") != expected_queue_sha256
        or protected_digest(protected) != record.get("protected_sha256")
        or digest(launch_manifest_data) != expected_launch_manifest_sha256
        or record.get("launch_manifest_sha256") != expected_launch_manifest_sha256
    ):
        raise SuccessorError("prepared successor task, prompt, queue, protected-set, or launch-manifest binding changed")
    prompt_path = Path(str(record["prompt_path"]))
    current_prompt = read_frozen_prompt(prompt_path)
    if current_prompt.data != prompt_data:
        raise SuccessorError("frozen successor prompt was swapped after preparation")
    launch_manifest_path = Path(str(record.get("launch_manifest_path", "")))
    if launch_manifest_path.parent != root or launch_manifest_path == journal_path:
        raise SuccessorError("launch manifest is not a distinct direct child of the committed root")
    current_launch_manifest = read_launch_manifest(launch_manifest_path)
    if current_launch_manifest.data != launch_manifest_data:
        raise SuccessorError("frozen launch manifest was swapped after preparation")
    old_after = decoded(record.get("old_after"), "old_after")
    todo_after = decoded(record.get("todo_after"), "todo_after")
    if read_snapshot(old_path, "superseded worker task").data != old_after or read_snapshot(todo_path, "prepared TODO").data != todo_after:
        raise SuccessorError("superseded worker or TODO bytes changed")
    target = canonical_target(str(record["target"]))
    launch_config = validated_launch_config(
        launch_manifest_data,
        expected_sha256=expected_launch_manifest_sha256,
        root=root,
        task_file=str(record["successor_task"]),
        target=target,
        manager_target=canonical_target(str(record["manager_target"])),
        tool=str(record["tool"]),
    )
    if verify_prelaunch_state:
        task = read_snapshot(successor_path, "prepared successor task")
        if task.data != successor_data:
            raise SuccessorError("prepared successor task bytes changed")
        task_metadata = metadata(task.data, root, "prepared successor task")
        if task_metadata.status != "blocked" or task_metadata.pending_task_items != queue:
            raise SuccessorError("prepared successor is not blocked with the exact queue")
        if (
            authoritative_active_target_task_paths(root, target) != (successor_path.resolve(),)
            or active_owners(root, target, {}) != (successor_path.resolve(),)
        ):
            raise SuccessorError("prepared successor is not the sole authoritative owner")
        if pane_inventory().get(target) is not None:
            raise SuccessorError("prepared successor target already exists")
    return Binding(
        journal,
        root,
        old_path,
        successor_path,
        todo_path,
        target,
        canonical_target(str(record["manager_target"])),
        str(record["tool"]),
        prompt_path,
        prompt_data,
        expected_prompt_sha256,
        queue,
        expected_queue_sha256,
        protected,
        str(record["protected_sha256"]),
        decoded(record.get("old_before"), "old_before"),
        old_after,
        decoded(record.get("todo_before"), "todo_before"),
        todo_after,
        successor_data,
        launch_manifest_path,
        launch_manifest_data,
        expected_launch_manifest_sha256,
        launch_config,
        "committed",
    )


def reserve_launch_receipt(binding: Binding, suffix: str, fields: tuple[str, ...]) -> Snapshot:
    receipt = binding.journal.path.with_name(f".{binding.journal.path.name}.{suffix}")
    data = ("\n".join(fields) + "\n").encode()
    return create_snapshot(receipt, data, 0o600)


def main(argv: list[str] | None = None) -> int:
    try:
        print(prepare_successor(parse_args(sys.argv[1:] if argv is None else argv)))
    except (HeesReplaceError, ManagerReplaceError, OSError, SuccessorError, ValueError) as exc:
        print(f"omo_worker_successor.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
