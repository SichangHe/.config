#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Rebind the exact consolidated PB service without restarting live state."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_start import (
    StartError,
    held_rollout_metadata,
    process_held_rollouts,
    strict_reconciliation_process_tree,
)
from omo_manager.omo_stale_predecessor_close import PanePin, target_identity
from omo_manager.omo_task_lock import task_file_lock, task_file_lock_path, task_target_lock, task_target_lock_path
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import (
    authoritative_active_target_task_paths,
    has_pending_marker,
    reconcile_todo_text,
    relative_task_ref,
    replace_if_unchanged_locked_published,
    reserve_private_audit,
    root_membership_lock,
    same_file_state,
    update_frontmatter_status,
)
from omo_manager.omo_tmux_input_lock import tmux_input_lock, tmux_input_lock_path

SCHEMA = "omo-pb-owner-handoff/v1"
OPERATION = "pb-consolidated-owner-handoff"
OLD_TASK = "pb_news_live.md"
SUCCESSOR_TASK = "news_service.md"
TARGET = "pb:0"
OLD_MANAGER = "pb:1"
SUCCESSOR_MANAGER = "wl:1"
OLD_BLOCKER = SUCCESSOR_TASK
SUCCESSOR_BLOCKER = "watcher_repair.md"
ENV_NAME = "pb_watcher.env"
ENV_TASK_KEY = "PB_WATCHER_AGENT_TASK_FILE"
ENV_DATA_ROOT_KEY = "PB_WATCHER_DATA_ROOT"
PREFLIGHT = Path("scripts/news/pb-agent-wake")
LOOP_TARGET = "pb-watch-loop:0"
PB_REPOSITORY_COMMIT = "f9893b5eeb4cefa74d147d0642915db251b3bc6d"
PB_PREFLIGHT_STDOUT = "agent_owner=pb:0.0\n"
OLD_PRE_SHA256 = "1060291110358026a44fdd727849daea0e9aefa259884151ffc58904294a627e"
SUCCESSOR_PRE_SHA256 = "1710372777696560dec72aa52f93f867ed0e611f6821ab6e2bb0fb368f8277ba"
ENVIRONMENT_PRE_SHA256 = "89ebff70b7fc0182bbc5d8583f22d117da4e277308db7a7711ab14b1af5210ed"
SOURCE1982_REF = Path("manager_mail/85c5dff58359-1982.txt")
SOURCE1982_LOCATOR = "manager_mail/85c5dff58359-1982.txt:1-7"
SOURCE1982_SHA256 = "7b57f157958a7fccb57c9b31351a5660b13cc920865a0b898f35ff5cca30c32e"
SOURCE1982_BYTES = (
    b"Subject: Consolidate agents\n\n"
    b"Spawn a new agent to do this\r\n"
    b"Find all the agents who have emailed the human in the last hour\r\n"
    b"Terminate every other agent and collect all their pending task items\r\n"
    b"Independently decide which of those task items are still worth working on, group them. "
    b"Be very skeptical of agent-oriented tasks\r\n"
    b"Spawn new agents to work on the ones still worthy"
)
SOURCE1982_ENVELOPE = f'<human_instruction authoritative="true" source="{SOURCE1982_LOCATOR}">\n' + SOURCE1982_BYTES.replace(b"\r\n", b"\n").decode() + "</human_instruction>"
TRUSTED_DATA_ROOT = Path("/ssd1/sichangheagent/data")
PB_HANDOFF_LOCK_NAME = "manager-report-handoff.lock"
TRUSTED_ROOT_DEVICE = 66307
TRUSTED_ROOT_INODE = 84569809
TRUSTED_REPO_DEVICE = 66307
TRUSTED_REPO_INODE = 88490610
TRUSTED_DATA_DEVICE = 66307
TRUSTED_DATA_INODE = 83639326
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PANE_RE = re.compile(r"^%[0-9]+$")
HANDOFF_NOTE = (
    "(🤖 PB fleet consolidation: historical `pb_news_live.md` ownership completed without signaling reused target `pb:0`; "
    "live successor `news_service.md` remains managed by `wl:1`; watcher environment rebound without restarting the worker or loop.)"
)


@dataclass(frozen=True)
class Args:
    root: Path
    repo: Path
    mode: str
    old_sha256: str = ""
    successor_sha256: str = ""
    todo_sha256: str = ""
    env_sha256: str = ""
    repo_commit: str = ""
    pane: PanePin | None = None
    session_id: str = ""
    loop_pane: PanePin | None = None
    audit: Path | None = None


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    state: os.stat_result


@dataclass(frozen=True)
class Plan:
    args: Args
    old: Snapshot
    successor: Snapshot
    todo: Snapshot
    environment: Snapshot
    authority: Snapshot
    old_after: bytes
    successor_after: bytes
    todo_after: bytes
    environment_after: bytes
    helper_sha256: str


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    repo: Path = Path()
    mode: str = ""
    old_sha256: str = ""
    successor_sha256: str = ""
    todo_sha256: str = ""
    env_sha256: str = ""
    repo_commit: str = ""
    pane_id: str = ""
    pane_pid: int = 0
    pane_start_ticks: int = 0
    session_id: str = ""
    loop_pane_id: str = ""
    loop_pane_pid: int = 0
    loop_pane_start_ticks: int = 0
    audit: Path | None = None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: dict[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def binding_id(value: dict[str, object]) -> str:
    unsigned = dict(value)
    unsigned.pop("binding_id", None)
    return digest(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("describe", "execute"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--old-sha256", default="")
    parser.add_argument("--successor-sha256", default="")
    parser.add_argument("--todo-sha256", default="")
    parser.add_argument("--env-sha256", default="")
    parser.add_argument("--repo-commit", default="")
    parser.add_argument("--pane-id", default="")
    parser.add_argument("--pane-pid", type=int, default=0)
    parser.add_argument("--pane-start-ticks", type=int, default=0)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--loop-pane-id", default="")
    parser.add_argument("--loop-pane-pid", type=int, default=0)
    parser.add_argument("--loop-pane-start-ticks", type=int, default=0)
    parser.add_argument("--audit", type=Path)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    root = parsed.root.expanduser().resolve(strict=True)
    repo = parsed.repo.expanduser().resolve(strict=True)
    if parsed.mode == "describe":
        unexpected = (
            parsed.old_sha256,
            parsed.successor_sha256,
            parsed.todo_sha256,
            parsed.env_sha256,
            parsed.repo_commit,
            parsed.pane_id,
            parsed.pane_pid,
            parsed.pane_start_ticks,
            parsed.session_id,
            parsed.loop_pane_id,
            parsed.loop_pane_pid,
            parsed.loop_pane_start_ticks,
            parsed.audit,
        )
        if any(unexpected):
            parser.error("describe accepts only --root and --repo")
        return Args(root, repo, "describe")
    hashes = (parsed.old_sha256, parsed.successor_sha256, parsed.todo_sha256, parsed.env_sha256)
    if (
        any(SHA256_RE.fullmatch(value) is None for value in hashes)
        or parsed.old_sha256 != OLD_PRE_SHA256
        or parsed.successor_sha256 != SUCCESSOR_PRE_SHA256
        or parsed.env_sha256 != ENVIRONMENT_PRE_SHA256
        or parsed.repo_commit != PB_REPOSITORY_COMMIT
        or PANE_RE.fullmatch(parsed.pane_id) is None
        or parsed.pane_pid <= 1
        or parsed.pane_start_ticks <= 0
        or SESSION_RE.fullmatch(parsed.session_id) is None
        or PANE_RE.fullmatch(parsed.loop_pane_id) is None
        or parsed.loop_pane_pid <= 1
        or parsed.loop_pane_start_ticks <= 0
        or parsed.audit is None
        or not parsed.audit.is_absolute()
    ):
        parser.error("execute requires exact digests, repository commit, worker/loop process pins, session id, and absolute audit path")
    return Args(
        root,
        repo,
        "execute",
        parsed.old_sha256,
        parsed.successor_sha256,
        parsed.todo_sha256,
        parsed.env_sha256,
        parsed.repo_commit,
        PanePin(TARGET, parsed.pane_id, parsed.pane_pid, parsed.pane_start_ticks),
        parsed.session_id.lower(),
        PanePin(LOOP_TARGET, parsed.loop_pane_id, parsed.loop_pane_pid, parsed.loop_pane_start_ticks),
        parsed.audit.resolve(strict=False),
    )


def trusted_directory(path: Path, device: int, inode: int, label: str) -> None:
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or (details.st_dev, details.st_ino) != (device, inode):
        raise TaskFrontmatterError(f"{label} is not the exact trusted directory")


@contextmanager
def pb_handoff_lock() -> Iterator[None]:
    """Serialize the owner switch with PB database claims and prompt handoffs."""

    trusted_directory(TRUSTED_DATA_ROOT, TRUSTED_DATA_DEVICE, TRUSTED_DATA_INODE, "PB data root")
    path = TRUSTED_DATA_ROOT / PB_HANDOFF_LOCK_NAME
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1:
        raise TaskFrontmatterError("PB manager-report handoff lock is not the exact private regular file")
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not same_file_state(before, opened):
            raise TaskFrontmatterError("PB manager-report handoff lock changed while it was opened")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = path.lstat()
        if not same_file_state(opened, current):
            raise TaskFrontmatterError("PB manager-report handoff lock changed while it was acquired")
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def snapshot(path: Path, label: str) -> Snapshot:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1:
        raise TaskFrontmatterError(f"{label} must be one owner-owned regular file")
    data = path.read_bytes()
    after = path.lstat()
    if not same_file_state(details, after) or len(data) != details.st_size:
        raise TaskFrontmatterError(f"{label} changed while it was read")
    return Snapshot(path, data, details)


def fsync_parent(path: Path) -> None:
    """Make one completed same-directory rename durable before the next phase."""

    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_replace(path: Path, data: bytes, before: os.stat_result) -> os.stat_result:
    """Publish exact UTF-8 bytes and durably record the directory entry."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError(f"cannot publish non-UTF-8 PB handoff state: {path}") from exc
    published = replace_if_unchanged_locked_published(path, text, before)
    try:
        os.fsync(published.fd)
        fsync_parent(path)
        return published.state
    finally:
        published.close()


def durable_sync_existing(path: Path, expected: os.stat_result) -> None:
    """Finish durability for an already-published audit on crash replay."""

    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not same_file_state(expected, opened) or not same_file_state(opened, path.lstat()):
            raise TaskFrontmatterError("PB handoff audit changed before durability replay")
        os.fsync(descriptor)
        fsync_parent(path)
        if not same_file_state(opened, path.lstat()):
            raise TaskFrontmatterError("PB handoff audit changed during durability replay")
    finally:
        os.close(descriptor)


def git_output(repo: Path, *arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except subprocess.SubprocessError as exc:
        raise TaskFrontmatterError(f"PB repository identity check failed: {exc}") from exc


def repository_commit(repo: Path) -> str:
    head = git_output(repo, "rev-parse", "HEAD")
    upstream = git_output(repo, "rev-parse", "origin/main")
    if head != PB_REPOSITORY_COMMIT or upstream != PB_REPOSITORY_COMMIT:
        raise TaskFrontmatterError("PB repository is not the exact reviewed and pushed handoff commit")
    for arguments in (("diff", "--quiet", "--"), ("diff", "--cached", "--quiet", "--")):
        try:
            subprocess.run(["git", "-C", str(repo), *arguments], check=True, timeout=10)
        except subprocess.CalledProcessError as exc:
            raise TaskFrontmatterError("PB repository has tracked changes") from exc
    return head


def require_pin(pin: PanePin, label: str) -> None:
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError(f"{label} process identity changed")


def root_session_from_process(
    pin: PanePin,
    repo: Path,
    session_root: Path | None = None,
) -> str:
    """Bind exactly one top-level Codex rollout held by the stable pane tree."""

    root = (session_root or (Path.home() / ".codex/sessions")).resolve(strict=True)
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("PB worker changed before Codex rollout binding")
    try:
        tree_before = strict_reconciliation_process_tree(pin.pane_pid)
        held_before = process_held_rollouts(tree_before, root)
        candidates: list[tuple[str, Path]] = []
        forbidden_parent_keys = {
            "parent",
            "parent_id",
            "parent_session_id",
            "parent_thread_id",
            "fork",
            "fork_id",
            "forked_from",
            "forked_from_id",
        }
        for identity, (path, _holder_pid, _descriptor) in held_before.items():
            _metadata_bytes, metadata = held_rollout_metadata(path, identity)
            payload = metadata.get("payload")
            if not isinstance(payload, dict):
                continue
            if not (
                metadata.get("type") == "session_meta"
                and metadata.get("ordinal") == 0
                and payload.get("originator") == "codex-tui"
                and payload.get("source") == "cli"
                and payload.get("thread_source") == "user"
            ):
                continue
            session_id = payload.get("id")
            cwd = payload.get("cwd")
            if (
                not isinstance(session_id, str)
                or SESSION_RE.fullmatch(session_id) is None
                or payload.get("session_id") != session_id
                or not isinstance(cwd, str)
                or any(key in metadata or key in payload for key in forbidden_parent_keys)
                or not path.name.endswith(f"-{session_id}.jsonl")
            ):
                raise TaskFrontmatterError("PB top-level Codex rollout metadata is malformed")
            try:
                rollout_cwd = Path(cwd).resolve(strict=True)
            except OSError as exc:
                raise TaskFrontmatterError("PB top-level Codex rollout working directory is unavailable") from exc
            candidates.append((session_id.lower(), rollout_cwd))
        if len(candidates) != 1:
            raise TaskFrontmatterError("PB worker does not hold exactly one top-level Codex rollout")
        if candidates[0][1] != repo:
            raise TaskFrontmatterError("PB top-level Codex rollout does not belong to the PB repository")
        tree_after = strict_reconciliation_process_tree(pin.pane_pid)
        held_after = process_held_rollouts(tree_after, root)
    except StartError as exc:
        raise TaskFrontmatterError(f"PB Codex rollout binding failed: {exc}") from exc
    if tree_after != tree_before or held_after != held_before or target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("PB worker process tree or rollout bindings changed while read")
    return candidates[0][0]


def require_runtime(args: Args) -> None:
    if args.pane is None or args.loop_pane is None:
        raise TaskFrontmatterError("PB runtime pins are missing")
    require_pin(args.pane, "PB worker")
    if root_session_from_process(args.pane, args.repo) != args.session_id:
        raise TaskFrontmatterError("PB worker Codex session changed")
    require_pin(args.loop_pane, "PB loop")


def environment_after(data: bytes, old_task: Path, successor_task: Path, repo: Path) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("PB watcher environment is not UTF-8") from exc
    lines = text.splitlines(keepends=True)
    required = {
        ENV_DATA_ROOT_KEY: str(TRUSTED_DATA_ROOT),
        ENV_TASK_KEY: str(old_task),
        "PB_WATCHER_AGENT_TMUX": "pb:0.0",
        "PB_WATCHER_AGENT_TOOL": "codex",
        "PB_WATCHER_AGENT_MODEL": "gpt-5.6-terra",
        "PB_WATCHER_AGENT_REASONING_EFFORT": "medium",
    }
    observed: dict[str, list[str]] = {key: [] for key in required}
    for line in lines:
        key, separator, value = line.rstrip("\r\n").partition("=")
        if separator and key in observed:
            observed[key].append(value)
    if any(observed[key] != [value] for key, value in required.items()):
        raise TaskFrontmatterError("PB watcher environment does not have the exact singleton owner binding")
    expected_data_root = repo.parent / "data"
    if TRUSTED_DATA_ROOT != expected_data_root:
        raise TaskFrontmatterError("PB watcher environment data root is not the trusted repository sibling")
    indexes = [index for index, line in enumerate(lines) if line.startswith(f"{ENV_TASK_KEY}=")]
    expected = f"{ENV_TASK_KEY}={old_task}"
    if len(indexes) != 1 or lines[indexes[0]].rstrip("\r\n") != expected:
        raise TaskFrontmatterError("PB watcher environment does not name the exact historical task")
    ending = lines[indexes[0]][len(lines[indexes[0]].rstrip("\r\n")) :]
    lines[indexes[0]] = f"{ENV_TASK_KEY}={successor_task}{ending}"
    return "".join(lines).encode()


def validate_initial_todo(text: str) -> None:
    """Require one exact current row for each side of the reused-target handoff."""

    section = ""
    current_sections = 0
    old_rows: list[tuple[str, str]] = []
    successor_rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line == line.lstrip() and line.endswith(":"):
            section = line[:-1]
            if section == "current":
                current_sections += 1
            continue
        fields = line.split()
        if not fields:
            continue
        if section == "current" and len(fields) != 2:
            raise TaskFrontmatterError("PB handoff TODO has a malformed current row")
        row = (section, line)
        if fields[0] == OLD_TASK:
            old_rows.append(row)
        if fields[0] == SUCCESSOR_TASK:
            successor_rows.append(row)
    if current_sections != 1 or old_rows != [("current", f"{OLD_TASK} {TARGET}")] or successor_rows != [("current", f"{SUCCESSOR_TASK} {TARGET}")]:
        raise TaskFrontmatterError("PB handoff TODO does not contain the exact predecessor/successor current rows")


def require_source1982(root: Path, successor_data: bytes) -> Snapshot:
    """Authenticate the exact Human source bytes and their successor envelope."""

    authority = snapshot(root / SOURCE1982_REF, "Source-1982 Human authority")
    if authority.data != SOURCE1982_BYTES or digest(authority.data) != SOURCE1982_SHA256:
        raise TaskFrontmatterError("Source-1982 Human authority bytes changed")
    try:
        successor_text = successor_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("PB successor authority envelope is not UTF-8") from exc
    if successor_text.count(SOURCE1982_ENVELOPE) != 1:
        raise TaskFrontmatterError("PB successor lacks the exact Source-1982 Human authority envelope")
    return authority


def plan(args: Args) -> Plan:
    trusted_directory(args.root, TRUSTED_ROOT_DEVICE, TRUSTED_ROOT_INODE, "work-log root")
    trusted_directory(args.repo, TRUSTED_REPO_DEVICE, TRUSTED_REPO_INODE, "PB repository")
    old = snapshot(args.root / OLD_TASK, "historical PB task")
    successor = snapshot(args.root / SUCCESSOR_TASK, "PB successor task")
    todo = snapshot(args.root / "TODO.md", "work-log TODO")
    environment = snapshot(args.repo / ENV_NAME, "PB watcher environment")
    authority = require_source1982(args.root, successor.data)
    if digest(old.data) != OLD_PRE_SHA256 or digest(successor.data) != SUCCESSOR_PRE_SHA256 or digest(environment.data) != ENVIRONMENT_PRE_SHA256:
        raise TaskFrontmatterError("PB handoff predecessor, successor, or environment left its exact reviewed pre-state")
    try:
        old_text = old.data.decode()
        successor_text = successor.data.decode()
        todo_text = todo.data.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("PB task or TODO input is not UTF-8") from exc
    old_metadata = parse_task_metadata(old_text, args.root)
    successor_metadata = parse_task_metadata(successor_text, args.root)
    validate_initial_todo(todo_text)
    if (
        old_metadata is None
        or old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status != "blocked"
        or old_metadata.blocked_on != OLD_BLOCKER
        or old_metadata.runat != TARGET
        or old_metadata.managerat != OLD_MANAGER
        or old_metadata.is_manager
        or old_metadata.pending_task_items
        or has_pending_marker(old_text)
    ):
        raise TaskFrontmatterError("historical PB task does not match the exact empty blocked predecessor")
    if (
        successor_metadata is None
        or successor_metadata.version != TASK_FRONTMATTER_V1
        or (successor_metadata.status, successor_metadata.blocked_on) != ("blocked", SUCCESSOR_BLOCKER)
        or successor_metadata.runat != TARGET
        or successor_metadata.managerat != SUCCESSOR_MANAGER
        or successor_metadata.is_manager
        or not successor_metadata.pending_task_items
        or successor_metadata.session_id != args.session_id
        or has_pending_marker(successor_text)
    ):
        raise TaskFrontmatterError("PB successor does not match the exact active consolidated owner")
    owners = authoritative_active_target_task_paths(args.root, TARGET)
    if owners != tuple(sorted((old.path.resolve(), successor.path.resolve()))):
        found = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
        raise TaskFrontmatterError(f"PB target ownership is not the exact predecessor/successor pair: {found}")
    old_after_text = update_frontmatter_status(old_text, "done", "", args.root).rstrip("\n")
    if HANDOFF_NOTE in old_after_text:
        raise TaskFrontmatterError("historical PB task already contains a handoff note without completed transaction evidence")
    old_after = f"{old_after_text}\n\n{HANDOFF_NOTE}\n".encode()
    successor_after = update_frontmatter_status(successor_text, "long_running", "", args.root).encode()
    todo_after = reconcile_todo_text(args.root, old.path, todo_text, TARGET, "previous", ("current",)).encode()
    environment_result = environment_after(environment.data, old.path, successor.path, args.repo)
    helper = Path(__file__).resolve().read_bytes()
    return Plan(
        args,
        old,
        successor,
        todo,
        environment,
        authority,
        old_after,
        successor_after,
        todo_after,
        environment_result,
        digest(helper),
    )


def audit_record(prepared: Plan, state: str) -> dict[str, object]:
    args = prepared.args
    assert args.pane is not None and args.loop_pane is not None
    record: dict[str, object] = {
        "schema": SCHEMA,
        "operation": OPERATION,
        "state": state,
        "root": str(args.root),
        "repo": str(args.repo),
        "repo_commit": args.repo_commit,
        "helper_sha256": prepared.helper_sha256,
        "old_task": OLD_TASK,
        "successor_task": SUCCESSOR_TASK,
        "target": TARGET,
        "old_manager": OLD_MANAGER,
        "successor_manager": SUCCESSOR_MANAGER,
        "authority_ref": str(SOURCE1982_REF),
        "authority_sha256": SOURCE1982_SHA256,
        "authority_envelope_sha256": digest(SOURCE1982_ENVELOPE.encode()),
        "old_before": base64.b64encode(prepared.old.data).decode(),
        "old_after": base64.b64encode(prepared.old_after).decode(),
        "successor_before": base64.b64encode(prepared.successor.data).decode(),
        "successor_after": base64.b64encode(prepared.successor_after).decode(),
        "todo_before": base64.b64encode(prepared.todo.data).decode(),
        "todo_after": base64.b64encode(prepared.todo_after).decode(),
        "environment_before": base64.b64encode(prepared.environment.data).decode(),
        "environment_after": base64.b64encode(prepared.environment_after).decode(),
        "pane": {
            "id": args.pane.pane_id,
            "pid": args.pane.pane_pid,
            "start_ticks": args.pane.pane_start_ticks,
            "session_id": args.session_id,
        },
        "loop_pane": {
            "id": args.loop_pane.pane_id,
            "pid": args.loop_pane.pane_pid,
            "start_ticks": args.loop_pane.pane_start_ticks,
        },
    }
    record["binding_id"] = binding_id(record)
    return record


def validate_execution_assertions(prepared: Plan) -> None:
    args = prepared.args
    expected = (
        (digest(prepared.old.data), args.old_sha256, "historical task"),
        (digest(prepared.successor.data), args.successor_sha256, "successor task"),
        (digest(prepared.todo.data), args.todo_sha256, "TODO"),
        (digest(prepared.environment.data), args.env_sha256, "watcher environment"),
        (repository_commit(args.repo), args.repo_commit, "PB repository commit"),
    )
    for observed, asserted, label in expected:
        if observed != asserted:
            raise TaskFrontmatterError(f"{label} does not match its exact assertion")


def read_audit(path: Path) -> tuple[dict[str, object], os.stat_result]:
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise TaskFrontmatterError("PB handoff audit directory is not owner-private")
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1 or details.st_size > 16 * 1024 * 1024:
        raise TaskFrontmatterError("PB handoff audit is not one private owner-owned regular file")
    try:
        value: object = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("PB handoff audit is not JSON") from exc
    if not isinstance(value, dict) or canonical_json({str(key): item for key, item in value.items()}) != path.read_bytes():
        raise TaskFrontmatterError("PB handoff audit is not canonical JSON")
    record = {str(key): item for key, item in value.items()}
    if record.get("binding_id") != binding_id(record):
        raise TaskFrontmatterError("PB handoff audit binding is invalid")
    return record, details


def decode_field(record: dict[str, object], name: str) -> bytes:
    value = record.get(name)
    if not isinstance(value, str):
        raise TaskFrontmatterError(f"PB handoff audit {name} is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TaskFrontmatterError(f"PB handoff audit {name} is invalid") from exc


def recover_plan(args: Args, record: dict[str, object]) -> Plan:
    pane = record.get("pane")
    loop = record.get("loop_pane")
    if (
        record.get("schema") != SCHEMA
        or record.get("operation") != OPERATION
        or record.get("state") not in {"prepared", "commit-intent", "committed", "rolled-back", "complete"}
        or record.get("root") != str(args.root)
        or record.get("repo") != str(args.repo)
        or record.get("repo_commit") != args.repo_commit
        or record.get("old_task") != OLD_TASK
        or record.get("successor_task") != SUCCESSOR_TASK
        or record.get("target") != TARGET
        or record.get("old_manager") != OLD_MANAGER
        or record.get("successor_manager") != SUCCESSOR_MANAGER
        or record.get("authority_ref") != str(SOURCE1982_REF)
        or record.get("authority_sha256") != SOURCE1982_SHA256
        or record.get("authority_envelope_sha256") != digest(SOURCE1982_ENVELOPE.encode())
        or not isinstance(pane, dict)
        or not isinstance(loop, dict)
        or args.pane is None
        or args.loop_pane is None
        or pane != {"id": args.pane.pane_id, "pid": args.pane.pane_pid, "start_ticks": args.pane.pane_start_ticks, "session_id": args.session_id}
        or loop != {"id": args.loop_pane.pane_id, "pid": args.loop_pane.pane_pid, "start_ticks": args.loop_pane.pane_start_ticks}
        or record.get("helper_sha256") != digest(Path(__file__).resolve().read_bytes())
    ):
        raise TaskFrontmatterError("PB handoff audit does not match this exact transaction")
    old_before = decode_field(record, "old_before")
    old_after = decode_field(record, "old_after")
    successor_before = decode_field(record, "successor_before")
    successor_after = decode_field(record, "successor_after")
    todo_before = decode_field(record, "todo_before")
    todo_after = decode_field(record, "todo_after")
    env_before = decode_field(record, "environment_before")
    env_after = decode_field(record, "environment_after")
    successor = snapshot(args.root / SUCCESSOR_TASK, "PB successor task")
    old = snapshot(args.root / OLD_TASK, "historical PB task")
    todo = snapshot(args.root / "TODO.md", "work-log TODO")
    environment = snapshot(args.repo / ENV_NAME, "PB watcher environment")
    authority = require_source1982(args.root, successor.data)
    if (
        digest(old_before) != args.old_sha256
        or digest(old_before) != OLD_PRE_SHA256
        or digest(successor_before) != args.successor_sha256
        or digest(successor_before) != SUCCESSOR_PRE_SHA256
        or digest(todo_before) != args.todo_sha256
        or digest(env_before) != args.env_sha256
        or digest(env_before) != ENVIRONMENT_PRE_SHA256
    ):
        raise TaskFrontmatterError("PB handoff audit does not match the original exact assertions")
    try:
        old_before_text = old_before.decode()
        successor_before_text = successor_before.decode()
        todo_before_text = todo_before.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("PB handoff audit input is not UTF-8") from exc
    expected_old = f"{update_frontmatter_status(old_before_text, 'done', '', args.root).rstrip(chr(10))}\n\n{HANDOFF_NOTE}\n".encode()
    expected_successor = update_frontmatter_status(
        successor_before_text,
        "long_running",
        "",
        args.root,
    ).encode()
    validate_initial_todo(todo_before_text)
    expected_todo = reconcile_todo_text(args.root, old.path, todo_before_text, TARGET, "previous", ("current",)).encode()
    expected_env = environment_after(env_before, old.path, successor.path, args.repo)
    if old_after != expected_old or successor_after != expected_successor or todo_after != expected_todo or env_after != expected_env:
        raise TaskFrontmatterError("PB handoff audit prepared outputs are invalid")
    if (
        old.data not in {old_before, old_after}
        or successor.data not in {successor_before, successor_after}
        or todo.data not in {todo_before, todo_after}
        or environment.data not in {env_before, env_after}
    ):
        raise TaskFrontmatterError("PB handoff file state is outside the prepared transaction")
    phases = (
        (old.data, old_before, old_after),
        (todo.data, todo_before, todo_after),
        (successor.data, successor_before, successor_after),
        (environment.data, env_before, env_after),
    )
    if not any(all(current == (after if index < prefix else before) for index, (current, before, after) in enumerate(phases)) for prefix in range(len(phases) + 1)):
        raise TaskFrontmatterError("PB handoff files are in a non-monotonic mixed state")
    return Plan(
        args,
        Snapshot(old.path, old_before, old.state),
        Snapshot(successor.path, successor_before, successor.state),
        Snapshot(todo.path, todo_before, todo.state),
        Snapshot(environment.path, env_before, environment.state),
        authority,
        old_after,
        successor_after,
        todo_after,
        env_after,
        str(record["helper_sha256"]),
    )


def validate_final(prepared: Plan) -> None:
    args = prepared.args
    if repository_commit(args.repo) != args.repo_commit:
        raise TaskFrontmatterError("PB repository commit changed during the handoff")
    if digest(Path(__file__).resolve().read_bytes()) != prepared.helper_sha256:
        raise TaskFrontmatterError("PB handoff helper changed during the transaction")
    if (
        prepared.old.data != prepared.old_after
        or prepared.successor.data != prepared.successor_after
        or prepared.todo.data != prepared.todo_after
        or prepared.environment.data != prepared.environment_after
    ):
        raise TaskFrontmatterError("PB final state does not equal the exact prepared transaction")
    old_text = prepared.old.data.decode()
    successor_text = prepared.successor.data.decode()
    old = parse_task_metadata(old_text, args.root)
    successor = parse_task_metadata(successor_text, args.root)
    if old is None or old.status != "done" or old.pending_task_items or old.runat != TARGET or HANDOFF_NOTE not in old_text:
        raise TaskFrontmatterError("PB predecessor final lifecycle state is invalid")
    if successor is None or successor.status != "long_running" or successor.runat != TARGET or successor.managerat != SUCCESSOR_MANAGER:
        raise TaskFrontmatterError("PB successor final lifecycle state is invalid")
    owners = authoritative_active_target_task_paths(args.root, TARGET)
    if owners != (prepared.successor.path.resolve(),):
        raise TaskFrontmatterError("PB successor is not the sole active target owner")
    require_runtime(args)
    completed = subprocess.run(
        [str(args.repo / PREFLIGHT), "--owner-preflight"],
        cwd=args.repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0 or completed.stdout != PB_PREFLIGHT_STDOUT or completed.stderr != "":
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise TaskFrontmatterError(f"PB final owner preflight rejected the transaction: {detail}")
    if repository_commit(args.repo) != args.repo_commit:
        raise TaskFrontmatterError("PB repository commit changed during final owner preflight")
    require_runtime(args)
    final_old = snapshot(prepared.old.path, "historical PB task")
    final_successor = snapshot(prepared.successor.path, "PB successor task")
    final_todo = snapshot(prepared.todo.path, "work-log TODO")
    final_environment = snapshot(prepared.environment.path, "PB watcher environment")
    require_source1982(args.root, final_successor.data)
    if (
        final_old.data != prepared.old_after
        or final_successor.data != prepared.successor_after
        or final_todo.data != prepared.todo_after
        or final_environment.data != prepared.environment_after
        or authoritative_active_target_task_paths(args.root, TARGET) != (prepared.successor.path.resolve(),)
    ):
        raise TaskFrontmatterError("PB final owner state changed during its exact preflight")
    if repository_commit(args.repo) != args.repo_commit or digest(Path(__file__).resolve().read_bytes()) != prepared.helper_sha256:
        raise TaskFrontmatterError("PB repository or handoff helper changed before commit")
    require_runtime(args)


def rollback(prepared: Plan) -> None:
    """Restore the exact prepared inputs when an in-process validation fails."""

    for path, before, after, label in (
        (prepared.environment.path, prepared.environment.data, prepared.environment_after, "PB watcher environment"),
        (prepared.successor.path, prepared.successor.data, prepared.successor_after, "PB successor task"),
        (prepared.todo.path, prepared.todo.data, prepared.todo_after, "work-log TODO"),
        (prepared.old.path, prepared.old.data, prepared.old_after, "historical PB task"),
    ):
        current = snapshot(path, label)
        if current.data == before:
            continue
        if current.data != after:
            raise TaskFrontmatterError(f"cannot roll back {label}: bytes left the prepared transaction")
        try:
            restored = before.decode()
        except UnicodeDecodeError as exc:
            raise TaskFrontmatterError(f"cannot roll back {label}: prepared input is not UTF-8") from exc
        durable_replace(path, restored.encode("utf-8"), current.state)


def publish_phase(before: Snapshot, after: bytes, label: str) -> None:
    """Publish one phase only from its authenticated input bytes."""

    current = snapshot(before.path, label)
    if current.data == after:
        return
    if current.data != before.data:
        raise TaskFrontmatterError(f"cannot publish {label}: bytes left the prepared transaction")
    durable_replace(current.path, after, current.state)


def describe(args: Args) -> dict[str, object]:
    commit = repository_commit(args.repo)
    pane_id, pane_pid, pane_ticks = target_identity(TARGET)
    loop_id, loop_pid, loop_ticks = target_identity(LOOP_TARGET)
    if not pane_id or not loop_id:
        raise TaskFrontmatterError("PB worker or loop is not live")
    bound = Args(
        args.root,
        args.repo,
        "execute",
        repo_commit=commit,
        pane=PanePin(TARGET, pane_id, pane_pid, pane_ticks),
        session_id=root_session_from_process(PanePin(TARGET, pane_id, pane_pid, pane_ticks), args.repo),
        loop_pane=PanePin(LOOP_TARGET, loop_id, loop_pid, loop_ticks),
    )
    prepared = plan(bound)
    require_runtime(bound)
    return {
        "root": str(args.root),
        "repo": str(args.repo),
        "old_sha256": digest(prepared.old.data),
        "successor_sha256": digest(prepared.successor.data),
        "todo_sha256": digest(prepared.todo.data),
        "env_sha256": digest(prepared.environment.data),
        "repo_commit": commit,
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "pane_start_ticks": pane_ticks,
        "session_id": bound.session_id,
        "loop_pane_id": loop_id,
        "loop_pane_pid": loop_pid,
        "loop_pane_start_ticks": loop_ticks,
    }


# 🧑 "preserve the live loop, browser/CDP/database/queue; do not restart or duplicate anything"
def execute(args: Args) -> None:
    if args.audit is None:
        raise TaskFrontmatterError("PB handoff audit path is missing or overlaps managed state")
    audit = args.audit.resolve(strict=False)
    managed_paths = {
        args.root / OLD_TASK,
        args.root / SUCCESSOR_TASK,
        args.root / "TODO.md",
        args.root / SOURCE1982_REF,
        args.root / ".omo-task-membership.lock",
        args.repo / ENV_NAME,
        TRUSTED_DATA_ROOT / PB_HANDOFF_LOCK_NAME,
    }
    protected_paths = managed_paths | {task_file_lock_path(path) for path in managed_paths}
    protected_paths.update(
        {
            task_file_lock_path(args.root / ".omo-task-membership.lock"),
            task_target_lock_path(args.root, TARGET),
            tmux_input_lock_path(TARGET),
        }
    )
    if audit in {path.resolve(strict=False) for path in protected_paths}:
        raise TaskFrontmatterError("PB handoff audit path is missing or overlaps managed state")
    trusted_directory(args.root, TRUSTED_ROOT_DEVICE, TRUSTED_ROOT_INODE, "work-log root")
    trusted_directory(args.repo, TRUSTED_REPO_DEVICE, TRUSTED_REPO_INODE, "PB repository")
    if repository_commit(args.repo) != args.repo_commit:
        raise TaskFrontmatterError("PB repository commit changed after evidence collection")
    with pb_handoff_lock(), tmux_input_lock(TARGET), root_membership_lock(args.root), task_target_lock(args.root, TARGET):
        with ExitStack() as locks:
            for path in sorted(
                (
                    args.root / OLD_TASK,
                    args.root / SUCCESSOR_TASK,
                    args.root / "TODO.md",
                    args.root / SOURCE1982_REF,
                    args.repo / ENV_NAME,
                    audit,
                ),
                key=str,
            ):
                locks.enter_context(task_file_lock(path))
            require_runtime(args)
            forward_only = False
            intent_attempted = False
            if audit.exists():
                record, audit_before = read_audit(audit)
                prepared = recover_plan(args, record)
                if record["state"] == "complete":
                    live_old = snapshot(prepared.old.path, "historical PB task")
                    live_successor = snapshot(prepared.successor.path, "PB successor task")
                    live_todo = snapshot(prepared.todo.path, "work-log TODO")
                    live_environment = snapshot(prepared.environment.path, "PB watcher environment")
                    if any(
                        (
                            live_old.data != prepared.old_after,
                            live_successor.data != prepared.successor_after,
                            live_todo.data != prepared.todo_after,
                            live_environment.data != prepared.environment_after,
                        )
                    ):
                        raise TaskFrontmatterError("completed PB handoff audit does not match live files")
                    validate_final(
                        Plan(
                            args,
                            live_old,
                            live_successor,
                            live_todo,
                            live_environment,
                            prepared.authority,
                            prepared.old_after,
                            prepared.successor_after,
                            prepared.todo_after,
                            prepared.environment_after,
                            prepared.helper_sha256,
                        )
                    )
                    durable_sync_existing(audit, audit_before)
                    return
                if record["state"] in {"commit-intent", "committed"}:
                    live_old = snapshot(prepared.old.path, "historical PB task")
                    live_successor = snapshot(prepared.successor.path, "PB successor task")
                    live_todo = snapshot(prepared.todo.path, "work-log TODO")
                    live_environment = snapshot(prepared.environment.path, "PB watcher environment")
                    if any(
                        (
                            live_old.data != prepared.old_after,
                            live_successor.data != prepared.successor_after,
                            live_todo.data != prepared.todo_after,
                            live_environment.data != prepared.environment_after,
                        )
                    ):
                        raise TaskFrontmatterError(f"{record['state']} PB handoff audit does not match live files")
                    forward_only = True
                if record["state"] == "rolled-back":
                    live_old = snapshot(prepared.old.path, "historical PB task")
                    live_successor = snapshot(prepared.successor.path, "PB successor task")
                    live_todo = snapshot(prepared.todo.path, "work-log TODO")
                    live_environment = snapshot(prepared.environment.path, "PB watcher environment")
                    if any(
                        (
                            live_old.data != prepared.old.data,
                            live_successor.data != prepared.successor.data,
                            live_todo.data != prepared.todo.data,
                            live_environment.data != prepared.environment.data,
                        )
                    ):
                        raise TaskFrontmatterError("rolled-back PB handoff audit does not match the restored inputs")
                    replacement = audit_record(prepared, "prepared")
                    durable_replace(audit, canonical_json(replacement), audit_before)
                    _, audit_before = read_audit(audit)
            else:
                prepared = plan(args)
                validate_execution_assertions(prepared)
                record = audit_record(prepared, "prepared")
                reserve_private_audit(audit, canonical_json(record).decode())
                _, audit_before = read_audit(audit)
            try:
                require_runtime(args)
                publish_phase(prepared.old, prepared.old_after, "historical PB task")
                require_runtime(args)
                publish_phase(prepared.todo, prepared.todo_after, "work-log TODO")
                require_runtime(args)
                publish_phase(prepared.successor, prepared.successor_after, "PB successor task")
                require_runtime(args)
                publish_phase(prepared.environment, prepared.environment_after, "PB watcher environment")
                final_plan = Plan(
                    args,
                    snapshot(prepared.old.path, "historical PB task"),
                    snapshot(prepared.successor.path, "PB successor task"),
                    snapshot(prepared.todo.path, "work-log TODO"),
                    snapshot(prepared.environment.path, "PB watcher environment"),
                    prepared.authority,
                    prepared.old_after,
                    prepared.successor_after,
                    prepared.todo_after,
                    prepared.environment_after,
                    prepared.helper_sha256,
                )
                if not forward_only:
                    intent_attempted = True
                    durable_replace(audit, canonical_json(audit_record(prepared, "commit-intent")), audit_before)
                    forward_only = True
                    _, audit_before = read_audit(audit)
                validate_final(final_plan)
                durable_replace(audit, canonical_json(audit_record(prepared, "committed")), audit_before)
                completed = audit_record(prepared, "complete")
                _, committed_audit = read_audit(audit)
                durable_replace(audit, canonical_json(completed), committed_audit)
            except Exception as exc:
                if intent_attempted and not forward_only:
                    try:
                        durable_state, _ = read_audit(audit)
                        forward_only = durable_state.get("state") in {"commit-intent", "committed", "complete"}
                    except Exception:
                        forward_only = True
                if forward_only:
                    raise TaskFrontmatterError("PB handoff entered forward-only recovery; rerun the same command to validate or finish its audit") from exc
                try:
                    rollback(prepared)
                    rolled_back = audit_record(prepared, "rolled-back")
                    durable_replace(audit, canonical_json(rolled_back), audit_before)
                except Exception as rollback_exc:
                    raise TaskFrontmatterError(f"PB handoff failed and exact rollback also failed: {rollback_exc}") from exc
                raise


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.mode == "describe":
            print(canonical_json(describe(args)).decode(), end="")
        else:
            execute(args)
            print("Rebound the live PB service to news_service.md without restarting worker, loop, browser, CDP, database, or queue state.")
        return 0
    except (OSError, ValueError, TaskFrontmatterError, subprocess.SubprocessError) as exc:
        print(f"omo_pb_owner_handoff.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
