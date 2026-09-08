#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Close one authenticated inert manager-containment sentinel without a successor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import parse_task_text, same_tmux_target
from omo_manager.omo_manager_containment_launch import Args as BridgeArgs
from omo_manager.omo_manager_containment_launch import (
    BridgeError,
    ContainmentReceipt,
    ProtectedBinding,
    containment_receipt,
    process_has_failed_successor_identity,
    protected_binding,
    protected_unchanged,
    tmux_condition,
)
from omo_manager.omo_manager_containment_launch import parse_args as parse_bridge_args
from omo_manager.omo_manager_rotate import PaneIdentity, resolve_exact_pane
from omo_manager.omo_manager_rotation_contain import (
    ContainmentError,
    FileEvidence,
    ProcessIdentity,
    current_command,
    json_no_duplicates,
    manager_rotation_lock,
    parse_datetime,
    process_argv,
    process_snapshot,
    process_stat,
    process_tree,
    read_regular_file,
    receipt_bytes,
    replace_private_exact,
    require_private_parent,
    stable_process_identity,
    write_private_exclusive,
)
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import Args as TaskStatusArgs
from omo_manager.omo_task_status import authoritative_active_target_task_paths, has_pending_marker, read_park_authority, read_park_authority_envelope

CLOSE_VERSION = "omo-manager-containment-close/v1"
CLOSE_OPERATION = "close-contained-sentinel-without-successor"
CLOSE_CLAIM = "receipt-bound-inert-sentinel-absent-no-successor-launched-by-this-helper"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CLOSE_RECORD_KEYS = {
    "version",
    "operation",
    "state",
    "claim",
    "close_id",
    "prepared_at",
    "completed_at",
    "completion_kind",
    "binding",
}
COMPLETE_KINDS = {"guarded-close-confirmed", "recovered-target-absent"}


class CloseError(RuntimeError):
    """A containment-sentinel close assertion or mutation failed."""


@dataclass(frozen=True)
class AuthorityBinding:
    source: str
    source_sha256: str
    source_lines: str
    excerpt_sha256: str
    envelope: str
    envelope_sha256: str


@dataclass(frozen=True)
class CurrentTaskBinding:
    file: FileEvidence
    todo: FileEvidence
    status: str
    blocker: str
    runat: str
    managerat: str
    todo_section: str
    todo_row: str


@dataclass(frozen=True)
class SentinelLive:
    pane: PaneIdentity
    sentinel: ProcessIdentity
    protected: tuple[ProtectedBinding, ...]


@dataclass(frozen=True)
class Args:
    bridge: BridgeArgs
    expected_current_task_sha256: str
    expected_current_todo_sha256: str
    expected_current_status: str
    expected_current_blocker: str
    expected_current_manager_target: str
    authority_file: Path
    authority_lines: tuple[int, int]
    authority_sha256: str
    authority_envelope: Path
    authority_envelope_sha256: str
    close_receipt: Path
    dry_run: bool


def line_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", value)
    if match is None or int(match.group(2)) < int(match.group(1)):
        raise argparse.ArgumentTypeError("expected an inclusive START-END line range")
    return int(match.group(1)), int(match.group(2))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        epilog=(
            "This close-only helper accepts one complete containment receipt and exact Human authority, then closes "
            "only its one-pane non-h* window. It never starts, resumes, or replaces an agent. --dry-run is read-only."
        ),
    )
    _ = parser.add_argument("--close-contained-sentinel-without-successor", action="store_true")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-file", type=Path, required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--containment-receipt", type=Path, required=True)
    _ = parser.add_argument("--containment-receipt-sha256", required=True)
    _ = parser.add_argument("--session-root", type=Path, required=True)
    _ = parser.add_argument("--expected-contained-pane-id", required=True)
    _ = parser.add_argument("--expected-contained-window-id", required=True)
    _ = parser.add_argument("--expected-contained-pid", type=int, required=True)
    _ = parser.add_argument("--expected-contained-start-ticks", type=int, required=True)
    _ = parser.add_argument("--expected-contained-argv-sha256", required=True)
    _ = parser.add_argument("--expected-failed-successor-command", required=True)
    _ = parser.add_argument("--expected-task-sha256", required=True)
    _ = parser.add_argument("--expected-blocker", required=True)
    _ = parser.add_argument("--expected-manager-target", required=True)
    _ = parser.add_argument("--expected-pending-item", action="append", default=[])
    _ = parser.add_argument("--expect-empty-queue", action="store_true")
    _ = parser.add_argument("--expected-watcher-pid", type=int, required=True)
    _ = parser.add_argument("--expected-watcher-start-ticks", type=int, required=True)
    _ = parser.add_argument("--protected-target", action="append", default=[])
    _ = parser.add_argument("--expected-current-task-sha256", required=True)
    _ = parser.add_argument("--expected-current-todo-sha256", required=True)
    _ = parser.add_argument("--expected-current-status", choices=("blocked", "done"), required=True)
    _ = parser.add_argument("--expected-current-blocker", default="")
    _ = parser.add_argument("--expected-current-manager-target", required=True)
    _ = parser.add_argument("--authority-file", type=Path, required=True)
    _ = parser.add_argument("--authority-lines", type=line_range, required=True)
    _ = parser.add_argument("--authority-sha256", required=True)
    _ = parser.add_argument("--authority-envelope", type=Path, required=True)
    _ = parser.add_argument("--authority-envelope-sha256", required=True)
    _ = parser.add_argument("--close-receipt", type=Path, required=True)
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv)
    if not parsed.close_contained_sentinel_without_successor:
        parser.error("--close-contained-sentinel-without-successor is required.")
    closing_hashes = (
        parsed.expected_current_task_sha256,
        parsed.expected_current_todo_sha256,
        parsed.authority_sha256,
        parsed.authority_envelope_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in closing_hashes):
        parser.error("current-state and authority SHA-256 assertions must be lowercase 64-character hexadecimal values.")
    if (parsed.expected_current_status == "blocked") != bool(parsed.expected_current_blocker):
        parser.error("blocked current state requires --expected-current-blocker; done current state forbids it.")
    if parsed.expect_empty_queue and parsed.expected_pending_item:
        parser.error("use either repeated --expected-pending-item or --expect-empty-queue, exactly once.")
    if not parsed.close_receipt.expanduser().is_absolute():
        parser.error("--close-receipt must be absolute.")
    containment_path = Path(os.path.abspath(parsed.containment_receipt.expanduser()))
    if not containment_path.is_absolute():
        parser.error("--containment-receipt must be absolute.")
    successor_receipt = containment_path.with_name(f"{containment_path.stem}-successor-ownership.receipt")
    expected_close_receipt = containment_path.with_name(f"{containment_path.stem}-sentinel-close.receipt")
    close_receipt = Path(os.path.abspath(parsed.close_receipt.expanduser()))
    if close_receipt != expected_close_receipt:
        parser.error(f"--close-receipt must use the one-shot path {expected_close_receipt}.")
    bridge_argv = [
        "--launch-contained-successor",
        "--root",
        str(parsed.root),
        "--task-file",
        str(parsed.task_file),
        "--target",
        parsed.target,
        "--containment-receipt",
        str(containment_path),
        "--containment-receipt-sha256",
        parsed.containment_receipt_sha256,
        "--session-root",
        str(parsed.session_root),
        "--expected-contained-pane-id",
        parsed.expected_contained_pane_id,
        "--expected-contained-window-id",
        parsed.expected_contained_window_id,
        "--expected-contained-pid",
        str(parsed.expected_contained_pid),
        "--expected-contained-start-ticks",
        str(parsed.expected_contained_start_ticks),
        "--expected-contained-argv-sha256",
        parsed.expected_contained_argv_sha256,
        "--expected-failed-successor-command",
        parsed.expected_failed_successor_command,
        "--expected-task-sha256",
        parsed.expected_task_sha256,
        "--expected-blocker",
        parsed.expected_blocker,
        "--expected-manager-target",
        parsed.expected_manager_target,
        "--expected-watcher-pid",
        str(parsed.expected_watcher_pid),
        "--expected-watcher-start-ticks",
        str(parsed.expected_watcher_start_ticks),
        "--ownership-receipt",
        str(successor_receipt),
        "--dry-run",
    ]
    bridge_argv.append("--expect-empty-queue" if parsed.expect_empty_queue else "--expected-pending-item")
    if not parsed.expect_empty_queue:
        if not parsed.expected_pending_item:
            parser.error("use either repeated --expected-pending-item or --expect-empty-queue, exactly once.")
        bridge_argv.append(parsed.expected_pending_item[0])
        for item in parsed.expected_pending_item[1:]:
            bridge_argv.extend(("--expected-pending-item", item))
    for target in parsed.protected_target:
        bridge_argv.extend(("--protected-target", target))
    bridge = parse_bridge_args(bridge_argv)
    return Args(
        bridge,
        parsed.expected_current_task_sha256,
        parsed.expected_current_todo_sha256,
        parsed.expected_current_status,
        parsed.expected_current_blocker,
        parsed.expected_current_manager_target,
        parsed.authority_file,
        parsed.authority_lines,
        parsed.authority_sha256,
        parsed.authority_envelope,
        parsed.authority_envelope_sha256,
        close_receipt,
        parsed.dry_run,
    )


def authority_binding(args: Args) -> AuthorityBinding:
    status_args = TaskStatusArgs(
        args.bridge.root,
        args.bridge.task_file,
        "",
        "",
        authority_file=args.authority_file,
        authority_lines=args.authority_lines,
        authority_sha256=args.authority_sha256,
        authority_envelope=args.authority_envelope,
        authority_envelope_sha256=args.authority_envelope_sha256,
    )
    excerpt, locator = read_park_authority(status_args)
    if not locator.startswith("manager_mail/"):
        raise CloseError("sentinel close authority must be one current direct manager_mail source.")
    envelope = read_park_authority_envelope(status_args, excerpt, locator)
    human_lines = [line.strip() for line in excerpt.replace("\r\n", "\n").splitlines() if line.strip() and not line.lstrip().startswith(">") and not line.casefold().startswith("subject:")]
    normalized = re.sub(r"\s+", " ", " ".join(human_lines)).strip()
    prohibited = re.search(
        r"\b(?:do not|don't|must not|never|should not)\b[^.!?]{0,160}\b(?:close|shut down|remove)\b",
        normalized,
        re.IGNORECASE,
    )
    statements = re.split(r"(?<=[.!?])\s+|\n+", normalized)
    direct_close = re.compile(
        rf"^(?i:(?:please\s+)?(?:close|shut\s+down|remove)\s+(?:the\s+)?)"
        rf"{re.escape(args.bridge.target)}"
        r"(?![A-Za-z0-9_:@/-]|\.[0-9])",
    )
    direct = any(
        "?" not in statement and direct_close.search(statement.strip()) is not None and re.search(r"\b(?:if|unless|whether|after|before|once|until|subject to)\b", statement, re.IGNORECASE) is None
        for statement in statements
    )
    if prohibited is not None or not direct:
        raise CloseError("authority excerpt does not contain one unconditional direct close instruction naming the exact target.")
    start, end = args.authority_lines
    return AuthorityBinding(
        locator,
        args.authority_sha256,
        f"{start}-{end}",
        hashlib.sha256(excerpt.encode()).hexdigest(),
        envelope,
        args.authority_envelope_sha256,
    )


def current_task_binding(args: Args) -> CurrentTaskBinding:
    task_bytes, task_file = read_regular_file(
        args.bridge.task_file,
        "current contained-manager task",
        args.expected_current_task_sha256,
    )
    todo_path = args.bridge.root / "TODO.md"
    todo_bytes, todo_file = read_regular_file(
        todo_path,
        "current contained-manager TODO index",
        args.expected_current_todo_sha256,
    )
    try:
        task_text = task_bytes.decode("utf-8")
        todo_text = todo_bytes.decode("utf-8")
        metadata = parse_task_metadata(task_text, args.bridge.root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise CloseError(f"current contained-manager task or TODO metadata is invalid: {exc}") from exc
    if metadata is None:
        raise CloseError("current contained-manager task lacks task frontmatter.")
    if (
        metadata.version != "v1.0.0"
        or metadata.status != args.expected_current_status
        or metadata.blocked_on != args.expected_current_blocker
        or metadata.tool != "codex"
        or not metadata.is_manager
        or not same_tmux_target(metadata.runat, args.bridge.target)
        or not same_tmux_target(metadata.managerat, args.expected_current_manager_target)
        or metadata.pending_task_items
        or has_pending_marker(task_text)
    ):
        raise CloseError("current manager status, blocker, target, parent, queue, or pending-marker state drifted.")
    task_ref = args.bridge.task_file.relative_to(args.bridge.root).as_posix()
    expected_section = f"todo:{'previous' if metadata.status == 'done' else 'current'}"
    expected_row = f"{task_ref} {metadata.runat}"
    claims = [claim for claim in parse_task_text(todo_text) if claim.task_file == task_ref]
    if (
        todo_text.splitlines().count("current:") != 1
        or todo_text.splitlines().count("previous:") != 1
        or [(claim.section, claim.line, claim.target) for claim in claims] != [(expected_section, expected_row, metadata.runat)]
    ):
        raise CloseError("TODO must contain one exact row for the current contained-manager lifecycle state.")
    owners = authoritative_active_target_task_paths(args.bridge.root, metadata.runat)
    expected_owners = () if metadata.status == "done" else (args.bridge.task_file,)
    if owners != expected_owners:
        raise CloseError("contained-manager target does not have the expected singular lifecycle owner state.")
    return CurrentTaskBinding(
        task_file,
        todo_file,
        metadata.status,
        metadata.blocked_on,
        metadata.runat,
        metadata.managerat,
        expected_section.removeprefix("todo:"),
        expected_row,
    )


def sentinel_live(args: Args, containment: ContainmentReceipt) -> SentinelLive:
    pane = resolve_exact_pane(args.bridge.target)
    expected_pane = containment.failed_successor_pane
    if (
        pane.pane_id != args.bridge.expected_contained_pane_id
        or pane.window_id != args.bridge.expected_contained_window_id
        or not same_tmux_target(pane.canonical_target, args.bridge.target)
        or pane.working_directory != expected_pane.working_directory
        or pane.pane_pid != containment.contained_process.pid
        or current_command(pane.pane_id) != "sleep"
    ):
        raise CloseError("live pane is not the exact receipt-bound inert containment sentinel.")
    sentinel = process_stat(pane.pane_pid)
    argv = process_argv(pane.pane_pid)
    if (
        sentinel.state == "Z"
        or stable_process_identity(sentinel) != stable_process_identity(containment.contained_process)
        or len(argv) != 2
        or Path(argv[0]).name != "sleep"
        or argv[1] != "infinity"
        or hashlib.sha256("\0".join(argv).encode()).hexdigest() != containment.contained_process.argv_sha256
    ):
        raise CloseError("live process is not the exact receipt-bound sleep infinity sentinel.")
    processes = process_snapshot()
    if process_tree(sentinel.pid, processes) != (sentinel,):
        raise CloseError("receipt-bound inert sentinel unexpectedly has a child process.")
    survivors = sorted(process.pid for process in processes.values() if process.pid != sentinel.pid and process_has_failed_successor_identity(process, containment))
    failed_ttys = {value.tty for value in containment.failed_successor_tree if value.tty}
    survivors.extend(sorted(process.pid for process in processes.values() if process.pid != sentinel.pid and process.tty in failed_ttys and process.pid not in survivors))
    if survivors:
        raise CloseError(f"failed successor process session/group has live survivors: {survivors}")
    window = subprocess.run(
        ["tmux", "list-panes", "-t", pane.window_id, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if window.returncode != 0 or window.stdout.splitlines() != [pane.pane_id]:
        raise CloseError("sentinel close requires the receipt-bound pane to be alone in its window.")
    protected = tuple(protected_binding(target) for target in args.bridge.protected_targets)
    if any(binding.pane.window_id == pane.window_id or binding.pane.pane_id == pane.pane_id for binding in protected):
        raise CloseError("a receipt-protected target overlaps the sentinel pane or window.")
    return SentinelLive(pane, sentinel, protected)


def revalidate_sentinel(args: Args, containment: ContainmentReceipt, initial: SentinelLive) -> SentinelLive:
    current = sentinel_live(args, containment)
    if current.pane != initial.pane or stable_process_identity(current.sentinel) != stable_process_identity(initial.sentinel) or not protected_unchanged(current.protected, initial.protected):
        raise CloseError("receipt-bound sentinel or protected target changed before its guarded close.")
    return current


def close_locks(args: Args, state_dir: Path) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(manager_rotation_lock(state_dir))
    stack.enter_context(task_file_lock(args.bridge.root / ".omo-task-membership.lock"))
    for target in sorted((args.bridge.target, *args.bridge.protected_targets)):
        stack.enter_context(task_target_lock(args.bridge.root, target))
    for path in sorted((args.bridge.task_file, args.bridge.root / "TODO.md"), key=str):
        stack.enter_context(task_file_lock(path))
    return stack


def close_binding(
    args: Args,
    containment: ContainmentReceipt,
    current: CurrentTaskBinding,
    authority: AuthorityBinding,
) -> dict[str, object]:
    pane = asdict(containment.failed_successor_pane)
    pane["working_directory"] = str(containment.failed_successor_pane.working_directory)
    pane["pane_pid"] = containment.contained_process.pid
    binding = {
        "root": str(args.bridge.root),
        "target": args.bridge.target,
        "containment_receipt": asdict(containment.file),
        "original_task": asdict(containment.task),
        "current_task": asdict(current),
        "original_watcher": asdict(containment.watcher),
        "pane": pane,
        "contained_process": asdict(containment.contained_process),
        "protected_targets": list(containment.protected_targets),
        "authority": asdict(authority),
        "successor_ownership_receipt": str(args.bridge.ownership_receipt),
        "successor_launched": False,
    }
    return cast(dict[str, object], json.loads(json.dumps(binding, ensure_ascii=True, sort_keys=True)))


def close_id(binding: dict[str, object]) -> str:
    canonical = json.dumps(binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def close_record(
    binding: dict[str, object],
    state: str,
    prepared_at: str,
    *,
    completion_kind: str = "",
    completed_at: str = "",
) -> dict[str, object]:
    return {
        "version": CLOSE_VERSION,
        "operation": CLOSE_OPERATION,
        "state": state,
        "claim": CLOSE_CLAIM,
        "close_id": close_id(binding),
        "prepared_at": prepared_at,
        "completed_at": completed_at,
        "completion_kind": completion_kind,
        "binding": binding,
    }


def validate_close_record(raw: bytes, binding: dict[str, object]) -> dict[str, object]:
    record = json_no_duplicates(raw, "sentinel close receipt")
    if (
        set(record) != CLOSE_RECORD_KEYS
        or record.get("version") != CLOSE_VERSION
        or record.get("operation") != CLOSE_OPERATION
        or record.get("claim") != CLOSE_CLAIM
        or record.get("close_id") != close_id(binding)
        or record.get("binding") != binding
        or record.get("state") not in {"prepared-or-committed", "complete"}
        or not isinstance(record.get("prepared_at"), str)
        or not isinstance(record.get("completed_at"), str)
        or not isinstance(record.get("completion_kind"), str)
    ):
        raise CloseError("sentinel close receipt is malformed or does not match the requested binding.")
    prepared_at = cast(str, record["prepared_at"])
    completed_at = cast(str, record["completed_at"])
    completion_kind = cast(str, record["completion_kind"])
    prepared_time = parse_datetime(prepared_at, "sentinel close prepared timestamp")
    if record["state"] == "prepared-or-committed":
        if completed_at or completion_kind:
            raise CloseError("prepared sentinel close receipt contains premature completion evidence.")
    elif completion_kind not in COMPLETE_KINDS or not completed_at:
        raise CloseError("complete sentinel close receipt lacks one supported completion classification.")
    elif parse_datetime(completed_at, "sentinel close completed timestamp") < prepared_time:
        raise CloseError("sentinel close receipt completion predates preparation.")
    return record


def existing_close_record(path: Path, binding: dict[str, object]) -> tuple[bytes, dict[str, object]] | None:
    if not path.exists() and not path.is_symlink():
        return None
    raw, evidence = read_regular_file(path, "sentinel close receipt")
    if evidence.mode != 0o600 or evidence.uid != os.getuid():
        raise CloseError("sentinel close receipt must be owner-private and owned by the current user.")
    return raw, validate_close_record(raw, binding)


def close_condition(live: SentinelLive) -> str:
    return f"#{{&&:{tmux_condition(live.pane, 'sleep')},#{{==:#{{window_panes}},1}}}}"


def guarded_sentinel_close(live: SentinelLive) -> None:
    accepted = f"OMO_MANAGER_SENTINEL_CLOSE_ACCEPTED_{os.getpid()}_{time.monotonic_ns()}"
    rejected = f"OMO_MANAGER_SENTINEL_CLOSE_REJECTED_{os.getpid()}_{time.monotonic_ns()}"
    kill = shlex.join(["kill-pane", "-t", live.pane.pane_id])
    result = subprocess.run(
        [
            "tmux",
            "if-shell",
            "-F",
            "-t",
            live.pane.canonical_target,
            close_condition(live),
            f"display-message -p {accepted} ; {kill}",
            f"display-message -p {rejected}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise CloseError("tmux rejected the exact one-pane inert-sentinel close guard.")


def closed_target_absent(live: SentinelLive) -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{window_id}\t#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.casefold()
        if "no server running" in error or "failed to connect to server" in error:
            return True
        raise CloseError("tmux pane inventory failed while confirming sentinel closure.")
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 3
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", fields[0]) is None
            or re.fullmatch(r"@[1-9][0-9]*", fields[1]) is None
            or re.fullmatch(r"%[1-9][0-9]*", fields[2]) is None
        ):
            raise CloseError("tmux returned a malformed pane inventory after sentinel closure.")
        if fields[0] == live.pane.canonical_target or fields[1] == live.pane.window_id or fields[2] == live.pane.pane_id:
            return False
    return True


def wait_closed(live: SentinelLive) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if closed_target_absent(live):
            return
        time.sleep(0.05)
    if not closed_target_absent(live):
        raise CloseError("the receipt-bound containment pane/window remained live after its guarded close.")


def process_binding_absent(process: ProcessIdentity) -> bool:
    processes = process_snapshot()
    return all(
        not (
            (value.pid == process.pid and value.start_ticks == process.start_ticks)
            or value.session == process.session
            or value.process_group == process.process_group
            or (process.tty and value.tty == process.tty)
        )
        for value in processes.values()
    )


def complete_receipt(path: Path, prepared: bytes, record: dict[str, object], completion_kind: str) -> None:
    complete = close_record(
        cast(dict[str, object], record["binding"]),
        "complete",
        cast(str, record["prepared_at"]),
        completion_kind=completion_kind,
        completed_at=datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - tests also run on Python 3.10
    )
    replace_private_exact(path, prepared, receipt_bytes(complete))


def close_contained_sentinel(args: Args) -> tuple[str, Path | None]:
    initial_containment = containment_receipt(args.bridge)
    initial_task = current_task_binding(args)
    initial_authority = authority_binding(args)
    binding = close_binding(args, initial_containment, initial_task, initial_authority)
    require_private_parent(args.close_receipt, "sentinel close receipt")
    if args.bridge.ownership_receipt.exists() or args.bridge.ownership_receipt.is_symlink():
        raise CloseError("a successor ownership receipt already exists; close-only custody is ambiguous.")
    with close_locks(args, Path(initial_containment.audit.state_dir)):
        if args.bridge.ownership_receipt.exists() or args.bridge.ownership_receipt.is_symlink():
            raise CloseError("a successor ownership receipt appeared before close-only execution.")
        rebound_containment = containment_receipt(args.bridge)
        rebound_task = current_task_binding(args)
        rebound_authority = authority_binding(args)
        if rebound_containment != initial_containment or rebound_task != initial_task or rebound_authority != initial_authority:
            raise CloseError("containment receipt, current task, or Human authority changed before sentinel closure.")
        existing = existing_close_record(args.close_receipt, binding)
        if existing is not None:
            prepared, record = existing
            if record["state"] == "complete":
                if not closed_binding_absent(binding):
                    raise CloseError("a complete close receipt conflicts with a live receipt-bound sentinel.")
                return "already-closed-without-successor", args.close_receipt
            if args.dry_run:
                raise CloseError("--dry-run does not reconcile an existing prepared close receipt.")
            if closed_binding_absent(binding):
                complete_receipt(args.close_receipt, prepared, record, "recovered-target-absent")
                return "closed-without-successor", args.close_receipt
            live = sentinel_live(args, rebound_containment)
        else:
            live = sentinel_live(args, rebound_containment)
            _ = revalidate_sentinel(args, rebound_containment, live)
            if authority_binding(args) != rebound_authority:
                raise CloseError("Human authority changed during close-only preflight.")
            if args.dry_run:
                return "sentinel-close-ready", None
            record = close_record(
                binding,
                "prepared-or-committed",
                datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - tests also run on Python 3.10
            )
            prepared = receipt_bytes(record)
            write_private_exclusive(args.close_receipt, prepared)
        _ = revalidate_sentinel(args, rebound_containment, live)
        if current_task_binding(args) != rebound_task:
            raise CloseError("current task or TODO changed immediately before sentinel closure.")
        if authority_binding(args) != rebound_authority:
            raise CloseError("Human authority changed immediately before sentinel closure.")
        guarded_sentinel_close(live)
        wait_closed(live)
        if not process_binding_absent(live.sentinel):
            raise CloseError("the receipt-bound sentinel process session/group survived pane closure.")
        if current_task_binding(args) != rebound_task:
            raise CloseError("current task or TODO changed across sentinel closure.")
        complete_receipt(args.close_receipt, prepared, record, "guarded-close-confirmed")
        return "closed-without-successor", args.close_receipt


def closed_binding_absent(binding: dict[str, object]) -> bool:
    pane_record = binding.get("pane")
    if not isinstance(pane_record, dict):
        raise CloseError("sentinel close binding lacks its exact pane identity.")
    pane = cast(dict[str, object], pane_record)
    window_id = pane.get("window_id")
    pane_id = pane.get("pane_id")
    canonical_target = pane.get("canonical_target")
    if not isinstance(window_id, str) or not isinstance(pane_id, str) or not isinstance(canonical_target, str):
        raise CloseError("sentinel close binding has a malformed pane identity.")
    process_record = binding.get("contained_process")
    if not isinstance(process_record, dict):
        raise CloseError("sentinel close binding lacks its exact process identity.")
    process_values = cast(dict[str, object], process_record)
    try:
        process = ProcessIdentity(
            int(process_values["pid"]),
            int(process_values["ppid"]),
            str(process_values["state"]),
            int(process_values["process_group"]),
            int(process_values["session"]),
            int(process_values["tty"]),
            int(process_values["start_ticks"]),
            str(process_values["argv_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CloseError("sentinel close binding has a malformed process identity.") from exc
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{window_id}\t#{pane_id}"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.casefold()
        if "no server running" in error or "failed to connect to server" in error:
            return process_binding_absent(process)
        raise CloseError("tmux pane inventory failed while reconciling sentinel closure.")
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 3
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", fields[0]) is None
            or re.fullmatch(r"@[1-9][0-9]*", fields[1]) is None
            or re.fullmatch(r"%[1-9][0-9]*", fields[2]) is None
        ):
            raise CloseError("tmux returned a malformed pane inventory while reconciling sentinel closure.")
        if fields[0] == canonical_target or fields[1] == window_id or fields[2] == pane_id:
            return False
    return process_binding_absent(process)


def main(argv: list[str] | None = None) -> int:
    try:
        result, receipt = close_contained_sentinel(parse_args(sys.argv[1:] if argv is None else argv))
    except (BridgeError, CloseError, ContainmentError, OSError, TaskFrontmatterError, subprocess.SubprocessError) as exc:
        print(f"omo_manager_containment_close: {exc}", file=sys.stderr)
        return 2
    print(f"result: {result}")
    if receipt is not None:
        print(f"receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
