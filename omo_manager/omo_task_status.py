#!/usr/bin/env python3
"""Safely update task-file frontmatter status."""
from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TASK_FRONTMATTER_STATUSES
from omo_manager.omo_agent_status import TaskMetadata
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import has_close_note
from omo_manager.omo_codex_stop import record_close
from omo_manager.omo_codex_stop import stop
from omo_manager.omo_agent_status import frontmatter_parts
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_task_lock import task_target_lock

PENDING_MARKER = "(pending)"
DONE_REMINDER = "Status set to done. Remember to email the human."
BOOKKEEPING_FAILED_PREFIX = "done_close_bookkeeping_failed"
CLOSE_FAILED_PREFIX = "done_close_failed"
DONE_CLOSE_IN_PROGRESS = "done_close_in_progress: manager is closing the agent before marking done"


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: Path
    status: str
    blocked_on: str
    finish_closed_done: bool = False
    session_id: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: Path
    status: str = ""
    blocked_on: str = ""
    finish_closed_done: bool = False
    session_id: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--finish-closed-done", action="store_true", help="Finish done bookkeeping after the agent was already closed by a failed prior run.")
    _ = parser.add_argument("--session-id", default="", help="Session id captured by the prior close, if available.")
    _ = parser.add_argument("task_file", type=Path)
    _ = parser.add_argument("status", nargs="?", choices=sorted(TASK_FRONTMATTER_STATUSES))
    _ = parser.add_argument("--blocked-on", default="", help="Required when setting status to `blocked`; removed for all other statuses.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.finish_closed_done:
        if parsed.status not in {None, "", "done"}:
            parser.error("--finish-closed-done only supports status `done`.")
        return Args(parsed.root.resolve(), parsed.task_file, "done", parsed.blocked_on.strip(), True, parsed.session_id.strip())
    if not parsed.status:
        parser.error("status is required unless --finish-closed-done is used.")
    if parsed.session_id:
        parser.error("--session-id is only valid with --finish-closed-done.")
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


def parse_manager_child_metadata(text: str) -> TaskMetadata | None:
    """Validate historical `done` plus `retired` children without changing active-task rules."""
    parts = frontmatter_parts(text)
    if parts is None:
        return None
    frontmatter, body = parts
    fields = {key: value.strip() for line in frontmatter for key, sep, value in (line.partition(":"),) if sep}
    if fields.get("status") != "done" or fields.get("runat") != "retired":
        return parse_task_metadata(text)
    compatible: list[str] = []
    for line in frontmatter:
        key, sep, _value = line.partition(":")
        compatible.append("status: blocked" if sep and key == "status" else line)
        if sep and key == "status":
            compatible.append("blocked_on: archived completed task")
    trailing_newline = "\n" if text.endswith("\n") else ""
    validated = parse_task_metadata("\n".join(["---", *compatible, "---", *body]) + trailing_newline)
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
            metadata = parse_manager_child_metadata(text)
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


def update_frontmatter_status(text: str, status: str, blocked_on: str) -> str:
    """Return task text with validated `status` and `blocked_on` frontmatter."""
    metadata = parse_task_metadata(text)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    if has_pending_marker(text):
        raise TaskFrontmatterError("task file still contains `(pending)`; handle pending markers before changing status.")
    if status == "done" and metadata.pending_task_items:
        raise TaskFrontmatterError(
            "task file still has `pending_task_items`; verify each pending item is actually complete or cancelled, then remove it before marking done."
        )
    if status == "blocked" and not blocked_on:
        raise TaskFrontmatterError("`--blocked-on` is required when setting status to `blocked`.")
    if "\n" in blocked_on or "\r" in blocked_on:
        raise TaskFrontmatterError("`--blocked-on` must be one line.")
    if status != "blocked" and blocked_on:
        raise TaskFrontmatterError("`--blocked-on` is only valid when setting status to `blocked`.")
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
            if status == "blocked":
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
    _ = parse_task_metadata(updated_text)
    return updated_text


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mtime_ns == right.st_mtime_ns and left.st_size == right.st_size


def replace_if_unchanged(path: Path, text: str, before: os.stat_result) -> None:
    """Replace `path` atomically after checking it did not change since read."""
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
            metadata = parse_task_metadata(text)
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
        allow_self = bool(stable_pane_id and worker_self_close_allowed(root, path, metadata))
        if allow_self:
            allow_self = worker_self_close_allowed(root, path, metadata) and exact_pane_id(metadata.runat) == stable_pane_id
        stop_target = stable_pane_id if allow_self else metadata.runat
        stop_args = StopArgs(stop_target, 10.0, 2000, False, allow_self, root, task_file, True, 0.0)
        session_id = stop(stop_args)
    record_args = StopArgs(metadata.runat, 10.0, 2000, False, allow_self, root, task_file, True, 0.0)
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


def done_close_failed_reason(exc: Exception) -> str:
    reason = " ".join(str(exc).split())
    return f"{CLOSE_FAILED_PREFIX}: {reason or exc.__class__.__name__}"


def mark_done_bookkeeping_failed(path: Path, exc: Exception) -> None:
    rollback_before = path.stat()
    rollback_text = path.read_text(encoding="utf-8")
    rollback = update_frontmatter_status(rollback_text, "blocked", done_bookkeeping_failed_reason(exc))
    replace_if_unchanged(path, rollback, rollback_before)


def finish_closed_done(args: Args, path: Path, text: str, before: os.stat_result) -> tuple[str, str]:
    metadata = parse_task_metadata(text)
    if metadata is None:
        raise TaskFrontmatterError("task file has no frontmatter.")
    ensure_manager_has_no_active_children(args.root, path, metadata)
    verified_already_closed = (
        not metadata.is_manager
        and is_close_failed_reason(metadata.blocked_on)
        and not exact_pane_id(metadata.runat)
        and task_is_in_todo_section(args.root, path, "previous")
    )
    retryable_blocker = is_bookkeeping_failed_reason(metadata.blocked_on) or verified_already_closed or (
        metadata.blocked_on == DONE_CLOSE_IN_PROGRESS and has_close_note(text, metadata.runat, args.session_id)
    )
    if metadata.status != "blocked" or not retryable_blocker:
        raise TaskFrontmatterError("--finish-closed-done requires a task blocked by failed done close or close bookkeeping.")
    _ = update_frontmatter_status(text, "done", "")
    close_session_id = "" if verified_already_closed else args.session_id
    close_args = StopArgs(metadata.runat, 10.0, 2000, False, False, args.root, path.relative_to(args.root).as_posix(), True, 0.0)
    try:
        record_close(close_args, close_session_id)
    except Exception as exc:
        mark_done_bookkeeping_failed(path, exc)
        raise TaskFrontmatterError(f"done close bookkeeping retry failed; task marked blocked for retry: {exc}") from exc
    after = path.stat()
    updated = update_frontmatter_status(path.read_text(encoding="utf-8"), "done", "")
    replace_if_unchanged(path, updated, after)
    return metadata.runat, close_session_id


def run(args: Args) -> int:
    target = ""
    session_id = ""
    try:
        path = task_path(args.root, args.task_file)
        before = path.stat()
        text = path.read_text(encoding="utf-8")
        if args.finish_closed_done:
            target, session_id = finish_closed_done(args, path, text, before)
        else:
            metadata = parse_task_metadata(text)
            if metadata is not None and args.status == "done":
                ensure_manager_has_no_active_children(args.root, path, metadata)
            target = metadata.runat if metadata is not None and args.status == "done" else ""
            updated = update_frontmatter_status(text, args.status, args.blocked_on)
            close_args: StopArgs | None = None
            if target:
                in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS)
                replace_if_unchanged(path, in_progress, before)
                try:
                    assert metadata is not None
                    close_args, session_id = stop_done_agent(args.root, path, metadata)
                except Exception as exc:
                    rollback_before = path.stat()
                    rollback_text = path.read_text(encoding="utf-8")
                    rollback = update_frontmatter_status(rollback_text, "blocked", done_close_failed_reason(exc))
                    replace_if_unchanged(path, rollback, rollback_before)
                    raise
                before = path.stat()
            if close_args is None:
                replace_if_unchanged(path, updated, before)
            else:
                try:
                    record_close(close_args, session_id)
                except Exception as exc:
                    mark_done_bookkeeping_failed(path, exc)
                    raise TaskFrontmatterError(f"done close bookkeeping failed after closing agent; task marked blocked for retry: {exc}") from exc
                before = path.stat()
                updated = update_frontmatter_status(path.read_text(encoding="utf-8"), args.status, args.blocked_on)
                replace_if_unchanged(path, updated, before)
    except (OSError, TaskFrontmatterError) as exc:
        print(f"omo_task_status.py: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"omo_task_status.py: failed to close done agent: {exc}", file=sys.stderr)
        return 2
    if args.status == "done":
        if target:
            print(done_close_message(target, session_id))
        print(DONE_REMINDER)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
