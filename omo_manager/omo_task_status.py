#!/usr/bin/env python3
"""Safely update task-file frontmatter status."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TASK_FRONTMATTER_STATUSES
from omo_manager.omo_agent_status import TaskMetadata
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_agent_status import TASK_RE
from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import load_yaml_mapping
from omo_manager.omo_blocking import only_wake_pending_markers
from omo_manager.omo_blocking import render_task
from omo_manager.omo_blocking import split_task_text
from omo_manager.omo_blocking import V2_VERSION
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import capture
from omo_manager.omo_codex_stop import close_note
from omo_manager.omo_codex_stop import close_exited_codex_shell
from omo_manager.omo_codex_stop import has_close_note
from omo_manager.omo_codex_stop import moved_todo_text
from omo_manager.omo_codex_stop import pane_id
from omo_manager.omo_codex_stop import record_close
from omo_manager.omo_codex_stop import stop
try:
    from omo_manager.omo_codex_stop import validate_human_close_authorization as _validate_human_close_authorization
except ImportError:
    _validate_human_close_authorization = None
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_task_metadata import TARGET_RE
from omo_manager.omo_task_metadata import UniqueKeyLoader
from omo_manager.omo_blocking_actor import request as blocking_request

PENDING_MARKER = "(pending)"
DONE_REMINDER = "Status set to done. Remember to email the human."
BOOKKEEPING_FAILED_PREFIX = "done_close_bookkeeping_failed"
CLOSE_FAILED_PREFIX = "done_close_failed"
DONE_CLOSE_IN_PROGRESS = "done_close_in_progress: manager is closing the agent before marking done"
TODO_ROW_RE = re.compile(r"\s*`?([A-Za-z0-9_./-]+\.md)`?(?:\s+(.*?))?\s*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
CUSTODY_RECEIPT_VERSION = "v1.0.0"


def root_membership_lock(root: Path):
    """Serialize task creation/manager ownership membership with closure scans."""
    return task_file_lock(root / ".omo-task-membership.lock")


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    status: str
    blocked_on: str
    finish_closed_done: bool = False
    session_id: str = ""
    finish_replaced_done: bool = False
    replacement_task: Path | None = None
    stale_target: str = ""
    replacement_target: str = ""
    stale_sha256: str = ""
    replacement_sha256: str = ""
    replacement_status: str = ""
    protected_targets: tuple[str, ...] = ()
    stopped_evidence: str = ""
    replacement_pane_evidence: str = ""
    audit_output: Path | None = None
    recover_exited_shell_done: bool = False
    pane_id: str = ""
    terminal_evidence: str = ""
    retire_blocked_target: bool = False
    reconcile_long_running_human_index: bool = False
    reconcile_blocked_index: bool = False
    closure_repository: Path | None = None
    dirty_path_handoff: Path | None = None
    restore_terminal_target: bool = False
    historical_target: str = ""
    task_sha256: str = ""
    historical_commit: str = ""
    close_shared_target: bool = False
    close_retired_done: bool = False
    normalize_retired_todo: bool = False
    normalize_low_priority_current: bool = False
    shared_target: str = ""
    active_target: str = ""
    manager_target: str = ""
    source_sha256: str = ""
    human_close_authorization_source: str = ""
    human_close_authorization_sha256: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: Path
    status: str = ""
    blocked_on: str = ""
    finish_closed_done: bool = False
    session_id: str = ""
    finish_replaced_done: bool = False
    replacement_task: Path | None = None
    stale_target: str = ""
    replacement_target: str = ""
    stale_sha256: str = ""
    replacement_sha256: str = ""
    replacement_status: str = ""
    protected_target: list[str] = []
    stopped_evidence: str = ""
    replacement_pane_evidence: str = ""
    audit_output: Path | None = None
    recover_exited_shell_done: bool = False
    pane_id: str = ""
    terminal_evidence: str = ""
    retire_blocked_target: bool = False
    reconcile_long_running_human_index: bool = False
    reconcile_blocked_index: bool = False
    closure_repository: Path | None = None
    dirty_path_handoff: Path | None = None
    restore_terminal_target: bool = False
    historical_target: str = ""
    task_sha256: str = ""
    historical_commit: str = ""
    close_shared_target: bool = False
    close_retired_done: bool = False
    normalize_retired_todo: bool = False
    normalize_low_priority_current: bool = False
    shared_target: str = ""
    active_target: str = ""
    manager_target: str = ""
    source_sha256: str = ""
    human_close_authorization_source: str = ""
    human_close_authorization_sha256: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""The helper refuses status changes while the task has a live
`(pending)` marker. It also refuses `done` while pending_task_items is nonempty.
Use the `done` status for normal task closure: it owns TODO movement and worker
shutdown.""",
    )
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--finish-closed-done", action="store_true", help="Finish done bookkeeping after the agent was already closed by a failed prior run.")
    _ = parser.add_argument("--finish-replaced-done", action="store_true", help="Finish a stopped stale record without signaling its pane after proving an explicit live replacement.")
    _ = parser.add_argument("--recover-exited-shell-done", action="store_true", help="Close and finish one blocked worker whose completed Codex session exited to an unchanged shell.")
    _ = parser.add_argument("--retire-blocked-target", action="store_true", help="Atomically retire one blocked human-pending worker target that conflicts with one live lifecycle owner; performs no tmux action.")
    _ = parser.add_argument("--reconcile-long-running-human-index", action="store_true", help="Move one unchanged long_running task with exact human blocker from TODO current to human pending without changing task or pane state.")
    _ = parser.add_argument("--reconcile-blocked-index", action="store_true", help="Move one digest-bound v1 blocked worker with an open queue from TODO previous to human pending without changing task or pane state.")
    _ = parser.add_argument("--session-id", default="", help="Session id captured by the prior close, if available.")
    _ = parser.add_argument("--replacement-task", type=Path, help="Active replacement task file; required with --finish-replaced-done.")
    _ = parser.add_argument("--stale-target", help="Exact stopped target recorded by the stale task; required with --finish-replaced-done.")
    _ = parser.add_argument("--replacement-target", help="Exact live target recorded by the successor task; required with --finish-replaced-done.")
    _ = parser.add_argument("--stale-sha256", help="Expected SHA-256 of the stale task bytes; required with --finish-replaced-done.")
    _ = parser.add_argument("--replacement-sha256", help="Expected SHA-256 of the successor task bytes; required with --finish-replaced-done.")
    _ = parser.add_argument("--replacement-status", choices=("running", "long_running"), help="Expected active successor status; required with --finish-replaced-done.")
    _ = parser.add_argument("--protected-target", action="append", default=[], help="Target that replacement closure must not touch; repeat the authoritative protected set.")
    _ = parser.add_argument("--stopped-evidence", default="", help="Exact evidence from a prior verified pending-item removal; required with --finish-replaced-done.")
    _ = parser.add_argument("--replacement-pane-evidence", default="", help="Exact text currently visible in the replacement pane; required with --finish-replaced-done.")
    _ = parser.add_argument("--audit-output", type=Path, help="New owner-private audit file; required with --finish-replaced-done.")
    _ = parser.add_argument("--pane-id", default="", help="Exact numeric pane id captured by the failed close; required with --recover-exited-shell-done.")
    _ = parser.add_argument("--terminal-evidence", default="", help="Specific accepted terminal-report token visible before Codex exited; required with --recover-exited-shell-done.")
    _ = parser.add_argument("--closure-repository", type=Path, help="Owned Git repository whose clean or explicitly handed-off tracked state gates done closure.")
    _ = parser.add_argument("--dirty-path-handoff", type=Path, help="Reviewed custody receipt required when --closure-repository has tracked changes.")
    _ = parser.add_argument("--restore-terminal-target", action="store_true", help="Restore one historically proven target on an unchanged done/retired record without pane or TODO action.")
    _ = parser.add_argument("--historical-target", default="", help="Exact proven prior target required with --restore-terminal-target.")
    _ = parser.add_argument("--task-sha256", default="", help="Exact current task digest required with --restore-terminal-target.")
    _ = parser.add_argument("--historical-commit", default="", help="Full Git commit containing the proven prior target; required with --restore-terminal-target.")
    _ = parser.add_argument("--close-shared-target", action="store_true", help="Close one explicitly proven manager record on a shared target using metadata and TODO files only; never accesses tmux.")
    _ = parser.add_argument("--close-retired-done", action="store_true", help="Close one already-stopped blocked/retired worker using Git-proven historical target and recorded close evidence; never accesses tmux.")
    _ = parser.add_argument("--normalize-retired-todo", action="store_true", help="Normalize the sole targetless human-pending row for one already-retired blocked worker; never accesses tmux or task bytes.")
    _ = parser.add_argument("--normalize-low-priority-current", action="store_true", help="Move the sole low-priority TODO row for one exact active v1 manager record to current; never accesses tmux.")
    _ = parser.add_argument("--shared-target", default="", help="Exact shared manager target required with --close-shared-target.")
    _ = parser.add_argument("--active-target", default="", help="Exact active task target required with --normalize-low-priority-current.")
    _ = parser.add_argument("--manager-target", default="", help="Exact manager owner target required with --normalize-low-priority-current.")
    _ = parser.add_argument("--source-sha256", default="", help="Exact SHA-256 of the source task bytes required with --close-shared-target, --close-retired-done, or --normalize-retired-todo.")
    _ = parser.add_argument("--human-close-authorization-source", default="", help="Exact manager_mail/<id>.txt record that directly authorizes closing this human-owned task target during normal done closure.")
    _ = parser.add_argument("--human-close-authorization-sha256", default="", help="Lowercase SHA-256 of that exact human-close authorization record.")
    _ = parser.add_argument("task_file", type=Path)
    _ = parser.add_argument("status", nargs="?", choices=sorted(TASK_FRONTMATTER_STATUSES))
    _ = parser.add_argument("--blocked-on", default="", help="Required when setting status to `blocked`; optional for `long_running`; removed for other statuses.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    human_close_authority = (
        parsed.human_close_authorization_source.strip(),
        parsed.human_close_authorization_sha256.strip(),
    )
    if any(human_close_authority) and not all(human_close_authority):
        parser.error("human-close authorization requires both source and digest.")
    if parsed.closure_repository is not None and (parsed.status != "done" or any((parsed.finish_closed_done, parsed.finish_replaced_done, parsed.recover_exited_shell_done, parsed.retire_blocked_target, parsed.reconcile_long_running_human_index))):
        parser.error("--closure-repository is only valid with a normal done transition.")
    if parsed.closure_repository is not None and not parsed.closure_repository.is_absolute():
        parser.error("--closure-repository must be an explicit absolute Git worktree root.")
    if parsed.dirty_path_handoff is not None and parsed.closure_repository is None:
        parser.error("--dirty-path-handoff requires --closure-repository.")
    if sum((parsed.finish_closed_done, parsed.finish_replaced_done, parsed.recover_exited_shell_done, parsed.retire_blocked_target, parsed.reconcile_long_running_human_index, parsed.reconcile_blocked_index, parsed.restore_terminal_target, parsed.close_shared_target, parsed.close_retired_done, parsed.normalize_retired_todo, parsed.normalize_low_priority_current)) > 1:
        parser.error("finish and recovery modes are mutually exclusive.")
    if (parsed.active_target or parsed.manager_target) and not parsed.normalize_low_priority_current:
        parser.error("--active-target and --manager-target are only valid with --normalize-low-priority-current.")
    if any(human_close_authority) and (
        parsed.status != "done"
        or any((parsed.finish_closed_done, parsed.finish_replaced_done, parsed.recover_exited_shell_done, parsed.retire_blocked_target, parsed.reconcile_long_running_human_index, parsed.reconcile_blocked_index, parsed.restore_terminal_target, parsed.close_shared_target, parsed.close_retired_done, parsed.normalize_retired_todo, parsed.normalize_low_priority_current))
    ):
        parser.error("human-close authorization is valid only for a normal done transition.")
    if parsed.retire_blocked_target:
        parser.error("--retire-blocked-target is disabled: preserve the historical target and resolve ownership without writing retired semantics.")
    if parsed.restore_terminal_target:
        if parsed.status not in {None, ""} or TARGET_RE.fullmatch(parsed.historical_target.strip()) is None or SHA256_RE.fullmatch(parsed.task_sha256.strip()) is None or GIT_COMMIT_RE.fullmatch(parsed.historical_commit.strip()) is None:
            parser.error("--restore-terminal-target requires --historical-target TARGET, full --historical-commit, and lowercase --task-sha256, without status.")
        if any((parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff)):
            parser.error("unrelated lifecycle, replacement, pane, and repository evidence is not valid with --restore-terminal-target.")
        return Args(parsed.root.resolve(), parsed.task_file, "", "", restore_terminal_target=True, historical_target=parsed.historical_target.strip(), task_sha256=parsed.task_sha256.strip(), historical_commit=parsed.historical_commit.strip())
    if parsed.close_retired_done:
        unrelated = (parsed.status, parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff, parsed.shared_target, parsed.task_sha256)
        if any(unrelated) or TARGET_RE.fullmatch(parsed.historical_target.strip()) is None or SHA256_RE.fullmatch(parsed.source_sha256.strip()) is None or GIT_COMMIT_RE.fullmatch(parsed.historical_commit.strip()) is None:
            parser.error("--close-retired-done requires --historical-target TARGET, full --historical-commit, and lowercase --source-sha256, without lifecycle or pane evidence.")
        return Args(parsed.root.resolve(), parsed.task_file, "done", "", close_retired_done=True, historical_target=parsed.historical_target.strip(), historical_commit=parsed.historical_commit.strip(), source_sha256=parsed.source_sha256.strip())
    if parsed.normalize_retired_todo:
        unrelated = (parsed.status, parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff, parsed.historical_target, parsed.task_sha256, parsed.historical_commit, parsed.shared_target)
        if any(unrelated) or SHA256_RE.fullmatch(parsed.source_sha256.strip()) is None:
            parser.error("--normalize-retired-todo requires only lowercase --source-sha256 for one exact blocked/retired source task.")
        return Args(parsed.root.resolve(), parsed.task_file, "", "", normalize_retired_todo=True, source_sha256=parsed.source_sha256.strip())
    if parsed.normalize_low_priority_current:
        unrelated = (parsed.status, parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff, parsed.historical_target, parsed.task_sha256, parsed.historical_commit, parsed.shared_target)
        active_target = parsed.active_target.strip()
        manager_target = parsed.manager_target.strip()
        if any(unrelated) or TARGET_RE.fullmatch(active_target) is None or TARGET_RE.fullmatch(manager_target) is None or SHA256_RE.fullmatch(parsed.source_sha256.strip()) is None:
            parser.error("--normalize-low-priority-current requires exact --active-target, --manager-target, and lowercase --source-sha256, without lifecycle or repository evidence.")
        if active_target.partition(":")[0].startswith("h") or manager_target.partition(":")[0].startswith("h"):
            parser.error("--normalize-low-priority-current cannot modify a human-owned `h*` target.")
        return Args(parsed.root.resolve(), parsed.task_file, "", "", normalize_low_priority_current=True, active_target=active_target, manager_target=manager_target, source_sha256=parsed.source_sha256.strip())
    if parsed.close_shared_target:
        unrelated = (parsed.status, parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff, parsed.historical_target, parsed.task_sha256, parsed.historical_commit)
        if any(unrelated) or TARGET_RE.fullmatch(parsed.shared_target.strip()) is None or SHA256_RE.fullmatch(parsed.source_sha256.strip()) is None:
            parser.error("--close-shared-target requires exact --shared-target and lowercase --source-sha256, without lifecycle or repository evidence.")
        return Args(parsed.root.resolve(), parsed.task_file, "done", "", close_shared_target=True, shared_target=parsed.shared_target.strip(), source_sha256=parsed.source_sha256.strip())
    if parsed.reconcile_long_running_human_index:
        if parsed.status not in {None, ""} or parsed.blocked_on:
            parser.error("--reconcile-long-running-human-index does not accept status or --blocked-on.")
        if any((parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence)):
            parser.error("unrelated lifecycle evidence is not valid with --reconcile-long-running-human-index.")
        return Args(parsed.root.resolve(), parsed.task_file, "", "", reconcile_long_running_human_index=True)
    if parsed.reconcile_blocked_index:
        unrelated = (parsed.status, parsed.blocked_on, parsed.session_id, parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output, parsed.pane_id, parsed.terminal_evidence, parsed.closure_repository, parsed.dirty_path_handoff, parsed.historical_target, parsed.task_sha256, parsed.historical_commit, parsed.shared_target, parsed.active_target, parsed.manager_target)
        if any(unrelated) or SHA256_RE.fullmatch(parsed.source_sha256.strip()) is None:
            parser.error("--reconcile-blocked-index requires only lowercase --source-sha256 for one exact blocked source task.")
        return Args(parsed.root.resolve(), parsed.task_file, "", "", reconcile_blocked_index=True, source_sha256=parsed.source_sha256.strip())
    if parsed.recover_exited_shell_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--recover-exited-shell-done only supports status `done`.")
        if not parsed.session_id.strip() or not parsed.pane_id.strip() or not parsed.terminal_evidence.strip():
            parser.error("--recover-exited-shell-done requires --session-id, --pane-id, and --terminal-evidence.")
        if any((parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output)):
            parser.error("replacement evidence is only valid with --finish-replaced-done.")
        return Args(
            parsed.root.resolve(),
            parsed.task_file,
            "done",
            parsed.blocked_on.strip(),
            session_id=parsed.session_id.strip(),
            recover_exited_shell_done=True,
            pane_id=parsed.pane_id.strip(),
            terminal_evidence=parsed.terminal_evidence.strip(),
        )
    if parsed.finish_replaced_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--finish-replaced-done only supports status `done`.")
        if parsed.session_id:
            parser.error("--session-id is only valid with --finish-closed-done.")
        if parsed.pane_id or parsed.terminal_evidence:
            parser.error("pane and terminal evidence are only valid with --recover-exited-shell-done.")
        required = (
            parsed.replacement_task,
            parsed.stale_target.strip(),
            parsed.replacement_target.strip(),
            parsed.stale_sha256.strip(),
            parsed.replacement_sha256.strip(),
            parsed.replacement_status.strip(),
            parsed.protected_target,
            parsed.stopped_evidence.strip(),
            parsed.replacement_pane_evidence.strip(),
            parsed.audit_output,
        )
        if not all(required) or parsed.replacement_task is None or parsed.audit_output is None:
            parser.error("--finish-replaced-done requires explicit stale/successor task, target, digest, status, evidence, and audit output values.")
        if SHA256_RE.fullmatch(parsed.stale_sha256.strip()) is None or SHA256_RE.fullmatch(parsed.replacement_sha256.strip()) is None:
            parser.error("replacement task digests must be lowercase SHA-256 values.")
        if any(TARGET_RE.fullmatch(target) is None for target in parsed.protected_target):
            parser.error("--protected-target values must be exact SESSION:WINDOW[.PANE] targets.")
        return Args(
            parsed.root.resolve(),
            parsed.task_file,
            "done",
            parsed.blocked_on.strip(),
            finish_replaced_done=True,
            replacement_task=parsed.replacement_task.expanduser().resolve(strict=False),
            stale_target=parsed.stale_target.strip(),
            replacement_target=parsed.replacement_target.strip(),
            stale_sha256=parsed.stale_sha256.strip(),
            replacement_sha256=parsed.replacement_sha256.strip(),
            replacement_status=parsed.replacement_status.strip(),
            protected_targets=tuple(parsed.protected_target),
            stopped_evidence=parsed.stopped_evidence.strip(),
            replacement_pane_evidence=parsed.replacement_pane_evidence.strip(),
            audit_output=parsed.audit_output.expanduser().resolve(strict=False),
        )
    if parsed.finish_closed_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--finish-closed-done only supports status `done`.")
        if parsed.pane_id or parsed.terminal_evidence:
            parser.error("pane and terminal evidence are only valid with --recover-exited-shell-done.")
        if any((parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output)):
            parser.error("replacement evidence is only valid with --finish-replaced-done.")
        return Args(parsed.root.resolve(), parsed.task_file, "done", parsed.blocked_on.strip(), True, parsed.session_id.strip())
    if not parsed.status:
        parser.error("status is required unless a finish or recovery mode is used.")
    if parsed.session_id:
        parser.error("--session-id is only valid with --finish-closed-done.")
    if any((parsed.replacement_task, parsed.stale_target, parsed.replacement_target, parsed.stale_sha256, parsed.replacement_sha256, parsed.replacement_status, parsed.protected_target, parsed.stopped_evidence, parsed.replacement_pane_evidence, parsed.audit_output)):
        parser.error("replacement evidence is only valid with --finish-replaced-done.")
    if parsed.pane_id or parsed.terminal_evidence:
        parser.error("pane and terminal evidence are only valid with --recover-exited-shell-done.")
    return Args(
        parsed.root.resolve(),
        parsed.task_file,
        parsed.status,
        parsed.blocked_on.strip(),
        closure_repository=parsed.closure_repository.expanduser().resolve(strict=False) if parsed.closure_repository is not None else None,
        dirty_path_handoff=parsed.dirty_path_handoff.expanduser().resolve(strict=False) if parsed.dirty_path_handoff is not None else None,
        human_close_authorization_source=human_close_authority[0],
        human_close_authorization_sha256=human_close_authority[1],
    )


def task_path(root: Path, task_file: Path) -> Path:
    path = task_file.expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=False)
    if path != root and root not in path.parents:
        raise TaskFrontmatterError("task file escapes root.")
    return path


def relative_task_ref(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def tracked_dirty_state(repository: Path) -> tuple[bytes, dict[str, str]]:
    """Return exact tracked porcelain bytes and path states; ignore untracked files."""

    result = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=no"],
        check=True,
        capture_output=True,
    )
    fields = result.stdout.split(b"\0")
    states: dict[str, str] = {}
    index = 0
    while index < len(fields) - 1:
        field = fields[index]
        if len(field) < 4 or field[2:3] != b" ":
            raise TaskFrontmatterError("Git returned malformed tracked dirty state.")
        try:
            state = field[:2].decode("ascii")
            path = field[3:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TaskFrontmatterError("tracked dirty paths must be UTF-8 for a durable YAML custody receipt") from exc
        if "R" in state or "C" in state:
            index += 1
            if index >= len(fields) - 1:
                raise TaskFrontmatterError("Git returned incomplete rename/copy state.")
        if path in states:
            raise TaskFrontmatterError(f"Git returned duplicate tracked dirty path: {path}")
        states[path] = state
        index += 1
    return result.stdout, states


def ensure_repository_closure_custody(repository: Path, receipt_path: Path | None) -> None:
    """Require clean tracked state or an exact durable owner assignment for every dirty path."""

    if not repository.is_dir():
        raise TaskFrontmatterError("closure repository must be an existing directory explicitly named by its owner")
    try:
        status_bytes, dirty = tracked_dirty_state(repository)
    except subprocess.CalledProcessError as exc:
        raise TaskFrontmatterError("closure repository must be an explicitly named Git worktree") from exc
    top_level = subprocess.run(["git", "-C", str(repository), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()
    if Path(top_level).resolve(strict=True) != repository.resolve(strict=True):
        raise TaskFrontmatterError("closure repository must name the exact Git worktree root, not a subdirectory")
    if not dirty:
        if receipt_path is not None:
            raise TaskFrontmatterError("dirty-path handoff is not valid for a clean tracked repository")
        return
    if receipt_path is None:
        raise TaskFrontmatterError(f"tracked repository changes require an explicit dirty-path ownership handoff: {','.join(sorted(dirty))}")
    try:
        value = yaml.load(receipt_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, TaskFrontmatterError) as exc:
        raise TaskFrontmatterError(f"cannot read dirty-path ownership handoff: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"version", "repository", "status_sha256", "assignments"} or value["version"] != CUSTODY_RECEIPT_VERSION:
        raise TaskFrontmatterError("dirty-path handoff must contain only version v1.0.0, repository, status_sha256, and assignments")
    expected_repository = repository.resolve(strict=True).as_posix()
    if value["repository"] != expected_repository or value["status_sha256"] != hashlib.sha256(status_bytes).hexdigest() or not isinstance(value["assignments"], list):
        raise TaskFrontmatterError("dirty-path handoff does not bind the current repository and tracked status snapshot")
    assigned: dict[str, str] = {}
    for assignment in value["assignments"]:
        if not isinstance(assignment, dict) or set(assignment) != {"path", "state", "owner", "evidence"}:
            raise TaskFrontmatterError("each dirty-path assignment must contain only path, state, owner, and evidence")
        path, state, owner, evidence = assignment["path"], assignment["state"], assignment["owner"], assignment["evidence"]
        if not all(isinstance(item, str) and item.strip() for item in (path, state, owner, evidence)) or Path(path).is_absolute() or Path(path).as_posix() != path or ".." in Path(path).parts:
            raise TaskFrontmatterError("dirty-path assignments require canonical relative paths, exact states, and nonempty owner/evidence")
        if path in assigned:
            raise TaskFrontmatterError(f"duplicate dirty-path assignment: {path}")
        assigned[path] = state
    if assigned != dirty:
        raise TaskFrontmatterError("dirty-path handoff must assign every and only current tracked modified/deleted path with its exact state")


def frontmatter_managerat_aliases(text: str, manager_target: str) -> bool:
    parts = frontmatter_parts(text)
    if parts is None:
        return False
    frontmatter, _body = parts
    for line in frontmatter:
        key, sep, value = line.partition(":")
        if sep and key.strip() == "managerat" and same_tmux_target(value.strip(), manager_target):
            return True
    return False


def parse_manager_child_metadata(text: str, work_log_root: Path | None = None) -> TaskMetadata | None:
    """Validate historical `done` plus `retired` children without changing active-task rules."""
    parts = frontmatter_parts(text)
    if parts is None:
        return None
    frontmatter, body = parts
    fields = {key: value.strip() for line in frontmatter for key, sep, value in (line.partition(":"),) if sep}
    if fields.get("status") != "done" or fields.get("runat") != "retired":
        return parse_task_metadata(text, work_log_root)
    compatible: list[str] = []
    for line in frontmatter:
        key, sep, _value = line.partition(":")
        compatible.append("status: blocked" if sep and key == "status" else line)
        if sep and key == "status":
            compatible.append("blocked_on: archived completed task")
    trailing_newline = "\n" if text.endswith("\n") else ""
    validated = parse_task_metadata("\n".join(["---", *compatible, "---", *body]) + trailing_newline, work_log_root)
    if validated is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    return replace(validated, status="done", blocked_on="")


def active_child_task_refs(root: Path, manager_path: Path, manager_target: str) -> tuple[str, ...]:
    """Return active task files whose `managerat` still points at the closing manager."""
    refs: list[str] = []
    for candidate in sorted(root.rglob("*.md")):
        if candidate == manager_path:
            continue
        task_ref = relative_task_ref(root, candidate)
        try:
            text = candidate.read_text(encoding="utf-8")
            metadata = parse_manager_child_metadata(text, root)
        except OSError as exc:
            raise TaskFrontmatterError(f"cannot verify manager child ownership because `{task_ref}` could not be read: {exc}") from exc
        except TaskFrontmatterError as exc:
            if frontmatter_managerat_aliases(text, manager_target):
                raise TaskFrontmatterError(f"cannot verify manager child ownership because `{task_ref}` has invalid task frontmatter: {exc}") from exc
            continue
        if metadata is None or metadata.status == "done":
            continue
        if same_tmux_target(metadata.managerat, manager_target):
            refs.append(task_ref)
    return tuple(refs)


def manager_owner_migration_command(root: Path, child_ref: str, old_manager: str, new_manager: str) -> str:
    return " ".join(
        (
            "omo_task.py",
            "--root",
            shlex.quote(root.as_posix()),
            "--task-file",
            shlex.quote(child_ref),
            "--migrate-manager-owner",
            "--old-manager-target",
            shlex.quote(old_manager),
            "--new-manager-target",
            shlex.quote(new_manager),
        )
    )


def ensure_manager_has_no_active_children(root: Path, manager_path: Path, metadata: TaskMetadata) -> None:
    if not metadata.is_manager:
        return
    child_refs = active_child_task_refs(root, manager_path, metadata.runat)
    if not child_refs:
        return
    shown = ", ".join(child_refs[:5])
    if len(child_refs) > 5:
        shown = f"{shown}, and {len(child_refs) - 5} more"
    command = manager_owner_migration_command(root, child_refs[0], metadata.runat, metadata.managerat)
    raise TaskFrontmatterError(
        f"manager task still owns active child task(s): {shown}. "
        f"Before marking this manager done, reassign each child from managerat {metadata.runat} to {metadata.managerat}; for example: `{command}`."
    )


def has_pending_marker(text: str) -> bool:
    """Return true when the task body still contains a live pending marker."""
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if not in_fence and stripped == PENDING_MARKER:
            return True
    return False


def update_frontmatter_status(text: str, status: str, blocked_on: str, work_log_root: Path | None = None) -> str:
    """Return task text with validated `status` and `blocked_on` frontmatter."""
    metadata = parse_task_metadata(text, work_log_root)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    if has_pending_marker(text) and not (metadata.version == V2_VERSION and only_wake_pending_markers(text)):
        raise TaskFrontmatterError("task file still contains `(pending)`; handle pending markers before changing status.")
    if status == "done" and metadata.pending_task_items:
        raise TaskFrontmatterError(
            "task file still has `pending_task_items`; verify each pending item is actually complete or cancelled, then remove it before marking done."
        )
    if status == "blocked" and not blocked_on:
        raise TaskFrontmatterError("`--blocked-on` is required when setting status to `blocked`.")
    if "\n" in blocked_on or "\r" in blocked_on:
        raise TaskFrontmatterError("`--blocked-on` must be one line.")
    if status not in {"blocked", "long_running"} and blocked_on:
        raise TaskFrontmatterError("`--blocked-on` is only valid when setting status to `blocked` or `long_running`.")
    if metadata.version == V2_VERSION:
        frontmatter_text, body_text = split_task_text(text)
        values = load_yaml_mapping(frontmatter_text)
        generated = [blocker for blocker in values.get("blocked_on", []) if blocker.get("kind") == "pending_items"]
        persistent = [blocker for blocker in values.get("blocked_on", []) if blocker.get("kind") == "persistent"]
        external = [blocker for blocker in values.get("blocked_on", []) if blocker.get("kind") not in {"pending_items", "persistent"}]
        if status == "blocked":
            if values["status"] != "blocked":
                values["resume_status"] = values["status"]
            values["status"] = "blocked"
            added = {"kind": "human", "reason": blocked_on}
            values["blocked_on"] = [*generated, *persistent, *external, *([] if added in external else [added])]
        elif generated:
            if status == "done":
                raise TaskFrontmatterError("dependency-blocked task cannot be marked done")
            values["status"] = "blocked"
            values["resume_status"] = status
            role = [{"kind": "persistent", "reason": blocked_on}] if status == "long_running" else []
            values["blocked_on"] = [*generated, *role]
        elif status == "long_running":
            values["status"] = status
            if blocked_on:
                values["blocked_on"] = [{"kind": "persistent", "reason": blocked_on}]
            else:
                values.pop("blocked_on", None)
            values.pop("resume_status", None)
        else:
            values["status"] = status
            values.pop("blocked_on", None)
            values.pop("resume_status", None)
        return render_task(values, body_text, work_log_root)
    parts = frontmatter_parts(text)
    if parts is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    frontmatter, body = parts
    updated: list[str] = []
    inserted_blocked_on = False
    for line in frontmatter:
        key, sep, _value = line.partition(":")
        if not sep:
            updated.append(line)
            continue
        if key == "status":
            updated.append(f"status: {status}")
            if status == "blocked" or (status == "long_running" and blocked_on):
                updated.append(f"blocked_on: {blocked_on}")
                inserted_blocked_on = True
            continue
        if key == "blocked_on":
            continue
        updated.append(line)
    if status == "blocked" and not inserted_blocked_on:
        raise TaskFrontmatterError("frontmatter has no `status` field to attach `blocked_on` after.")
    trailing_newline = "\n" if text.endswith("\n") else ""
    updated_text = "\n".join(["---", *updated, "---", *body]) + trailing_newline
    _ = parse_task_metadata(updated_text, work_log_root)
    return updated_text


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mtime_ns == right.st_mtime_ns and left.st_size == right.st_size


def replace_if_unchanged(path: Path, text: str, before: os.stat_result) -> None:
    """Replace `path` atomically after checking it did not change since read."""
    with task_file_lock(path):
        replace_if_unchanged_locked(path, text, before)


def replace_if_unchanged_locked(path: Path, text: str, before: os.stat_result) -> None:
    """Replace `path` after an outer `task_file_lock` has serialized its writer."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            _ = handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        tmp_path.chmod(before.st_mode & 0o7777)
        after = path.stat()
        if not same_file_state(before, after):
            raise TaskFrontmatterError("task file changed while status update was being prepared; retry after rereading it.")
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def replace_bytes_if_unchanged(path: Path, payload: bytes, before: os.stat_result) -> None:
    """Atomically replace exact bytes after checking the source inode snapshot."""

    with task_file_lock(path):
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            temporary.chmod(before.st_mode & 0o7777)
            if not same_file_state(before, path.stat()):
                raise TaskFrontmatterError("task file changed while target restoration was being prepared; retry after rereading it.")
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def current_target_task_paths(root: Path, target: str) -> tuple[Path, ...]:
    """Return TODO `current` task paths that claim `target` in metadata or TODO text."""

    matches: set[Path] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.section != "todo:current":
            continue
        candidate = (root / task.task_file).resolve(strict=False)
        if candidate != root and root not in candidate.parents:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            if task.target and same_tmux_target(task.target, target):
                matches.add(candidate)
            continue
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError:
            metadata = None
        raw_runat_claim = any(key.strip() == "runat" and sep and same_tmux_target(value.strip(), target) for key, sep, value in (line.partition(":") for line in text.splitlines()))
        if metadata is None:
            if (task.target and same_tmux_target(task.target, target)) or raw_runat_claim:
                matches.add(candidate)
        elif metadata.status != "done" and ((task.target and same_tmux_target(task.target, target)) or same_tmux_target(metadata.runat, target)):
            matches.add(candidate)
    return tuple(sorted(matches))


def authoritative_active_target_task_paths(root: Path, target: str) -> tuple[Path, ...]:
    """Return every valid active task whose frontmatter owns `target`."""

    matches: list[Path] = []
    for candidate in sorted(root.rglob("*.md")):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskFrontmatterError(f"cannot verify target ownership because `{relative_task_ref(root, candidate)}` could not be read: {exc}") from exc
        raw_claim = any(
            key.strip() == "runat" and sep and same_tmux_target(value.strip(), target)
            for key, sep, value in (line.partition(":") for line in text.splitlines())
        )
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError as exc:
            if raw_claim:
                raise TaskFrontmatterError(
                    f"cannot verify target ownership because `{relative_task_ref(root, candidate)}` has invalid task frontmatter: {exc}"
                ) from exc
            continue
        if metadata is not None and metadata.status != "done" and same_tmux_target(metadata.runat, target):
            matches.append(candidate.resolve())
    return tuple(matches)


def worker_self_close_allowed(root: Path, path: Path, metadata: TaskMetadata) -> bool:
    """Allow self-close only for the sole current non-manager owner of its pane."""

    if metadata.is_manager:
        return False
    manager_target = os.environ.get("OMO_MANAGER_TMUX_TARGET", "").strip()
    if manager_target and same_tmux_target(metadata.runat, manager_target):
        return False
    return current_target_task_paths(root, metadata.runat) == (path,)


def human_close_stop_args(root: Path, task_file: str, target: str, source: str, digest: str, authorized_target: str) -> StopArgs:
    """Build a hash-bound human-close request only when the stop helper supports it."""
    try:
        return StopArgs(target, 10.0, 2000, False, False, root, task_file, True, 0.0, source, digest, authorized_target)
    except TypeError as exc:
        raise TaskFrontmatterError("human-close authorization requires the compatible omo_codex_stop helper") from exc


def validate_human_close_authorization(args: StopArgs) -> None:
    """Fail closed until the paired stop helper can validate human authority."""
    if _validate_human_close_authorization is None:
        raise TaskFrontmatterError("human-close authorization requires the compatible omo_codex_stop helper")
    _validate_human_close_authorization(args)


def stop_done_agent(
    root: Path,
    path: Path,
    metadata: TaskMetadata,
    human_close_authorization_source: str = "",
    human_close_authorization_sha256: str = "",
) -> tuple[StopArgs, str]:
    """Close the task's Codex pane and return the captured session id."""

    task_file = path.relative_to(root).as_posix()
    with task_target_lock(root, metadata.runat):
        stable_pane_id = exact_pane_id(metadata.runat)
        human_target = metadata.runat if metadata.runat.partition(":")[0].startswith("h") else ""
        record_args = (
            human_close_stop_args(root, task_file, metadata.runat, human_close_authorization_source, human_close_authorization_sha256, human_target)
            if human_target
            else StopArgs(metadata.runat, 10.0, 2000, False, False, root, task_file, True, 0.0)
        )
        if not stable_pane_id:
            return record_args, ""
        allow_self = bool(stable_pane_id and worker_self_close_allowed(root, path, metadata))
        if allow_self:
            allow_self = worker_self_close_allowed(root, path, metadata) and exact_pane_id(metadata.runat) == stable_pane_id
        stop_args = replace(record_args, target=stable_pane_id, allow_self=allow_self)
        try:
            session_id = stop(stop_args)
        except Exception:
            if exact_pane_id(metadata.runat) or pane_id(stable_pane_id):
                raise
            session_id = ""
    record_args = replace(record_args, allow_self=allow_self)
    return record_args, session_id


def done_close_message(target: str, session_id: str) -> str:
    if session_id:
        return f"Closed {target}; session_id: {session_id}."
    return f"Closed {target}; Codex session id not found."


def done_bookkeeping_failed_reason(exc: Exception) -> str:
    reason = " ".join(str(exc).split())
    return f"{BOOKKEEPING_FAILED_PREFIX}: {reason or exc.__class__.__name__}"


def is_bookkeeping_failed_reason(blocked_on: str) -> bool:
    return blocked_on == BOOKKEEPING_FAILED_PREFIX or blocked_on.startswith(f"{BOOKKEEPING_FAILED_PREFIX}: ")


def is_close_failed_reason(blocked_on: str) -> bool:
    return blocked_on == CLOSE_FAILED_PREFIX or blocked_on.startswith(f"{CLOSE_FAILED_PREFIX}: ")


def task_is_in_todo_section(root: Path, path: Path, section: str) -> bool:
    """Return whether TODO places the exact task in `section`."""

    for task in parse_task_lines(root / "TODO.md"):
        if task.section != f"todo:{section}":
            continue
        candidate = (root / task.task_file).resolve(strict=False)
        if candidate == path:
            return True
    return False


def todo_row_task_paths(root: Path, line: str) -> tuple[Path, ...]:
    """Return root-contained task paths referenced by one TODO row."""
    paths: list[Path] = []
    for match in TASK_RE.finditer(line):
        candidate = Path(match.group(1)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            path = candidate.resolve(strict=False)
        except OSError:
            continue
        if path == root or root in path.parents:
            paths.append(path)
    return tuple(paths)


def validate_reconciled_todo_row(root: Path, path: Path, line: str, runat: str) -> None:
    """Require one unqualified task reference and an optional matching pane."""
    stripped = line.rstrip("\r\n")
    match = TODO_ROW_RE.fullmatch(stripped)
    if match is None or len(list(TASK_RE.finditer(stripped))) != 1 or todo_row_task_paths(root, stripped) != (path,):
        raise TaskFrontmatterError(f"expected one unambiguous TODO entry for `{relative_task_ref(root, path)}`.")
    suffix = match.group(2) or ""
    targets = TARGET_RE.findall(suffix)
    if len(targets) > 1 or (targets and not same_tmux_target(targets[0], runat)):
        raise TaskFrontmatterError(f"TODO entry for `{relative_task_ref(root, path)}` does not match its authoritative `runat`.")
    if re.search(r"\((?:blocked|done)(?::[^)]*)?\)", suffix, re.IGNORECASE) is not None:
        raise TaskFrontmatterError(f"TODO entry for `{relative_task_ref(root, path)}` has a terminal lifecycle annotation.")


def retired_task_text(text: str, stale_target: str, root: Path) -> str:
    """Replace only the authoritative frontmatter run target with `retired`."""
    raise TaskFrontmatterError("target retirement is disabled: retired is not a run target")
    metadata = parse_task_metadata(text, root)
    if metadata is None or metadata.version == V2_VERSION or metadata.status != "blocked" or metadata.blocked_on != "human" or metadata.is_manager or not metadata.pending_task_items:
        raise TaskFrontmatterError("target retirement requires one v1 non-manager task blocked on `human` with a nonempty pending queue.")
    if metadata.runat != stale_target:
        raise TaskFrontmatterError("blocked task `runat` does not equal --stale-target.")
    parts = frontmatter_parts(text)
    if parts is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    lines = text.splitlines(keepends=True)
    closing = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
    replaced = 0
    for idx in range(1, closing):
        line = lines[idx]
        key, sep, value = line.partition(":")
        if sep and key.strip() == "runat":
            if value.strip() != stale_target:
                raise TaskFrontmatterError("blocked task frontmatter run target drifted.")
            newline = line[len(line.rstrip("\r\n")) :]
            lines[idx] = f"runat: retired{newline}"
            replaced += 1
    if replaced != 1:
        raise TaskFrontmatterError(f"expected exactly one frontmatter `runat`, found {replaced}.")
    updated = "".join(lines)
    retired = parse_task_metadata(updated, root)
    if retired is None or retired.runat != "retired":
        raise TaskFrontmatterError("retired task metadata did not validate.")
    return updated


def retire_todo_text(root: Path, path: Path, text: str, stale_target: str) -> str:
    """Replace the sole human-pending task row's exact target with `retired`."""
    raise TaskFrontmatterError("target retirement is disabled: retired is not a run target")
    lines = text.splitlines(keepends=True)
    section = ""
    rows: list[int] = []
    for idx, line in enumerate(lines):
        name = line.strip()
        if name.endswith(":"):
            section = name[:-1].casefold()
            continue
        if path in todo_row_task_paths(root, line):
            if section != "human pending":
                raise TaskFrontmatterError(f"expected `{relative_task_ref(root, path)}` in `human pending:`, found `{section}:`.")
            rows.append(idx)
    if len(rows) != 1:
        raise TaskFrontmatterError(f"expected exactly one TODO row for `{relative_task_ref(root, path)}`, found {len(rows)}.")
    row = lines[rows[0]]
    validate_reconciled_todo_row(root, path, row, stale_target)
    match = TODO_ROW_RE.fullmatch(row.rstrip("\r\n"))
    assert match is not None
    suffix = match.group(2) or ""
    targets = TARGET_RE.findall(suffix)
    if targets != [stale_target]:
        raise TaskFrontmatterError("target retirement requires the TODO row to name the exact stale target once.")
    if row.count(stale_target) != 1:
        raise TaskFrontmatterError("target retirement requires one literal stale target in the TODO row.")
    lines[rows[0]] = row.replace(stale_target, "retired", 1)
    return "".join(lines)


def retire_blocked_target(args: Args, path: Path, text: str, before: os.stat_result) -> None:
    """Atomically retire a blocked worker's conflicting target without tmux access."""
    raise TaskFrontmatterError("target retirement is disabled: retired is not a run target")
    if TARGET_RE.fullmatch(args.stale_target) is None:
        raise TaskFrontmatterError("--stale-target must be an exact SESSION:WINDOW[.PANE] target.")
    if args.stale_target.partition(":")[0].startswith("h"):
        raise TaskFrontmatterError("target retirement cannot modify a human-owned `h*` target.")
    updated_task = retired_task_text(text, args.stale_target, args.root)
    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with task_target_lock(args.root, args.stale_target):
        owners = authoritative_active_target_task_paths(args.root, args.stale_target)
        successors = tuple(owner for owner in owners if owner != path)
        if path not in owners or len(successors) != 1:
            refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
            raise TaskFrontmatterError(f"target retirement requires the stale task and exactly one conflicting owner of `{args.stale_target}`: {refs}.")
        successor_path = successors[0]
        successor_before = successor_path.stat()
        successor_text = successor_path.read_text(encoding="utf-8")
        with ExitStack() as locks:
            for locked_path in sorted({path, todo, successor_path}, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            if not same_file_state(before, current_before) or current_text != text:
                raise TaskFrontmatterError("blocked task changed while target retirement was being prepared; retry after rereading it.")
            if not same_file_state(successor_before, successor_path.stat()) or successor_path.read_text(encoding="utf-8") != successor_text:
                raise TaskFrontmatterError("conflicting target owner changed while retirement was being prepared; retry after rereading it.")
            successor = parse_task_metadata(successor_text, args.root)
            if successor is None or successor.status not in {"running", "long_running"}:
                raise TaskFrontmatterError("conflicting target owner must be running or long_running.")
            if authoritative_active_target_task_paths(args.root, args.stale_target) != owners:
                raise TaskFrontmatterError("authoritative target ownership changed while retirement was being prepared; retry.")
            todo_before = todo.stat()
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = retire_todo_text(args.root, path, todo_text, args.stale_target)
            replace_if_unchanged_locked(todo, updated_todo, todo_before)
            moved_todo_before = todo.stat()
            try:
                replace_if_unchanged_locked(path, updated_task, current_before)
            except Exception as exc:
                try:
                    replace_if_unchanged_locked(todo, todo_text, moved_todo_before)
                except Exception as rollback_exc:
                    raise TaskFrontmatterError(f"blocked target retirement failed and TODO rollback also failed: {rollback_exc}") from exc
                raise


def restore_terminal_target(args: Args, path: Path, text: str, before: os.stat_result) -> None:
    """Restore only a proven historical target on an unchanged terminal record."""

    current_bytes = path.read_bytes()
    if hashlib.sha256(current_bytes).hexdigest() != args.task_sha256:
        raise TaskFrontmatterError("terminal target restoration task digest does not match current bytes")
    try:
        current_text = current_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("terminal task must be UTF-8") from exc
    metadata = parse_manager_child_metadata(current_text, args.root)
    if metadata is None or metadata.version == V2_VERSION or metadata.status != "done" or metadata.runat != "retired" or metadata.pending_task_items:
        raise TaskFrontmatterError("terminal target restoration requires one v1 done/retired task with an empty queue")
    relative = relative_task_ref(args.root, path)
    try:
        repository_root = Path(subprocess.run(["git", "-C", str(args.root), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()).resolve(strict=True)
        if repository_root != args.root.resolve(strict=True):
            raise TaskFrontmatterError("task root must be the exact Git worktree containing historical proof")
        proven_commit = subprocess.run(["git", "-C", str(args.root), "rev-parse", f"{args.historical_commit}^{{commit}}"], check=True, capture_output=True, text=True).stdout.strip()
        if proven_commit != args.historical_commit:
            raise TaskFrontmatterError("historical commit must be the full canonical commit id")
        historical_bytes = subprocess.run(["git", "-C", str(args.root), "show", f"{proven_commit}:{relative}"], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise TaskFrontmatterError("cannot verify historical target from the supplied Git commit and task path") from exc
    try:
        historical_text = historical_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("historical task blob must be UTF-8") from exc
    historical = parse_manager_child_metadata(historical_text, args.root)
    if historical is None or historical.runat != args.historical_target:
        raise TaskFrontmatterError("historical Git blob does not prove the supplied target for this task")
    lines = current_bytes.splitlines(keepends=True)
    closing = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == b"---"), None)
    if closing is None:
        raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
    changed = 0
    for idx in range(1, closing):
        key, separator, value = lines[idx].partition(b":")
        if separator and key.strip() == b"runat":
            if value.strip() != b"retired":
                raise TaskFrontmatterError("terminal target restoration source target drifted")
            newline = lines[idx][len(lines[idx].rstrip(b"\r\n")) :]
            lines[idx] = f"runat: {args.historical_target}".encode() + newline
            changed += 1
    if changed != 1:
        raise TaskFrontmatterError(f"expected exactly one frontmatter `runat`, found {changed}.")
    updated_bytes = b"".join(lines)
    if historical_bytes != updated_bytes:
        raise TaskFrontmatterError("historical Git blob must equal current task bytes except for the sole restored run target")
    updated = updated_bytes.decode("utf-8")
    restored = parse_task_metadata(updated, args.root)
    if restored is None or restored.status != "done" or restored.runat != args.historical_target or restored.pending_task_items:
        raise TaskFrontmatterError("restored terminal task metadata did not validate")
    replace_bytes_if_unchanged(path, updated_bytes, before)


def close_retired_done(args: Args, path: Path, text: str, before: os.stat_result) -> str:
    """Finish one proven already-closed retired worker without inspecting tmux."""
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != args.source_sha256:
        raise TaskFrontmatterError("retired closure source bytes do not match --source-sha256")
    metadata = parse_manager_child_metadata(text, args.root)
    blocked_source = (
        metadata is not None
        and metadata.version != V2_VERSION
        and metadata.status == "blocked"
        and metadata.runat == "retired"
        and not metadata.is_manager
        and not metadata.pending_task_items
    )
    recoverable_intermediate = (
        metadata is not None
        and metadata.version != V2_VERSION
        and metadata.status == "done"
        and metadata.runat == args.historical_target
        and not metadata.is_manager
        and not metadata.pending_task_items
    )
    if not blocked_source and not recoverable_intermediate:
        raise TaskFrontmatterError("retired closure requires one unchanged blocked/retired worker or its exact done-task intermediate state")
    relative = relative_task_ref(args.root, path)
    try:
        repository_root = Path(subprocess.run(["git", "-C", str(args.root), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()).resolve(strict=True)
        if repository_root != args.root.resolve(strict=True):
            raise TaskFrontmatterError("task root must be the exact Git worktree containing historical proof")
        proven_commit = subprocess.run(["git", "-C", str(args.root), "rev-parse", f"{args.historical_commit}^{{commit}}"], check=True, capture_output=True, text=True).stdout.strip()
        if proven_commit != args.historical_commit:
            raise TaskFrontmatterError("historical commit must be the full canonical commit id")
        historical_bytes = subprocess.run(["git", "-C", str(args.root), "show", f"{proven_commit}:{relative}"], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise TaskFrontmatterError("cannot verify historical target from the supplied Git commit and task path") from exc
    historical = parse_manager_child_metadata(historical_bytes.decode("utf-8"), args.root)
    if historical is None or historical.runat != args.historical_target:
        raise TaskFrontmatterError("historical Git blob does not prove the supplied target for this task")
    close_pattern = re.compile(rf"manager closed Codex agent[^\n]*tmux target [`']{re.escape(args.historical_target)}[`']")
    if close_pattern.search(text) is None:
        raise TaskFrontmatterError("retired closure requires a recorded manager-close note for the proven historical target")

    updated_bytes = source_bytes
    if blocked_source:
        lines = source_bytes.splitlines(keepends=True)
        closing = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == b"---"), None)
        if closing is None:
            raise TaskFrontmatterError("task frontmatter opening marker has no closing marker.")
        updated_lines: list[bytes] = []
        changed_status = 0
        changed_runat = 0
        removed_blocker = 0
        for idx, line in enumerate(lines):
            if 0 < idx < closing:
                key, separator, value = line.partition(b":")
                normalized = key.strip()
                newline = line[len(line.rstrip(b"\r\n")) :]
                if separator and normalized == b"status":
                    if value.strip() != b"blocked":
                        raise TaskFrontmatterError("retired closure source status drifted")
                    updated_lines.append(b"status: done" + newline)
                    changed_status += 1
                    continue
                if separator and normalized == b"runat":
                    if value.strip() != b"retired":
                        raise TaskFrontmatterError("retired closure source target drifted")
                    updated_lines.append(f"runat: {args.historical_target}".encode() + newline)
                    changed_runat += 1
                    continue
                if separator and normalized == b"blocked_on":
                    removed_blocker += 1
                    continue
            updated_lines.append(line)
        if (changed_status, changed_runat, removed_blocker) != (1, 1, 1):
            raise TaskFrontmatterError("retired closure requires exactly one status, runat, and blocked_on field")
        updated_bytes = b"".join(updated_lines)
    updated_text = updated_bytes.decode("utf-8")
    updated_metadata = parse_task_metadata(updated_text, args.root)
    if updated_metadata is None or updated_metadata.status != "done" or updated_metadata.runat != args.historical_target or updated_metadata.pending_task_items:
        raise TaskFrontmatterError("retired closure output did not validate")

    todo = args.root / "TODO.md"
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        if not same_file_state(before, current_before) or path.read_bytes() != source_bytes:
            raise TaskFrontmatterError("retired task changed while closure was being prepared; retry")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        rows = [(idx, line) for idx, line in enumerate(todo_text.splitlines(keepends=True)) if path in todo_row_task_paths(args.root, line)]
        canonical_retired_row = re.compile(rf"^\s*{re.escape(relative)}\s+retired\s*$")
        if len(rows) != 1 or TARGET_RE.findall(rows[0][1]) != [] or canonical_retired_row.fullmatch(rows[0][1].rstrip("\r\n")) is None:
            raise TaskFrontmatterError("retired closure requires exactly one targetless retired TODO row")
        todo_lines = todo_text.splitlines(keepends=True)
        row_idx, row = rows[0]
        todo_lines[row_idx] = re.sub(r"retired(?P<trailing>\s*)$", rf"{args.historical_target}\g<trailing>", row)
        normalized_todo = "".join(todo_lines)
        updated_todo = reconcile_todo_text(args.root, path, normalized_todo, args.historical_target, "previous", ("human pending",))
        updated_task_before = current_before
        if blocked_source:
            replace_if_unchanged_locked(path, updated_text, current_before)
            updated_task_before = path.stat()
        try:
            replace_if_unchanged_locked(todo, updated_todo, todo_before)
        except Exception as exc:
            if blocked_source:
                try:
                    replace_if_unchanged_locked(path, text, updated_task_before)
                except Exception as rollback_exc:
                    raise TaskFrontmatterError(f"retired closure failed and task rollback also failed: {rollback_exc}") from exc
                raise TaskFrontmatterError(f"retired closure failed and task was rolled back: {exc}") from exc
            raise TaskFrontmatterError(f"retired closure recovery could not finish TODO: {exc}") from exc
    return args.historical_target


def normalize_retired_todo(args: Args, path: Path, text: str, before: os.stat_result) -> None:
    """Add the required `retired` marker to one exact already-retired TODO row.

    This is deliberately an index-only preflight for ``--close-retired-done``.
    It never restores a target, changes frontmatter, or accesses tmux.
    """
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != args.source_sha256:
        raise TaskFrontmatterError("retired TODO normalization source bytes do not match --source-sha256")
    metadata = parse_task_metadata(text, args.root)
    if (
        metadata is None
        or metadata.version == V2_VERSION
        or metadata.status != "blocked"
        or metadata.runat != "retired"
        or metadata.is_manager
        or metadata.pending_task_items
        or PENDING_MARKER in text
    ):
        raise TaskFrontmatterError("retired TODO normalization requires one unchanged v1 blocked/retired non-manager worker with an empty queue and no live pending marker")
    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    relative = relative_task_ref(args.root, path)
    canonical_targetless_row = re.compile(rf"^\s*{re.escape(relative)}\s*$")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_bytes = path.read_bytes()
        if not same_file_state(before, current_before) or current_bytes != source_bytes:
            raise TaskFrontmatterError("retired task changed while TODO normalization was being prepared; retry")
        current_metadata = parse_task_metadata(current_bytes.decode("utf-8"), args.root)
        if (
            current_metadata is None
            or current_metadata.version == V2_VERSION
            or current_metadata.status != "blocked"
            or current_metadata.runat != "retired"
            or current_metadata.is_manager
            or current_metadata.pending_task_items
            or PENDING_MARKER in current_bytes.decode("utf-8")
        ):
            raise TaskFrontmatterError("retired task changed while TODO normalization was being prepared; retry")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        rows: list[tuple[int, str]] = []
        section = ""
        lines = todo_text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            heading = line.strip()
            if heading.endswith(":"):
                section = heading[:-1].casefold()
                continue
            if path in todo_row_task_paths(args.root, line):
                rows.append((index, section))
        if len(rows) != 1 or rows[0][1] != "human pending":
            raise TaskFrontmatterError("retired TODO normalization requires exactly one human-pending TODO row")
        row_index, _ = rows[0]
        row = lines[row_index]
        if TARGET_RE.findall(row) or canonical_targetless_row.fullmatch(row.rstrip("\r\n")) is None:
            raise TaskFrontmatterError("retired TODO normalization requires one exact targetless task row")
        line_ending = row[len(row.rstrip("\r\n")) :]
        lines[row_index] = f"{row.rstrip(chr(13) + chr(10))} retired{line_ending}"
        replace_if_unchanged_locked(todo, "".join(lines), todo_before)


def normalize_low_priority_current(args: Args, path: Path, text: str, before: os.stat_result) -> None:
    """Promote one exact active manager TODO row from low priority to current."""
    if (
        TARGET_RE.fullmatch(args.active_target) is None
        or TARGET_RE.fullmatch(args.manager_target) is None
        or args.active_target.partition(":")[0].startswith("h")
        or args.manager_target.partition(":")[0].startswith("h")
    ):
        raise TaskFrontmatterError("low-priority normalization requires non-human exact active and manager targets")
    source_bytes = path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != args.source_sha256:
        raise TaskFrontmatterError("low-priority normalization source bytes do not match --source-sha256")
    metadata = parse_task_metadata(text, args.root)
    if (
        metadata is None
        or metadata.version == V2_VERSION
        or metadata.status not in {"running", "long_running"}
        or not metadata.is_manager
        or metadata.runat != args.active_target
        or metadata.managerat != args.manager_target
        or has_pending_marker(text)
    ):
        raise TaskFrontmatterError("low-priority normalization requires one unchanged active v1 manager with exact runat and manager ownership and no live pending marker")
    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with root_membership_lock(args.root), task_target_lock(args.root, args.active_target):
        with ExitStack() as locks:
            task_files = {candidate.resolve(strict=False) for candidate in args.root.rglob("*.md")}
            task_files.update((path, todo))
            for locked_path in sorted(task_files, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_bytes = path.read_bytes()
            if not same_file_state(before, current_before) or current_bytes != source_bytes:
                raise TaskFrontmatterError("active manager task changed while low-priority normalization was being prepared; retry")
            current_text = current_bytes.decode("utf-8")
            current = parse_task_metadata(current_text, args.root)
            if (
                current is None
                or current.version == V2_VERSION
                or current.status not in {"running", "long_running"}
                or not current.is_manager
                or current.runat != args.active_target
                or current.managerat != args.manager_target
                or has_pending_marker(current_text)
            ):
                raise TaskFrontmatterError("active manager task ownership or lifecycle changed while low-priority normalization was being prepared; retry")
            owners = authoritative_active_target_task_paths(args.root, args.active_target)
            if owners != (path,):
                refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
                raise TaskFrontmatterError(f"low-priority normalization requires the task to be the sole active owner of `{args.active_target}`: {refs}.")
            todo_before = todo.stat()
            todo_text = todo.read_text(encoding="utf-8")
            lines = todo_text.splitlines(keepends=True)
            section = ""
            headers = {"current": 0, "low priority": 0, "human pending": 0, "previous": 0}
            rows: list[tuple[int, str]] = []
            for index, line in enumerate(lines):
                heading = line.strip()
                if heading.endswith(":"):
                    normalized = heading[:-1].casefold()
                    if normalized in headers:
                        if heading != f"{normalized}:":
                            raise TaskFrontmatterError("low-priority normalization requires canonical lowercase TODO section headers")
                        headers[normalized] += 1
                    section = normalized
                    continue
                if path in todo_row_task_paths(args.root, line):
                    rows.append((index, section))
            if any(count != 1 for count in headers.values()):
                raise TaskFrontmatterError("low-priority normalization requires exactly one canonical current, low priority, human pending, and previous TODO section")
            if len(rows) != 1 or rows[0][1] != "low priority":
                raise TaskFrontmatterError("low-priority normalization requires exactly one TODO row in low priority")
            row_index, _ = rows[0]
            row = lines[row_index]
            match = TODO_ROW_RE.fullmatch(row.rstrip("\r\n"))
            canonical_row = f"{relative_task_ref(args.root, path)} {args.active_target}"
            if match is None or row.rstrip("\r\n") != canonical_row:
                raise TaskFrontmatterError("low-priority normalization requires the sole canonical TODO row to name the exact active target")
            updated_todo = reconcile_todo_text(args.root, path, todo_text, args.active_target, "current", ("low priority",))
            if updated_todo == todo_text:
                raise TaskFrontmatterError("low-priority normalization requires the sole TODO row to move from low priority to current")
            if todo.read_text(encoding="utf-8") != todo_text or not same_file_state(todo_before, todo.stat()):
                raise TaskFrontmatterError("TODO changed while low-priority normalization was being prepared; retry")
            replace_if_unchanged_locked(todo, updated_todo, todo_before)


def reconcile_todo_text(root: Path, path: Path, text: str, runat: str, destination: str, allowed_sections: tuple[str, ...]) -> str:
    """Move one validated TODO row between allowed lifecycle sections."""
    lines = text.splitlines(keepends=True)
    section = ""
    rows: list[tuple[int, str]] = []
    destination_headers: list[int] = []
    for idx, line in enumerate(lines):
        name = line.strip()
        if name.endswith(":"):
            section = name[:-1].casefold()
            if section == destination:
                destination_headers.append(idx)
            continue
        if path in todo_row_task_paths(root, line):
            rows.append((idx, section))
    if len(rows) != 1:
        raise TaskFrontmatterError(f"expected exactly one TODO row for `{relative_task_ref(root, path)}`, found {len(rows)}.")
    if len(destination_headers) != 1:
        raise TaskFrontmatterError(f"expected exactly one `{destination}:` section while reconciling `{relative_task_ref(root, path)}`.")
    source_idx, source_section = rows[0]
    validate_reconciled_todo_row(root, path, lines[source_idx], runat)
    if source_section not in allowed_sections:
        expected = " or ".join(f"`{candidate}:`" for candidate in allowed_sections)
        raise TaskFrontmatterError(f"expected `{relative_task_ref(root, path)}` to be in {expected}, found `{source_section}:`.")
    if source_section == destination:
        return text
    moved = lines.pop(source_idx)
    destination_idx = next(idx for idx, line in enumerate(lines) if line.strip().casefold() == f"{destination}:")
    insert_at = destination_idx + 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    if not moved.endswith(("\n", "\r")) and insert_at < len(lines):
        raise TaskFrontmatterError(f"cannot move unterminated TODO row for `{relative_task_ref(root, path)}`.")
    lines.insert(insert_at, moved)
    return "".join(lines)


def reconcile_running_todo_text(root: Path, path: Path, text: str, runat: str) -> str:
    """Move the sole TODO row for a running task into `current`, or fail closed."""
    return reconcile_todo_text(root, path, text, runat, "current", ("current", "human pending", "previous"))


def reconcile_blocked_todo_text(root: Path, path: Path, text: str, runat: str) -> str:
    """Move the sole TODO row for a blocked task into `human pending`, or fail closed."""
    return reconcile_todo_text(root, path, text, runat, "human pending", ("current", "human pending"))


def reconcile_done_todo_text(root: Path, path: Path, text: str, runat: str) -> str:
    """Move the sole TODO row for a done task into `previous`, or fail closed."""
    return reconcile_todo_text(root, path, text, runat, "previous", ("current", "human pending", "low priority"))


def reconcile_shared_done_todo_text(root: Path, path: Path, text: str, shared_target: str) -> str:
    """Move one exact current TODO row for a shared-target manager into `previous`."""
    lines = text.splitlines(keepends=True)
    section = ""
    rows: list[str] = []
    current_headers = 0
    previous_headers = 0
    invalid_lifecycle_headers = 0
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
            if stripped == "current:":
                current_headers += 1
            elif stripped == "previous:":
                previous_headers += 1
            elif section in {"current", "previous"}:
                invalid_lifecycle_headers += 1
        elif path in todo_row_task_paths(root, line):
            if section == "current":
                rows.append(line)
            else:
                rows.append(f"__wrong_section__:{section}")
    if len(rows) != 1 or rows[0].startswith("__wrong_section__:"):
        found = len(rows)
        raise TaskFrontmatterError(f"shared-target closure requires exactly one TODO row in `current`, found {found}.")
    if current_headers != 1 or previous_headers != 1 or invalid_lifecycle_headers:
        raise TaskFrontmatterError("shared-target closure requires exactly one canonical lowercase `current:` and `previous:` TODO section.")
    match = TODO_ROW_RE.fullmatch(rows[0].rstrip("\r\n"))
    if match is None or TARGET_RE.findall(match.group(2) or "") != [shared_target]:
        raise TaskFrontmatterError("shared-target closure requires the sole current TODO row to name the exact shared target.")
    return reconcile_todo_text(root, path, text, shared_target, "previous", ("current",))


def finish_shared_target_done(args: Args, path: Path, text: str, before: os.stat_result) -> str:
    """Close a manager record that shares an explicitly supplied target, without tmux."""
    if hashlib.sha256(path.read_bytes()).hexdigest() != args.source_sha256:
        raise TaskFrontmatterError("shared-target closure source bytes do not match --source-sha256.")
    metadata = parse_task_metadata(text, args.root)
    if metadata is None or metadata.version == V2_VERSION or metadata.status != "long_running" or not metadata.is_manager:
        raise TaskFrontmatterError("shared-target closure requires one unchanged v1 long_running manager record.")
    if metadata.runat != args.shared_target:
        raise TaskFrontmatterError("shared-target closure target does not equal the authoritative task `runat`.")
    if same_tmux_target(metadata.managerat, args.shared_target):
        raise TaskFrontmatterError("shared-target closure requires a distinct manager owner target.")
    if metadata.pending_task_items or has_pending_marker(text):
        raise TaskFrontmatterError("shared-target closure requires an empty queue and no live pending marker.")
    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with root_membership_lock(args.root), task_target_lock(args.root, args.shared_target):
        # Membership is serialized by the root lock. Acquire the ordinary task
        # and TODO locks in the repository-wide sorted order used by status
        # reconciliation, avoiding a TODO-first inversion.
        with ExitStack() as locks:
            task_files = {candidate.resolve(strict=False) for candidate in args.root.rglob("*.md")}
            task_files.add(path)
            task_files.add(todo)
            for locked_path in sorted(task_files, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            if not same_file_state(before, current_before) or current_text != text:
                raise TaskFrontmatterError("shared-target task changed while closure was being prepared; retry.")
            current_metadata = parse_task_metadata(current_text, args.root)
            if current_metadata is None or current_metadata.status != "long_running" or current_metadata.runat != args.shared_target or not current_metadata.is_manager:
                raise TaskFrontmatterError("shared-target task ownership or lifecycle changed while closure was being prepared; retry.")
            ensure_manager_has_no_active_children(args.root, path, current_metadata)
            owners = authoritative_active_target_task_paths(args.root, args.shared_target)
            if owners != (path,):
                refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
                raise TaskFrontmatterError(f"shared-target closure requires the task to be the sole active owner of `{args.shared_target}`: {refs}.")
            todo_text = todo.read_text(encoding="utf-8")
            todo_before = todo.stat()
            updated_todo = reconcile_shared_done_todo_text(args.root, path, todo_text, args.shared_target)
            updated_task = update_frontmatter_status(current_text, "done", "", args.root)
            if updated_todo == todo_text:
                raise TaskFrontmatterError("shared-target closure requires the sole TODO row to move from current to previous.")
            # The strict preflight proves the row invariant; the existing
            # done transaction provides the canonical replacement/rollback
            # protocol while the target and file locks remain held.
            finish_done_transaction(args.root, path, updated_task, current_before, locked=True, prepared_todo=updated_todo, todo_text=todo_text, todo_before=todo_before)
    return args.shared_target


def reconcile_running_index(root: Path, path: Path, text: str, before: os.stat_result) -> None:
    """Move an already-running task's sole inactive TODO row into `current`."""
    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        current_metadata = parse_task_metadata(current_text, root)
        if not same_file_state(before, current_before) or current_text != text or current_metadata is None or current_metadata.status != "running":
            raise TaskFrontmatterError("task changed while running index reconciliation was being prepared; retry after rereading it.")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        updated_todo = reconcile_running_todo_text(root, path, todo_text, current_metadata.runat)
        if updated_todo != todo_text:
            replace_if_unchanged_locked(todo, updated_todo, todo_before)


def transition_running_index(root: Path, path: Path, text: str, updated: str, before: os.stat_result) -> None:
    """Move one inactive TODO row before committing the task's `running` status."""

    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    metadata = parse_task_metadata(text, root)
    updated_metadata = parse_task_metadata(updated, root)
    if metadata is None or updated_metadata is None or metadata.status == "running" or updated_metadata.status != "running" or metadata.runat != updated_metadata.runat:
        raise TaskFrontmatterError("running transition requires one unchanged non-running task and run target.")
    with task_target_lock(root, metadata.runat):
        with ExitStack() as locks:
            for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            if not same_file_state(before, current_before) or current_text != text:
                raise TaskFrontmatterError("task changed while running transition was being prepared; retry after rereading it.")
            todo_before = todo.stat()
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = reconcile_running_todo_text(root, path, todo_text, metadata.runat)
            if updated_todo == todo_text:
                replace_if_unchanged_locked(path, updated, current_before)
                return
            replace_if_unchanged_locked(todo, updated_todo, todo_before)
            moved_todo_before = todo.stat()
            try:
                replace_if_unchanged_locked(path, updated, current_before)
            except Exception as exc:
                try:
                    replace_if_unchanged_locked(todo, todo_text, moved_todo_before)
                except Exception as rollback_exc:
                    raise TaskFrontmatterError(f"running task update failed and TODO rollback also failed: {rollback_exc}") from exc
                raise


def reconcile_blocked_index(root: Path, path: Path, text: str, updated: str, before: os.stat_result) -> None:
    """Update an unchanged blocked task and move its TODO row into `human pending`."""
    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        current_metadata = parse_task_metadata(current_text, root)
        updated_metadata = parse_task_metadata(updated, root)
        if (
            not same_file_state(before, current_before)
            or current_text != text
            or current_metadata is None
            or current_metadata.status != "blocked"
            or updated_metadata is None
            or updated_metadata.status != "blocked"
            or current_metadata.runat != updated_metadata.runat
        ):
            raise TaskFrontmatterError("task changed or no longer matches the blocked status reconciliation; retry after rereading it.")
        if path == todo:
            combined = reconcile_blocked_todo_text(root, path, updated, current_metadata.runat)
            if combined != current_text:
                replace_if_unchanged_locked(path, combined, current_before)
            return
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        updated_todo = reconcile_blocked_todo_text(root, path, todo_text, current_metadata.runat)
        if updated_todo == todo_text:
            if updated != current_text:
                replace_if_unchanged_locked(path, updated, current_before)
            return
        replace_if_unchanged_locked(todo, updated_todo, todo_before)
        if updated == current_text:
            return
        moved_todo_before = todo.stat()
        try:
            replace_if_unchanged_locked(path, updated, current_before)
        except Exception as exc:
            try:
                replace_if_unchanged_locked(todo, todo_text, moved_todo_before)
            except Exception as rollback_exc:
                raise TaskFrontmatterError(f"blocked task update failed and TODO rollback also failed: {rollback_exc}") from exc
            raise


def reconcile_previous_blocked_index(args: Args, path: Path, text: str, before: os.stat_result) -> None:
    """Move one unchanged blocked worker with open work from `previous` to `human pending`."""

    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    if path == todo:
        raise TaskFrontmatterError("blocked index reconciliation requires a task file distinct from TODO.md.")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        metadata = parse_task_metadata(current_text, args.root)
        if not same_file_state(before, current_before) or current_text != text or hashlib.sha256(current_text.encode()).hexdigest() != args.source_sha256:
            raise TaskFrontmatterError("blocked index source bytes changed or do not match --source-sha256.")
        if metadata is None or metadata.version == V2_VERSION or metadata.status != "blocked" or metadata.is_manager:
            raise TaskFrontmatterError("blocked index reconciliation requires one unchanged v1 blocked worker.")
        if not metadata.pending_task_items or has_pending_marker(current_text):
            raise TaskFrontmatterError("blocked index reconciliation requires a nonempty queue and no live pending marker.")
        if metadata.blocked_on.startswith((DONE_CLOSE_IN_PROGRESS, CLOSE_FAILED_PREFIX, BOOKKEEPING_FAILED_PREFIX)):
            raise TaskFrontmatterError("blocked index reconciliation rejects incomplete or failed closure state.")
        if TARGET_RE.fullmatch(metadata.runat) is None or metadata.runat.partition(":")[0].startswith("h"):
            raise TaskFrontmatterError("blocked index reconciliation requires a non-human live worker target.")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        headers = {"current": 0, "human pending": 0, "previous": 0}
        invalid_headers = 0
        for line in todo_text.splitlines():
            stripped = line.strip()
            folded = stripped.casefold()
            if folded.endswith(":") and folded[:-1] in headers:
                section = folded[:-1]
                headers[section] += 1
                invalid_headers += stripped != f"{section}:"
        if headers != {"current": 1, "human pending": 1, "previous": 1} or invalid_headers:
            raise TaskFrontmatterError("blocked index reconciliation requires exactly one canonical current, human pending, and previous TODO section.")
        updated_todo = reconcile_todo_text(args.root, path, todo_text, metadata.runat, "human pending", ("previous",))
        if updated_todo == todo_text:
            raise TaskFrontmatterError("blocked index reconciliation requires the sole TODO row to move from previous to human pending.")
        replace_if_unchanged_locked(todo, updated_todo, todo_before)


def reconcile_long_running_human_index(root: Path, path: Path, text: str, before: os.stat_result) -> None:
    """Move an unchanged long_running human-blocked task from current to human pending."""

    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        metadata = parse_task_metadata(current_text, root)
        if (
            not same_file_state(before, current_before)
            or current_text != text
            or metadata is None
            or metadata.version == V2_VERSION
            or metadata.status != "long_running"
            or metadata.blocked_on != "human"
        ):
            raise TaskFrontmatterError("index reconciliation requires one unchanged v1 long_running task blocked exactly on human.")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        updated_todo = reconcile_todo_text(root, path, todo_text, metadata.runat, "human pending", ("current",))
        if updated_todo != todo_text:
            replace_if_unchanged_locked(todo, updated_todo, todo_before)


def reconcile_done_index(root: Path, path: Path, text: str, before: os.stat_result) -> None:
    """Move an unchanged, already-done task's sole stale TODO row into `previous`."""
    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    metadata = parse_task_metadata(text, root)
    if metadata is None or metadata.status != "done":
        raise TaskFrontmatterError("done index reconciliation requires an already-done task.")
    with task_target_lock(root, metadata.runat):
        with ExitStack() as locks:
            for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            current_metadata = parse_task_metadata(current_text, root)
            if (
                not same_file_state(before, current_before)
                or current_text != text
                or current_metadata is None
                or current_metadata.status != "done"
                or update_frontmatter_status(current_text, "done", "", root) != current_text
            ):
                raise TaskFrontmatterError("task changed or no longer matches done index reconciliation; retry after rereading it.")
            owners = current_target_task_paths(root, current_metadata.runat)
            if owners:
                refs = ", ".join(relative_task_ref(root, owner) for owner in owners)
                raise TaskFrontmatterError(f"done task target still has active current ownership: {refs}.")
            if exact_pane_id(current_metadata.runat):
                raise TaskFrontmatterError("done task pane is still active; use normal lifecycle recovery instead of index-only reconciliation.")
            todo_before = todo.stat()
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = reconcile_done_todo_text(root, path, todo_text, current_metadata.runat)
            if exact_pane_id(current_metadata.runat):
                raise TaskFrontmatterError("done task pane became active while index reconciliation was being prepared; retry.")
            if updated_todo != todo_text:
                replace_if_unchanged_locked(todo, updated_todo, todo_before)


def done_close_failed_reason(exc: Exception) -> str:
    reason = " ".join(str(exc).split())
    return f"{CLOSE_FAILED_PREFIX}: {reason or exc.__class__.__name__}"


def mark_done_bookkeeping_failed(root: Path, path: Path, exc: Exception) -> None:
    rollback_before = path.stat()
    rollback_text = path.read_text(encoding="utf-8")
    rollback = update_frontmatter_status(rollback_text, "blocked", done_bookkeeping_failed_reason(exc), root)
    replace_if_unchanged(path, rollback, rollback_before)


def finish_done_transaction(root: Path, path: Path, text: str, before: os.stat_result, *, locked: bool = False, todo_text: str | None = None, prepared_todo: str | None = None, todo_before: os.stat_result | None = None) -> None:
    """Atomically replace each bookkeeping file and roll back `TODO.md` if the task replacement fails."""
    replace_file = replace_if_unchanged_locked if locked else replace_if_unchanged
    todo = root / "TODO.md"
    if not todo.exists():
        replace_file(path, text, before)
        return
    current_todo_before = todo.stat()
    current_todo_text = todo.read_text(encoding="utf-8")
    if todo_text is not None and current_todo_text != todo_text:
        raise TaskFrontmatterError("TODO changed after strict shared-target validation; retry.")
    if todo_before is not None and not same_file_state(todo_before, current_todo_before):
        raise TaskFrontmatterError("TODO changed after strict shared-target validation; retry.")
    original_todo_text = current_todo_text if todo_text is None else todo_text
    task_file = path.relative_to(root).as_posix()
    updated_todo = prepared_todo if prepared_todo is not None else moved_todo_text(root, task_file, original_todo_text)
    if updated_todo == original_todo_text:
        replace_file(path, text, before)
        return
    replace_file(todo, updated_todo, current_todo_before)
    moved_todo_state = todo.stat()
    try:
        replace_file(path, text, before)
    except Exception as exc:
        try:
            replace_file(todo, original_todo_text, moved_todo_state)
        except Exception as rollback_exc:
            raise TaskFrontmatterError(f"task update failed and TODO rollback also failed: {rollback_exc}") from exc
        raise


def reserve_private_audit(path: Path, text: str) -> None:
    """Create one exclusive owner-private audit record before lifecycle mutation."""

    parent = path.parent
    try:
        parent_state = parent.stat()
    except OSError as exc:
        raise TaskFrontmatterError(f"audit output directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent_state.st_mode) or parent_state.st_uid != os.getuid() or stat.S_IMODE(parent_state.st_mode) & 0o077:
        raise TaskFrontmatterError("audit output directory must be owner-private.")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise TaskFrontmatterError(f"cannot reserve private audit output: {exc}") from exc


def finish_private_audit(path: Path, prepared: str, result: str) -> None:
    """Finalize only the exact audit file reserved by this operation."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    temporary: Path | None = None
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as output:
            current = os.fstat(output.fileno())
            if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600:
                raise TaskFrontmatterError("reserved audit output lost its owner-private file binding.")
            if output.read() != prepared:
                raise TaskFrontmatterError("reserved audit output changed before lifecycle completion.")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(prepared + f"final-result: {result}\n")
            output.flush()
            os.fsync(output.fileno())
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise TaskFrontmatterError("reserved audit output changed before atomic finalization.")
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise TaskFrontmatterError(f"cannot finalize private audit output: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replacement_task_text(
    args: Args,
    stale_path: Path,
    stale_text: str,
    stale_before: os.stat_result,
) -> tuple[str, TaskMetadata, TaskMetadata, str, os.stat_result, str]:
    replacement_path = args.replacement_task
    if replacement_path is None or replacement_path == stale_path:
        raise TaskFrontmatterError("replacement task must be a distinct explicit file.")
    if args.root not in replacement_path.parents:
        raise TaskFrontmatterError("replacement task must be under the work-log root.")
    if not replacement_path.is_file():
        raise TaskFrontmatterError(f"replacement task file not found: {replacement_path}")
    stale = parse_task_metadata(stale_text, args.root)
    if stale is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    _ = update_frontmatter_status(stale_text, "done", "", args.root)
    if stale.status != "blocked" or stale.pending_task_items:
        raise TaskFrontmatterError("--finish-replaced-done requires one blocked stale task with an empty pending queue.")
    if TARGET_RE.fullmatch(args.stale_target) is None or TARGET_RE.fullmatch(args.replacement_target) is None:
        raise TaskFrontmatterError("stale and replacement targets must be exact SESSION:WINDOW[.PANE] identities.")
    if stale.runat != args.stale_target:
        raise TaskFrontmatterError("stale task `runat` does not equal --stale-target.")
    if SHA256_RE.fullmatch(args.stale_sha256) is None or hashlib.sha256(stale_text.encode()).hexdigest() != args.stale_sha256:
        raise TaskFrontmatterError("stale task bytes do not match --stale-sha256.")
    ensure_manager_has_no_active_children(args.root, stale_path, stale)
    verified_empty_line = f"(verified empty stale task: {args.stopped_evidence})"
    if verified_empty_line not in stale_text.splitlines():
        raise TaskFrontmatterError("empty stale task requires an exact verified empty-stale-task record.")
    if args.stale_target.partition(":")[0].startswith("h") or args.replacement_target.partition(":")[0].startswith("h"):
        raise TaskFrontmatterError("replacement closure cannot inspect or modify a human-owned `h*` tmux session.")
    if any(same_tmux_target(target, protected) for target in (args.stale_target, args.replacement_target) for protected in args.protected_targets):
        raise TaskFrontmatterError("stale or replacement target is in the explicit protected-target set.")
    if exact_pane_id(stale.runat):
        raise TaskFrontmatterError("stale target is still live; replacement closure requires a stopped legacy target.")
    replacement_before = replacement_path.stat()
    replacement_text = replacement_path.read_text(encoding="utf-8")
    replacement = parse_task_metadata(replacement_text, args.root)
    if replacement is None:
        raise TaskFrontmatterError("replacement task file has no frontmatter.")
    if replacement.status != args.replacement_status or replacement.status not in {"running", "long_running"} or not replacement.pending_task_items:
        raise TaskFrontmatterError("replacement task status or pending queue does not match the explicit active successor precondition.")
    if replacement.runat != args.replacement_target:
        raise TaskFrontmatterError("replacement task `runat` does not equal --replacement-target.")
    if SHA256_RE.fullmatch(args.replacement_sha256) is None or hashlib.sha256(replacement_text.encode()).hexdigest() != args.replacement_sha256:
        raise TaskFrontmatterError("replacement task bytes do not match --replacement-sha256.")
    if not task_is_in_todo_section(args.root, replacement_path, "current"):
        raise TaskFrontmatterError("replacement task must be listed in the current TODO section.")
    if same_tmux_target(stale.runat, replacement.runat):
        raise TaskFrontmatterError("replacement task must use a different target from the stopped stale task.")
    if (stale.managerat, stale.tool, stale.is_manager) != (replacement.managerat, replacement.tool, replacement.is_manager):
        raise TaskFrontmatterError("replacement task ownership or role does not match the stale task.")
    owners = authoritative_active_target_task_paths(args.root, replacement.runat)
    if owners != (replacement_path,):
        refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
        raise TaskFrontmatterError(f"replacement task is not the sole authoritative active owner of `{replacement.runat}`: {refs}.")
    pane_id = exact_pane_id(replacement.runat)
    if not pane_id:
        raise TaskFrontmatterError("replacement target is not an exact live pane target.")
    pane_text = capture(pane_id, 2000)
    if args.replacement_pane_evidence not in pane_text:
        raise TaskFrontmatterError("replacement pane evidence is missing from the live reused pane.")
    if exact_pane_id(replacement.runat) != pane_id:
        raise TaskFrontmatterError("replacement pane changed while evidence was checked; retry.")
    if exact_pane_id(stale.runat):
        raise TaskFrontmatterError("stale target became live while replacement evidence was checked; retry.")
    if not same_file_state(stale_before, stale_path.stat()) or stale_path.read_text(encoding="utf-8") != stale_text:
        raise TaskFrontmatterError("stale task changed while replacement evidence was being checked; retry.")
    if not same_file_state(replacement_before, replacement_path.stat()) or replacement_path.read_text(encoding="utf-8") != replacement_text:
        raise TaskFrontmatterError("replacement task changed while evidence was being checked; retry.")
    return update_frontmatter_status(stale_text, "done", "", args.root), stale, replacement, replacement_text, replacement_before, pane_id


def finish_closed_done(args: Args, path: Path, text: str, before: os.stat_result) -> tuple[str, str]:
    metadata = parse_task_metadata(text, args.root)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    ensure_manager_has_no_active_children(args.root, path, metadata)
    matching_close_note = has_close_note(text, metadata.runat, args.session_id)
    verified_already_closed = (
        not metadata.is_manager
        and is_close_failed_reason(metadata.blocked_on)
        and not exact_pane_id(metadata.runat)
        and task_is_in_todo_section(args.root, path, "previous")
    )
    close_session_id = "" if verified_already_closed else args.session_id
    retryable_blocker = is_bookkeeping_failed_reason(metadata.blocked_on) or (
        matching_close_note and (metadata.blocked_on == DONE_CLOSE_IN_PROGRESS or is_close_failed_reason(metadata.blocked_on))
    ) or verified_already_closed
    if metadata.status != "blocked" or not retryable_blocker:
        raise TaskFrontmatterError("--finish-closed-done requires failed close bookkeeping or a matching prior-close note on a failed close.")
    bookkept = text if matching_close_note else text.rstrip("\n") + close_note(metadata.runat, close_session_id)
    updated = update_frontmatter_status(bookkept, "done", "", args.root)
    try:
        finish_done_transaction(args.root, path, updated, before)
    except Exception as exc:
        mark_done_bookkeeping_failed(args.root, path, exc)
        raise TaskFrontmatterError(f"done close bookkeeping retry failed; task marked blocked for retry: {exc}") from exc
    return metadata.runat, close_session_id


def recover_exited_shell_done(args: Args, path: Path, text: str, before: os.stat_result) -> tuple[str, str]:
    """Close one proven exited worker shell and finish its done bookkeeping."""

    todo = args.root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    initial_metadata = parse_task_metadata(text, args.root)
    if initial_metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    with task_target_lock(args.root, initial_metadata.runat):
        with ExitStack() as locks:
            for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
                locks.enter_context(task_file_lock(locked_path))
            current_before = path.stat()
            current_text = path.read_text(encoding="utf-8")
            metadata = parse_task_metadata(current_text, args.root)
            if not same_file_state(before, current_before) or current_text != text or metadata is None:
                raise TaskFrontmatterError("task changed while exited-shell recovery was being prepared; retry after rereading it.")
            if metadata.is_manager:
                raise TaskFrontmatterError("--recover-exited-shell-done supports non-manager tasks only.")
            if metadata.status != "blocked" or metadata.blocked_on != (
                f"{CLOSE_FAILED_PREFIX}: target is not a supported live Codex pane: {args.pane_id} status=not_codex"
            ):
                raise TaskFrontmatterError("task does not have the exact exited-shell done-close failure for the supplied pane id.")
            _ = update_frontmatter_status(current_text, "done", "", args.root)
            owners = authoritative_active_target_task_paths(args.root, metadata.runat)
            if owners != (path,):
                refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
                raise TaskFrontmatterError(f"task is not the sole authoritative active owner of `{metadata.runat}`: {refs}.")
            todo_before = todo.stat()
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = reconcile_todo_text(args.root, path, todo_text, metadata.runat, "previous", ("current",))
            if exact_pane_id(metadata.runat) != args.pane_id:
                raise TaskFrontmatterError("task target no longer resolves to the supplied exact pane id.")
            close_exited_codex_shell(metadata.runat, args.pane_id, args.session_id, args.terminal_evidence)
            closed_text = current_text.rstrip("\n") + close_note(metadata.runat, args.session_id)
            done_text = update_frontmatter_status(closed_text, "done", "", args.root)
            moved_todo_before: os.stat_result | None = None
            try:
                if updated_todo != todo_text:
                    replace_if_unchanged_locked(todo, updated_todo, todo_before)
                    moved_todo_before = todo.stat()
                replace_if_unchanged_locked(path, done_text, current_before)
            except Exception as exc:
                rollback_error: Exception | None = None
                if moved_todo_before is not None:
                    try:
                        replace_if_unchanged_locked(todo, todo_text, moved_todo_before)
                    except Exception as caught:
                        rollback_error = caught
                if rollback_error is not None:
                    raise TaskFrontmatterError(f"exited shell closed but TODO rollback failed: {rollback_error}") from exc
                retry_before = path.stat()
                retry_text = path.read_text(encoding="utf-8")
                if retry_text != current_text:
                    raise TaskFrontmatterError("exited shell closed but task changed before retry bookkeeping could be recorded.") from exc
                retry_with_note = retry_text.rstrip("\n") + close_note(metadata.runat, args.session_id)
                retry_blocked = update_frontmatter_status(retry_with_note, "blocked", done_bookkeeping_failed_reason(exc), args.root)
                replace_if_unchanged_locked(path, retry_blocked, retry_before)
                raise TaskFrontmatterError(f"exited shell closed; done bookkeeping failed and was recorded for retry: {exc}") from exc
    return metadata.runat, args.session_id


def finish_replaced_done(args: Args, path: Path, text: str, before: os.stat_result) -> str:
    replacement_path = args.replacement_task
    audit_path = args.audit_output
    if replacement_path is None or audit_path is None:
        raise TaskFrontmatterError("replacement task and audit output are required.")
    initial = parse_task_metadata(text, args.root)
    if initial is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    targets = sorted({initial.runat, args.replacement_target})
    todo = args.root / "TODO.md"
    with ExitStack() as locks:
        for target in targets:
            locks.enter_context(task_target_lock(args.root, target))
        for locked_path in sorted({path, replacement_path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        if not same_file_state(before, current_before) or current_text != text:
            raise TaskFrontmatterError("stale task changed before replacement closure acquired its locks; retry.")
        updated, stale, replacement, replacement_text, replacement_before, replacement_pane_id = replacement_task_text(args, path, current_text, current_before)
        prepared = "\n".join(
            (
                "operation: finish-replaced-done",
                f"stale-task: {relative_task_ref(args.root, path)}",
                f"stale-target: {stale.runat}",
                f"stale-sha256: {args.stale_sha256}",
                f"replacement-task: {relative_task_ref(args.root, replacement_path)}",
                f"replacement-target: {replacement.runat}",
                f"replacement-sha256: {args.replacement_sha256}",
                f"replacement-status: {replacement.status}",
                f"replacement-pane-id: {replacement_pane_id}",
                f"stopped-evidence-sha256: {hashlib.sha256(args.stopped_evidence.encode()).hexdigest()}",
                f"replacement-pane-evidence-sha256: {hashlib.sha256(args.replacement_pane_evidence.encode()).hexdigest()}",
                f"manager-target: {stale.managerat}",
                f"tool: {stale.tool}",
                f"is-manager: {str(stale.is_manager).lower()}",
                "completion: unknown-until-finalized",
                "",
            )
        )
        reserve_private_audit(audit_path, prepared)
        try:
            if replacement_path.read_text(encoding="utf-8") != replacement_text or not same_file_state(replacement_before, replacement_path.stat()):
                raise TaskFrontmatterError("replacement task changed immediately before stale lifecycle mutation; retry.")
            if exact_pane_id(stale.runat):
                raise TaskFrontmatterError("stale target became live after audit reservation; retry.")
            if exact_pane_id(replacement.runat) != replacement_pane_id:
                raise TaskFrontmatterError("replacement pane changed after audit reservation; retry.")
            if args.replacement_pane_evidence not in capture(replacement_pane_id, 2000):
                raise TaskFrontmatterError("replacement pane evidence disappeared after audit reservation; retry.")
            if exact_pane_id(replacement.runat) != replacement_pane_id:
                raise TaskFrontmatterError("replacement pane changed while post-reservation evidence was checked; retry.")
            if exact_pane_id(stale.runat):
                raise TaskFrontmatterError("stale target became live while post-reservation evidence was checked; retry.")
            finish_done_transaction(args.root, path, updated, current_before, locked=True)
        except Exception as mutation_error:
            try:
                finish_private_audit(audit_path, prepared, "not-completed")
            except Exception as audit_error:
                mutation_error.add_note(f"private audit finalization also failed; audit remains completion-unknown: {audit_error}")
            raise
        finish_private_audit(audit_path, prepared, "success")
    return stale.runat


def run(args: Args) -> int:
    target = ""
    session_id = ""
    preserved_replacement = False
    shared_target_closure = False
    try:
        path = task_path(args.root, args.task_file)
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        initial_metadata = parse_manager_child_metadata(text, args.root) if args.restore_terminal_target or args.close_retired_done else parse_task_metadata(text, args.root)
        if initial_metadata is not None and initial_metadata.version == V2_VERSION and not v2_enabled(args.root):
            raise BlockingError("v2 task writes are disabled until reviewed migration enablement")
        if initial_metadata is not None and initial_metadata.version != V2_VERSION and v2_enabled(args.root):
            raise BlockingError("v1 task writes are disabled after v2 enablement")
        if args.restore_terminal_target:
            restore_terminal_target(args, path, text, before)
        elif args.close_retired_done:
            target = close_retired_done(args, path, text, before)
        elif args.normalize_retired_todo:
            normalize_retired_todo(args, path, text, before)
        elif args.normalize_low_priority_current:
            normalize_low_priority_current(args, path, text, before)
        elif args.reconcile_blocked_index:
            reconcile_previous_blocked_index(args, path, text, before)
        elif args.retire_blocked_target:
            retire_blocked_target(args, path, text, before)
        elif args.reconcile_long_running_human_index:
            reconcile_long_running_human_index(args.root, path, text, before)
        elif args.finish_replaced_done:
            target = finish_replaced_done(args, path, text, before)
            preserved_replacement = True
        elif args.recover_exited_shell_done:
            target, session_id = recover_exited_shell_done(args, path, text, before)
        elif args.finish_closed_done:
            target, session_id = finish_closed_done(args, path, text, before)
        elif args.close_shared_target:
            target = finish_shared_target_done(args, path, text, before)
            shared_target_closure = True
        else:
            metadata = parse_task_metadata(text, args.root)
            if metadata is not None and args.status == "done":
                ensure_manager_has_no_active_children(args.root, path, metadata)
                if args.closure_repository is not None:
                    ensure_repository_closure_custody(args.closure_repository, args.dirty_path_handoff)
            updated = update_frontmatter_status(text, args.status, args.blocked_on, args.root)
            already_done = metadata is not None and metadata.status == "done" and args.status == "done"
            target = metadata.runat if metadata is not None and args.status == "done" and not already_done else ""
            close_args: StopArgs | None = None
            if already_done:
                reconcile_done_index(args.root, path, text, before)
            elif target:
                if metadata is not None and metadata.runat.partition(":")[0].startswith("h"):
                    validate_human_close_authorization(
                        human_close_stop_args(
                            args.root,
                            path.relative_to(args.root).as_posix(),
                            metadata.runat,
                            args.human_close_authorization_source,
                            args.human_close_authorization_sha256,
                            metadata.runat,
                        )
                    )
                in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS, args.root)
                replace_if_unchanged(path, in_progress, before)
                try:
                    assert metadata is not None
                    close_args, session_id = stop_done_agent(
                        args.root,
                        path,
                        metadata,
                        args.human_close_authorization_source,
                        args.human_close_authorization_sha256,
                    )
                except Exception as exc:
                    rollback_before = path.stat()
                    rollback_text = path.read_text(encoding="utf-8")
                    rollback = update_frontmatter_status(rollback_text, "blocked", done_close_failed_reason(exc), args.root)
                    replace_if_unchanged(path, rollback, rollback_before)
                    raise
                before = path.stat()
            if close_args is None and not already_done:
                updated_metadata = parse_task_metadata(updated, args.root)
                if args.status == "blocked" and initial_metadata is not None and initial_metadata.status == "blocked" and updated_metadata is not None and updated_metadata.status == "blocked":
                    reconcile_blocked_index(args.root, path, text, updated, before)
                elif args.status == "running" and updated_metadata is not None and updated_metadata.status == "running":
                    if initial_metadata is not None and initial_metadata.status == "running":
                        reconcile_running_index(args.root, path, text, before)
                    else:
                        transition_running_index(args.root, path, text, updated, before)
                else:
                    replace_if_unchanged(path, updated, before)
            elif close_args is not None:
                try:
                    record_close(close_args, session_id)
                except Exception as exc:
                    mark_done_bookkeeping_failed(args.root, path, exc)
                    raise TaskFrontmatterError(f"done close bookkeeping failed after closing agent; task marked blocked for retry: {exc}") from exc
                before = path.stat()
                updated = update_frontmatter_status(path.read_text(encoding="utf-8"), args.status, args.blocked_on, args.root)
                replace_if_unchanged(path, updated, before)
        final_metadata = parse_task_metadata(path.read_text(encoding="utf-8"), args.root)
        if final_metadata is not None and final_metadata.version == V2_VERSION:
            _ = blocking_request(args.root, {"operation": "reconcile"})
    except (OSError, TaskFrontmatterError, BlockingError) as exc:
        print(f"omo_task_status.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"omo_task_status.py: failed to close done agent: {exc}", file=sys.stderr)
        return 2
    if args.status == "done":
        if preserved_replacement:
            print(f"Finalized stopped stale task {target} without signaling it or the live successor pane.")
        elif args.close_retired_done:
            print(f"Finalized retired task metadata with historical target {target}; no pane was signalled.")
        elif target and not shared_target_closure:
            print(done_close_message(target, session_id))
        print(DONE_REMINDER)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
