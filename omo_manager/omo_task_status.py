#!/usr/bin/env python3
"""Safely update task-file frontmatter status."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TASK_FRONTMATTER_STATUSES
from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import has_close_note
from omo_manager.omo_codex_stop import record_close
from omo_manager.omo_codex_stop import stop
from omo_manager.omo_agent_status import frontmatter_parts
from omo_manager.omo_agent_status import parse_task_metadata

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


def stop_done_agent(root: Path, path: Path, target: str) -> tuple[StopArgs, str]:
    """Close the task's Codex pane and return the captured session id."""

    task_file = path.relative_to(root).as_posix()
    stop_args = StopArgs(target, 10.0, 2000, False, False, root, task_file, True, 0.0)
    session_id = stop(stop_args)
    return stop_args, session_id


def done_close_message(target: str, session_id: str) -> str:
    if session_id:
        return f"Closed {target}; session_id: {session_id}."
    return f"Closed {target}; Codex session id not found."


def done_bookkeeping_failed_reason(exc: Exception) -> str:
    reason = " ".join(str(exc).split())
    return f"{BOOKKEEPING_FAILED_PREFIX}: {reason or exc.__class__.__name__}"


def is_bookkeeping_failed_reason(blocked_on: str) -> bool:
    return blocked_on == BOOKKEEPING_FAILED_PREFIX or blocked_on.startswith(f"{BOOKKEEPING_FAILED_PREFIX}: ")


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
    retryable_blocker = is_bookkeeping_failed_reason(metadata.blocked_on) or (
        metadata.blocked_on == DONE_CLOSE_IN_PROGRESS and has_close_note(text, metadata.runat, args.session_id)
    )
    if metadata.status != "blocked" or not retryable_blocker:
        raise TaskFrontmatterError("--finish-closed-done requires a task blocked by failed done close or close bookkeeping.")
    _ = update_frontmatter_status(text, "done", "")
    close_args = StopArgs(metadata.runat, 10.0, 2000, False, False, args.root, path.relative_to(args.root).as_posix(), True, 0.0)
    try:
        record_close(close_args, args.session_id)
    except Exception as exc:
        mark_done_bookkeeping_failed(path, exc)
        raise TaskFrontmatterError(f"done close bookkeeping retry failed; task marked blocked for retry: {exc}") from exc
    after = path.stat()
    updated = update_frontmatter_status(path.read_text(encoding="utf-8"), "done", "")
    replace_if_unchanged(path, updated, after)
    return metadata.runat, args.session_id


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
            target = metadata.runat if metadata is not None and args.status == "done" else ""
            updated = update_frontmatter_status(text, args.status, args.blocked_on)
            close_args: StopArgs | None = None
            if target:
                in_progress = update_frontmatter_status(text, "blocked", DONE_CLOSE_IN_PROGRESS)
                replace_if_unchanged(path, in_progress, before)
                try:
                    close_args, session_id = stop_done_agent(args.root, path, target)
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
