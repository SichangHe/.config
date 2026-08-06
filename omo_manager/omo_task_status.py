#!/usr/bin/env python3
"""Safely update task-file frontmatter status."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

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
from omo_manager.omo_codex_stop import has_close_note
from omo_manager.omo_codex_stop import moved_todo_text
from omo_manager.omo_codex_stop import pane_id
from omo_manager.omo_codex_stop import record_close
from omo_manager.omo_codex_stop import stop
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_task_metadata import TARGET_RE
from omo_manager.omo_blocking_actor import request as blocking_request

PENDING_MARKER = "(pending)"
DONE_REMINDER = "Status set to done. Remember to email the human."
BOOKKEEPING_FAILED_PREFIX = "done_close_bookkeeping_failed"
CLOSE_FAILED_PREFIX = "done_close_failed"
DONE_CLOSE_IN_PROGRESS = "done_close_in_progress: manager is closing the agent before marking done"
TODO_ROW_RE = re.compile(r"\s*`?([A-Za-z0-9_./-]+\.md)`?(?:\s+(.*?))?\s*")


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
    stopped_evidence: str = ""
    replacement_pane_evidence: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: Path
    status: str = ""
    blocked_on: str = ""
    finish_closed_done: bool = False
    session_id: str = ""
    finish_replaced_done: bool = False
    replacement_task: Path | None = None
    stopped_evidence: str = ""
    replacement_pane_evidence: str = ""


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
    _ = parser.add_argument("--session-id", default="", help="Session id captured by the prior close, if available.")
    _ = parser.add_argument("--replacement-task", type=Path, help="Active replacement task file; required with --finish-replaced-done.")
    _ = parser.add_argument("--stopped-evidence", default="", help="Exact evidence from a prior verified pending-item removal; required with --finish-replaced-done.")
    _ = parser.add_argument("--replacement-pane-evidence", default="", help="Exact text currently visible in the replacement pane; required with --finish-replaced-done.")
    _ = parser.add_argument("task_file", type=Path)
    _ = parser.add_argument("status", nargs="?", choices=sorted(TASK_FRONTMATTER_STATUSES))
    _ = parser.add_argument("--blocked-on", default="", help="Required when setting status to `blocked`; optional for `long_running`; removed for other statuses.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.finish_closed_done and parsed.finish_replaced_done:
        parser.error("--finish-closed-done and --finish-replaced-done are mutually exclusive.")
    if parsed.finish_replaced_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--finish-replaced-done only supports status `done`.")
        if parsed.session_id:
            parser.error("--session-id is only valid with --finish-closed-done.")
        if parsed.replacement_task is None or not parsed.stopped_evidence.strip() or not parsed.replacement_pane_evidence.strip():
            parser.error("--finish-replaced-done requires --replacement-task, --stopped-evidence, and --replacement-pane-evidence.")
        return Args(
            parsed.root.resolve(),
            parsed.task_file,
            "done",
            parsed.blocked_on.strip(),
            finish_replaced_done=True,
            replacement_task=parsed.replacement_task.expanduser().resolve(strict=False),
            stopped_evidence=parsed.stopped_evidence.strip(),
            replacement_pane_evidence=parsed.replacement_pane_evidence.strip(),
        )
    if parsed.finish_closed_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--finish-closed-done only supports status `done`.")
        return Args(parsed.root.resolve(), parsed.task_file, "done", parsed.blocked_on.strip(), True, parsed.session_id.strip())
    if not parsed.status:
        parser.error("status is required unless --finish-closed-done is used.")
    if parsed.session_id:
        parser.error("--session-id is only valid with --finish-closed-done.")
    if parsed.replacement_task is not None or parsed.stopped_evidence or parsed.replacement_pane_evidence:
        parser.error("replacement evidence is only valid with --finish-replaced-done.")
    return Args(parsed.root.resolve(), parsed.task_file, parsed.status, parsed.blocked_on.strip())


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
        elif metadata.status != "done" and same_tmux_target(metadata.runat, target):
            matches.add(candidate)
    return tuple(sorted(matches))


def worker_self_close_allowed(root: Path, path: Path, metadata: TaskMetadata) -> bool:
    """Allow self-close only for the sole current non-manager owner of its pane."""

    if metadata.is_manager:
        return False
    manager_target = os.environ.get("OMO_MANAGER_TMUX_TARGET", "").strip()
    if manager_target and same_tmux_target(metadata.runat, manager_target):
        return False
    return current_target_task_paths(root, metadata.runat) == (path,)


def stop_done_agent(root: Path, path: Path, metadata: TaskMetadata) -> tuple[StopArgs, str]:
    """Close the task's Codex pane and return the captured session id."""

    task_file = path.relative_to(root).as_posix()
    with task_target_lock(root, metadata.runat):
        stable_pane_id = exact_pane_id(metadata.runat)
        record_args = StopArgs(metadata.runat, 10.0, 2000, False, False, root, task_file, True, 0.0)
        if not stable_pane_id:
            return record_args, ""
        allow_self = bool(stable_pane_id and worker_self_close_allowed(root, path, metadata))
        if allow_self:
            allow_self = worker_self_close_allowed(root, path, metadata) and exact_pane_id(metadata.runat) == stable_pane_id
        stop_args = StopArgs(stable_pane_id, 10.0, 2000, False, allow_self, root, task_file, True, 0.0)
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


def reconcile_todo_text(root: Path, path: Path, text: str, runat: str, destination: str, allowed_sections: tuple[str, ...]) -> str:
    """Move one validated TODO row between allowed lifecycle sections."""
    lines = text.splitlines(keepends=True)
    section = ""
    rows: list[tuple[int, str]] = []
    destination_headers: list[int] = []
    for idx, line in enumerate(lines):
        name = line.strip()
        if name.endswith(":"):
            section = name[:-1]
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
    if source_section == destination:
        return text
    if source_section not in allowed_sections:
        expected = " or ".join(f"`{candidate}:`" for candidate in allowed_sections)
        raise TaskFrontmatterError(f"expected `{relative_task_ref(root, path)}` to be in {expected}, found `{source_section}:`.")
    moved = lines.pop(source_idx)
    destination_idx = next(idx for idx, line in enumerate(lines) if line.strip() == f"{destination}:")
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


def reconcile_blocked_index(root: Path, path: Path, text: str, before: os.stat_result, blocked_on: str) -> None:
    """Move an unchanged blocked task's sole current TODO row into `human pending`."""
    todo = root / "TODO.md"
    if not todo.is_file():
        raise TaskFrontmatterError("TODO.md is not a regular file.")
    with ExitStack() as locks:
        for locked_path in sorted({path, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(locked_path))
        current_before = path.stat()
        current_text = path.read_text(encoding="utf-8")
        current_metadata = parse_task_metadata(current_text, root)
        if not same_file_state(before, current_before) or current_text != text or current_metadata is None or current_metadata.status != "blocked" or current_metadata.blocked_on != blocked_on:
            raise TaskFrontmatterError("task changed or no longer matches the blocked status reconciliation; retry after rereading it.")
        todo_before = todo.stat()
        todo_text = todo.read_text(encoding="utf-8")
        updated_todo = reconcile_blocked_todo_text(root, path, todo_text, current_metadata.runat)
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


def finish_done_transaction(root: Path, path: Path, text: str, before: os.stat_result) -> None:
    """Atomically replace each bookkeeping file and roll back `TODO.md` if the task replacement fails."""
    todo = root / "TODO.md"
    if not todo.exists():
        replace_if_unchanged(path, text, before)
        return
    todo_before = todo.stat()
    todo_text = todo.read_text(encoding="utf-8")
    task_file = path.relative_to(root).as_posix()
    updated_todo = moved_todo_text(root, task_file, todo_text)
    if updated_todo == todo_text:
        replace_if_unchanged(path, text, before)
        return
    replace_if_unchanged(todo, updated_todo, todo_before)
    moved_todo_state = todo.stat()
    try:
        replace_if_unchanged(path, text, before)
    except Exception as exc:
        try:
            replace_if_unchanged(todo, todo_text, moved_todo_state)
        except Exception as rollback_exc:
            raise TaskFrontmatterError(f"task update failed and TODO rollback also failed: {rollback_exc}") from exc
        raise


def replacement_task_text(args: Args, stale_path: Path, stale_text: str, stale_before: os.stat_result) -> str:
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
    if stale.status not in {"blocked", "running", "long_running"}:
        raise TaskFrontmatterError("--finish-replaced-done requires a blocked stale task or an empty running stale task.")
    ensure_manager_has_no_active_children(args.root, stale_path, stale)
    verified_line = f"(verified removed pending item: {args.stopped_evidence})"
    verified_empty_line = f"(verified empty stale task: {args.stopped_evidence})"
    if stale.pending_task_items:
        if stale.status != "blocked" or verified_line not in stale_text.splitlines():
            raise TaskFrontmatterError("stopped evidence does not match an exact verified pending-item removal in the blocked stale task.")
    elif verified_empty_line not in stale_text.splitlines() and (stale.status != "blocked" or verified_line not in stale_text.splitlines()):
        raise TaskFrontmatterError("empty stale task requires an exact verified empty-stale-task record.")
    replacement_before = replacement_path.stat()
    replacement_text = replacement_path.read_text(encoding="utf-8")
    replacement = parse_task_metadata(replacement_text, args.root)
    if replacement is None:
        raise TaskFrontmatterError("replacement task file has no frontmatter.")
    if replacement.status not in {"running", "long_running", "blocked"} or not replacement.pending_task_items:
        raise TaskFrontmatterError("replacement task must be active with at least one real pending item.")
    if not task_is_in_todo_section(args.root, replacement_path, "current"):
        raise TaskFrontmatterError("replacement task must be listed in the current TODO section.")
    if not same_tmux_target(stale.runat, replacement.runat):
        raise TaskFrontmatterError("replacement task `runat` does not match the stale reused pane.")
    if (stale.managerat, stale.tool, stale.is_manager) != (replacement.managerat, replacement.tool, replacement.is_manager):
        raise TaskFrontmatterError("replacement task ownership or role does not match the stale task.")
    pane_id = exact_pane_id(stale.runat)
    if not pane_id:
        raise TaskFrontmatterError("stale reused pane is not an exact live pane target.")
    pane_text = capture(pane_id, 2000)
    if args.replacement_pane_evidence not in pane_text:
        raise TaskFrontmatterError("replacement pane evidence is missing from the live reused pane.")
    if exact_pane_id(stale.runat) != pane_id:
        raise TaskFrontmatterError("stale reused pane changed while replacement evidence was checked; retry.")
    if not same_file_state(stale_before, stale_path.stat()):
        raise TaskFrontmatterError("stale task changed while replacement evidence was being checked; retry.")
    if not same_file_state(replacement_before, replacement_path.stat()):
        raise TaskFrontmatterError("replacement task changed while evidence was being checked; retry.")
    return update_frontmatter_status(stale_text, "done", "", args.root)


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


def finish_replaced_done(args: Args, path: Path, text: str, before: os.stat_result) -> str:
    updated = replacement_task_text(args, path, text, before)
    finish_done_transaction(args.root, path, updated, before)
    metadata = parse_task_metadata(text, args.root)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    return metadata.runat


def run(args: Args) -> int:
    target = ""
    session_id = ""
    preserved_replacement = False
    try:
        path = task_path(args.root, args.task_file)
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        initial_metadata = parse_task_metadata(text, args.root)
        if initial_metadata is not None and initial_metadata.version == V2_VERSION and not v2_enabled(args.root):
            raise BlockingError("v2 task writes are disabled until reviewed migration enablement")
        if initial_metadata is not None and initial_metadata.version != V2_VERSION and v2_enabled(args.root):
            raise BlockingError("v1 task writes are disabled after v2 enablement")
        if args.finish_replaced_done:
            target = finish_replaced_done(args, path, text, before)
            preserved_replacement = True
        elif args.finish_closed_done:
            target, session_id = finish_closed_done(args, path, text, before)
        else:
            metadata = parse_task_metadata(text, args.root)
            if metadata is not None and args.status == "done":
                ensure_manager_has_no_active_children(args.root, path, metadata)
            target = metadata.runat if metadata is not None and args.status == "done" else ""
            updated = update_frontmatter_status(text, args.status, args.blocked_on, args.root)
            close_args: StopArgs | None = None
            if target:
                in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS, args.root)
                replace_if_unchanged(path, in_progress, before)
                try:
                    assert metadata is not None
                    close_args, session_id = stop_done_agent(args.root, path, metadata)
                except Exception as exc:
                    rollback_before = path.stat()
                    rollback_text = path.read_text(encoding="utf-8")
                    rollback = update_frontmatter_status(rollback_text, "blocked", done_close_failed_reason(exc), args.root)
                    replace_if_unchanged(path, rollback, rollback_before)
                    raise
                before = path.stat()
            if close_args is None:
                updated_metadata = parse_task_metadata(updated, args.root)
                if args.status == "blocked" and initial_metadata is not None and initial_metadata.status == "blocked" and updated_metadata is not None and updated_metadata.status == "blocked":
                    reconcile_blocked_index(args.root, path, text, before, args.blocked_on)
                elif initial_metadata is not None and initial_metadata.status == "running" and updated_metadata is not None and updated_metadata.status == "running":
                    reconcile_running_index(args.root, path, text, before)
                else:
                    replace_if_unchanged(path, updated, before)
            else:
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
            print(f"Finalized stale task without signaling reused replacement pane {target}.")
        elif target:
            print(done_close_message(target, session_id))
        print(DONE_REMINDER)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
