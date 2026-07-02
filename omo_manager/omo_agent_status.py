#!/usr/bin/env python3
"""Print a concise manager agent status summary from markdown and tmux state."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import COMPACTING_RE
from omo_manager.omo_codex_status import Report
from omo_manager.omo_codex_status import inspect
from omo_manager.omo_codex_status import is_stock_placeholder_input_text
from omo_manager.omo_codex_status import submit_stuck_input_if_present
from omo_manager.omo_codex_status import visible_error_lines
from omo_manager.omo_stuck_watch import read_json, write_json_private


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


def load_local_env() -> dict[str, str]:
    env = dict(os.environ)
    local_env = Path(env.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config" / "omo_manager" / "local.env"))
    if not local_env.is_file():
        return env
    loaded = subprocess.run(["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(local_env)], capture_output=True, timeout=10, check=False)
    if loaded.returncode != 0:
        return env
    for item in loaded.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        raw_key, raw_value = item.split(b"=", 1)
        key = raw_key.decode(errors="ignore")
        if key and key not in os.environ:
            env[key] = raw_value.decode(errors="surrogateescape")
    return env


LOCAL_ENV = load_local_env()
DEFAULT_ROOT = Path(LOCAL_ENV.get("OMO_WORK_LOGS_ROOT", str(Path.home() / "work_logs")))
DEFAULT_REGISTRY = Path(LOCAL_ENV.get("OMO_MANAGER_SESSION_REGISTRY", str(default_state_dir() / "sessions.json")))
DEFAULT_MANAGER_TARGET = ""
PENDING_TASK_ITEMS_MARKER = "(above are pending task items)"
TASK_RE = re.compile(r"`?([A-Za-z0-9_./-]+\.md)`?")
TARGET_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
TARGET_SESSION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):")
LOOSE_TARGET_RE = re.compile(r"\b([a-z][A-Za-z0-9_-]*)\s+(\d+)\b")
PORT_RE = re.compile(r"\bport [`']?(\d{2,5})[`']?")
STATUS_DETAIL_RE = re.compile(r"^\((pending|running|done|blocked)(?::\s*([^)]*))?\)(?:\s+\(([^)]*)\))?$")
RUNAT_RE = re.compile(r"^runat:\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
CLOSE_TARGET_RE = re.compile(r"\btmux target [`']?([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)[`']?")
PERSISTENT_ROLE_RE = re.compile(r"\bpersistent\b.*\brole\b")
MANAGER_MD_REREAD_RE = re.compile(r"\b(?:re-?read|read)\b[^.\n?!;]{0,80}\bMANAGER\.md\b|\bMANAGER\.md\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b", re.IGNORECASE)
MANAGER_MD_REREAD_NEGATIVE_RE = re.compile(
    r"\b(?:did not|didn't|not|never|without|failed to|fails to|hasn't|haven't|hadn't|no need to|no need)\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b[^.\n?!;]{0,80}\bMANAGER\.md\b|"
    r"\bMANAGER\.md\b[^.\n?!;]{0,80}\b(?:was|is|has been|will be)?\s*(?:not|never)\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b",
    re.IGNORECASE,
)


def report_output_evidence(report: Report) -> str:
    if not report.lines:
        return ""
    tail = report.lines[-3:]
    if report.status != "error":
        return " output=" + " / ".join(tail)
    errors = visible_error_lines(report.lines, include_unmarked=False) or visible_error_lines(report.lines)
    if not errors:
        return " output=" + " / ".join(tail)
    evidence = " output=" + " / ".join(errors[-3:])
    if tail != errors[-3:]:
        evidence += " output_tail=" + " / ".join(tail)
    return evidence


def manager_compaction_needs_reread(report: Report) -> bool:
    if not any(COMPACTING_RE.search(line) is not None for line in report.lines):
        return False
    return not any("?" not in line and MANAGER_MD_REREAD_RE.search(line) is not None and MANAGER_MD_REREAD_NEGATIVE_RE.search(line) is None for line in report.lines)


@dataclass(frozen=True)
class Args:
    root: Path
    registry: Path
    prune_completed: bool
    exit_code_if_active: bool
    problems_only: bool = False
    manager_target: str = ""
    auto_unstick: bool = True


@dataclass(frozen=True)
class TaskLine:
    task_file: str
    section: str
    line: str
    target: str
    port: int | None
    status: str = ""
    persistent_role: bool = False


@dataclass(frozen=True)
class SessionRecord:
    task_file: str
    target: str
    port: int | None
    started_at_s: float


@dataclass(frozen=True)
class StatusRow:
    task_file: str
    status: str
    evidence: str
    persistent_role: bool = False
    task_status: str = ""
    target: str = ""
    unstick: str = ""


@dataclass(frozen=True)
class TaskState:
    status: str
    target: str
    port: int | None
    persistent_role: bool = False
    reason: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    registry: Path = DEFAULT_REGISTRY
    prune_completed: bool = False
    exit_code_if_active: bool = False
    problems_only: bool = False
    manager_target: str = DEFAULT_MANAGER_TARGET
    auto_unstick: bool = True


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--prune-completed", action="store_true", help="Remove completed/previous tasks from sessions.json after writing a .bak.TIMESTAMP backup.")
    _ = parser.add_argument("--exit-code-if-active", action="store_true", help="Exit 3 when any task is still active, meaning not done or blocked.")
    _ = parser.add_argument("--problems-only", action="store_true", help="Print only active-agent problems and exit 3 when any are found.")
    _ = parser.add_argument("--manager-target", default=DEFAULT_MANAGER_TARGET, help="Optional manager Codex tmux target to include in problem checks.")
    _ = parser.add_argument("--no-auto-unstick", dest="auto_unstick", action="store_false", help="Report stuck input without sending Enter even when the pane looks safe to submit.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.root.resolve(), parsed.registry, parsed.prune_completed, parsed.exit_code_if_active, parsed.problems_only, parsed.manager_target, parsed.auto_unstick)


def target_aliases(target: str) -> set[str]:
    return {target, target[:-2] if target.endswith(".0") else f"{target}.0"} if target else set()


def same_tmux_target(left: str, right: str) -> bool:
    return bool(target_aliases(left) & target_aliases(right))


def is_vl_task_file(task_file: str) -> bool:
    name = Path(task_file).name
    return name.startswith("vl_") or "/vl_" in task_file


def task_line_is_vl(task: TaskLine) -> bool:
    return is_vl_task_file(task.task_file) or target_session(task.target) == "vl"


def is_current_vl_supervisor(task_file: str) -> bool:
    return Path(task_file).name.startswith("vl_supervisor_current_")


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def target_session(target: str) -> str:
    match = TARGET_SESSION_RE.match(target)
    return match.group(1) if match is not None else ""


def section_name(line: str, current: str) -> str:
    stripped = line.strip().lower().rstrip(":")
    if stripped in {"current", "previous", "human pending", "low priority"}:
        return stripped
    return current


def parse_task_lines(path: Path) -> list[TaskLine]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    section = ""
    tasks: list[TaskLine] = []
    for line in lines:
        section = section_name(line, section)
        matches = list(TASK_RE.finditer(line))
        if not matches:
            continue
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            segment = line[match.end() : next_start]
            target_match = TARGET_RE.search(segment)
            loose_target_match = LOOSE_TARGET_RE.search(segment) if target_match is None else None
            port_match = PORT_RE.search(segment)
            target = ""
            if target_match is not None:
                target = target_match.group(1)
            elif loose_target_match is not None:
                target = f"{loose_target_match.group(1)}:{loose_target_match.group(2)}"
            tasks.append(
                TaskLine(
                    task_file=match.group(1),
                    section=f"todo:{section}",
                    line=line.strip(),
                    target=target,
                    port=int(port_match.group(1)) if port_match else None,
                )
            )
    return tasks


def resolve_task_path(root: Path, task_file: str) -> Path | None:
    path = Path(task_file).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    try:
        root_resolved = root.resolve(strict=False)
    except OSError:
        root_resolved = root
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def scan_task_state(path: Path) -> TaskState | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    status = ""
    reason = ""
    target = ""
    port: int | None = None
    persistent_role = False
    persistent_role_note_seen = False
    for line in reversed(lines):
        stripped = line.strip()
        if not status:
            status_match = STATUS_DETAIL_RE.match(stripped)
            if status_match is not None:
                status = status_match.group(1)
                reason = (status_match.group(2) or status_match.group(3) or "").strip()
                persistent_role = persistent_role_note_seen or PERSISTENT_ROLE_RE.search(stripped.lower()) is not None
            elif stripped.startswith("(") and stripped.endswith(")") and PERSISTENT_ROLE_RE.search(stripped.lower()) is not None:
                persistent_role_note_seen = True
            else:
                persistent_role_note_seen = False
        if not target:
            runat_match = RUNAT_RE.match(stripped)
            if runat_match is not None:
                target = runat_match.group(1)
            else:
                close_target_match = CLOSE_TARGET_RE.search(stripped)
                if close_target_match is not None:
                    target = close_target_match.group(1)
        if port is None:
            port_match = PORT_RE.search(stripped)
            if port_match is not None:
                port = int(port_match.group(1))
        if status and target and port is not None:
            break
    return TaskState(status, target, port, persistent_role, reason) if status else None


def pending_task_items(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == PENDING_TASK_ITEMS_MARKER:
            return items
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
    return []


def pending_task_item_rows(root: Path) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in seen:
            continue
        if task.section not in {"todo:current", "todo:human pending", "todo:low priority"}:
            continue
        seen.add(task.task_file)
        state_path = resolve_task_path(root, task.task_file)
        if state_path is None:
            continue
        state = scan_task_state(state_path)
        if state is None or state.status in {"running", "pending", "blocked"}:
            continue
        for item in pending_task_items(state_path):
            rows.append(StatusRow(task.task_file, "human_request", f"pending_item={item}", task_status=state.status if state is not None else "missing"))
    return rows


def load_task_state(root: Path) -> tuple[dict[str, TaskLine], set[str], set[str]]:
    todo_tasks = parse_task_lines(root / "TODO.md")
    current: dict[str, TaskLine] = {}
    done: set[str] = set()
    human_pending: set[str] = set()
    for task in todo_tasks:
        if task.task_file == "TODO.md":
            continue
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path) if state_path is not None else None
        if state is None:
            continue
        if state.status == "done":
            done.add(task.task_file)
            continue
        if state.status == "blocked":
            human_pending.add(task.task_file)
            continue
        target = state.target or task.target
        port = state.port if state.port is not None else task.port
        current[task.task_file] = TaskLine(task.task_file, "task-file", task.line, target, port, state.status, state.persistent_role)
    return current, done, human_pending


def persistent_blocked_task_lines(root: Path) -> list[TaskLine]:
    tasks: list[TaskLine] = []
    seen: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in seen:
            continue
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path) if state_path is not None else None
        if state is None or state.status != "blocked" or not state.persistent_role:
            continue
        target = state.target or task.target
        port = state.port if state.port is not None else task.port
        tasks.append(TaskLine(task.task_file, "task-file", task.line, target, port, state.status, True))
        seen.add(task.task_file)
    return tasks


def current_untracked_task_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for task in parse_task_lines(args.root / "TODO.md"):
        if task.task_file == "TODO.md" or task.section != "todo:current" or not task.target:
            continue
        target = canonical_target(task.target)
        if target in seen_targets:
            continue
        state_path = resolve_task_path(args.root, task.task_file)
        state = scan_task_state(state_path) if state_path is not None else None
        if state is not None:
            continue
        row = classify_target(task.task_file, task.target, task_status="unlinked", auto_unstick=auto_unstick, role="todo_current_untracked", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        rows.append(row)
        seen_targets.add(target)
    return rows


def blocked_idle_vl_task_rows(root: Path) -> list[StatusRow]:
    todo_tasks = parse_task_lines(root / "TODO.md")
    candidates: list[TaskLine] = []
    for task in todo_tasks:
        if task.task_file == "TODO.md":
            continue
        if task.section == "todo:current" and task_line_is_vl(task):
            candidates.append(task)
        elif is_current_vl_supervisor(task.task_file):
            candidates.append(task)
    rows: list[StatusRow] = []
    seen: set[str] = set()
    index = {Path(task.task_file).name: task for task in todo_tasks}
    for task in candidates:
        add_blocked_idle_vl_row(root, task, "blocked_idle_vl", rows, seen)
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path) if state_path is not None else None
        if state is None or state.status != "blocked" or not is_current_vl_supervisor(task.task_file):
            continue
        for match in TASK_RE.finditer(state.reason):
            mentioned = index.get(Path(match.group(1)).name)
            if mentioned is not None:
                add_blocked_idle_vl_row(root, mentioned, "blocked_idle_vl_dependency", rows, seen)
    return rows


def add_blocked_idle_vl_row(root: Path, task: TaskLine, role: str, rows: list[StatusRow], seen: set[str]) -> None:
    state_path = resolve_task_path(root, task.task_file)
    state = scan_task_state(state_path) if state_path is not None else None
    if state is None or state.status != "blocked":
        return
    target = state.target or task.target
    seen_key = f"target:{canonical_target(target)}" if target else f"task:{task.task_file}"
    if seen_key in seen:
        return
    idle_status = "not_codex"
    if target:
        idle_status = classify_target(task.task_file, target, auto_unstick=False).status
        if idle_status == "running":
            return
    reason = state.reason or "blocked with no reason in latest status line"
    evidence = f"target={target} role={role} task_status=blocked idle_status={idle_status} reason={reason}"
    rows.append(StatusRow(task.task_file, "blocked_idle", evidence, state.persistent_role, state.status, target))
    seen.add(seen_key)


def session_records(registry: Path) -> list[SessionRecord]:
    raw_obj = read_json(registry, {"sessions": []}).get("sessions", [])
    raw: list[object] = cast(list[object], raw_obj) if isinstance(raw_obj, list) else []
    records: list[SessionRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        typed_item = cast(dict[str, object], item)
        try:
            raw_port = typed_item.get("port")
            port = int(raw_port) if isinstance(raw_port, (int, float, str)) else None
            raw_started = typed_item.get("started_at_s", 0.0)
            started_at_s = float(raw_started) if isinstance(raw_started, (int, float, str)) else 0.0
        except (TypeError, ValueError):
            port = None
            started_at_s = 0.0
        records.append(
            SessionRecord(
                task_file=str(typed_item.get("task_file", "")),
                target=str(typed_item.get("tmux_target", "")),
                port=port,
                started_at_s=started_at_s,
            )
        )
    return [record for record in records if record.task_file]


def choose_session(task: TaskLine, records: list[SessionRecord]) -> SessionRecord | None:
    matches = [record for record in records if record.task_file == task.task_file]
    if not matches:
        return None
    if task.target:
        target_matches = [record for record in matches if record.target == task.target or record.target.startswith(f"{task.target}.")]
        if target_matches:
            matches = target_matches
    return max(matches, key=lambda record: record.started_at_s)


def display_target(task: TaskLine, record: SessionRecord | None) -> str:
    if record is not None and record.target:
        return record.target
    return task.target


def classify_target(task_file: str, target: str, persistent_role: bool = False, task_status: str = "", auto_unstick: bool = False, role: str = "", unstick_by_target: dict[str, str] | None = None, auto_unstick_disabled_reason: str = "not_problems_only", report: Report | None = None) -> StatusRow:
    if not target:
        return StatusRow(task_file, "not_codex", "target=", persistent_role, task_status)
    report = report or inspect(StatusArgs(target, 80))
    evidence = f"target={target}"
    unstick = ""
    if role:
        evidence += f" role={role}"
    if persistent_role:
        evidence += " persistent_role=true"
    if task_status:
        evidence += f" task_status={task_status}"
    evidence += report_output_evidence(report)
    if report.status == "stuck_input" and persistent_role and task_status == "blocked" and is_stock_placeholder_input_text(report.input_text):
        return StatusRow(task_file, "ready", evidence, persistent_role, task_status, target)
    if report.status == "stuck_input":
        if auto_unstick:
            unstick_key = canonical_target(target)
            if unstick_by_target is not None and unstick_key in unstick_by_target:
                unstick = "already_sent" if unstick_by_target[unstick_key] == "sent_enter" else unstick_by_target[unstick_key]
            else:
                unstick = submit_stuck_input_if_present(target, report)
                if unstick_by_target is not None:
                    unstick_by_target[unstick_key] = unstick
        else:
            unstick = f"disabled:{auto_unstick_disabled_reason}"
        evidence += f" unstick={unstick}"
    return StatusRow(task_file, report.status, evidence, persistent_role, task_status, target, unstick)


def classify_task(task: TaskLine, record: SessionRecord | None, auto_unstick: bool = False, unstick_by_target: dict[str, str] | None = None, no_auto_unstick_target: str = "", auto_unstick_disabled_reason: str = "not_problems_only") -> StatusRow:
    target = display_target(task, record)
    return classify_target(task.task_file, target, task.persistent_role, task.status, auto_unstick, unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)


def registry_unmanaged_task(record: SessionRecord, root: Path) -> TaskLine:
    path = resolve_task_path(root, record.task_file)
    state = scan_task_state(path) if path is not None else None
    target = record.target or (state.target if state is not None else "")
    port = record.port if record.port is not None or state is None else state.port
    task_status = state.status if state is not None else "unlinked"
    return TaskLine(record.task_file, "registry-unmanaged", "", target, port, task_status, state.persistent_role if state is not None else False)


def registry_unmanaged_problem_rows(args: Args, records: list[SessionRecord], skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for record in records:
        target = canonical_target(record.target)
        if not target or target in seen_targets:
            continue
        task = registry_unmanaged_task(record, args.root)
        row = classify_target(task.task_file, task.target, task.persistent_role, task.status, auto_unstick, role="registry_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        if row.status in {"error", "not_codex", "stuck_input"}:
            rows.append(row)
            seen_targets.add(target)
    return rows


def todo_unmanaged_task(root: Path, task: TaskLine) -> TaskLine | None:
    path = resolve_task_path(root, task.task_file)
    state = scan_task_state(path) if path is not None else None
    target = task.target
    port = task.port
    task_status = "unlinked"
    persistent_role = False
    if state is not None:
        target = target or state.target
        port = port if port is not None else state.port
        task_status = state.status
        persistent_role = state.persistent_role
    if not target:
        return None
    return TaskLine(task.task_file, "todo-unmanaged", task.line, target, port, task_status, persistent_role)


def todo_unmanaged_problem_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for task in parse_task_lines(args.root / "TODO.md"):
        unmanaged = todo_unmanaged_task(args.root, task)
        if unmanaged is None:
            continue
        target = canonical_target(unmanaged.target)
        if not target or target in seen_targets:
            continue
        row = classify_target(unmanaged.task_file, unmanaged.target, unmanaged.persistent_role, unmanaged.status, auto_unstick, role="todo_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        if row.status in {"error", "stuck_input"} or (row.status == "not_codex" and " output=" in row.evidence):
            rows.append(row)
            seen_targets.add(target)
    return rows


def tmux_list_panes() -> list[str]:
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [canonical_target(line.strip()) for line in out.stdout.splitlines() if line.strip()]


def unmanaged_session_names(root: Path) -> set[str]:
    names: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file.startswith("vl_") or "/vl_" in task.task_file:
            names.add("vl")
    return names


def tmux_unmanaged_problem_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    session_names = unmanaged_session_names(args.root)
    if not session_names:
        return []
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for target in tmux_list_panes():
        if target in seen_targets or target_session(target) not in session_names:
            continue
        row = classify_target(f"tmux:{target}", target, auto_unstick=auto_unstick, role="tmux_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        if row.status in {"error", "stuck_input"}:
            rows.append(row)
            seen_targets.add(target)
    return rows


def manager_problem_row(args: Args, skip_targets: set[str], unstick_by_target: dict[str, str]) -> StatusRow | None:
    if not args.manager_target:
        return None
    report = inspect(StatusArgs(args.manager_target, 80))
    evidence = f"target={args.manager_target} role=manager" + report_output_evidence(report)
    if manager_compaction_needs_reread(report):
        return StatusRow("manager", "manager_compaction", evidence, target=args.manager_target)
    if any(same_tmux_target(args.manager_target, target) for target in skip_targets):
        return None
    disabled_reason = "no_auto_unstick" if not args.auto_unstick else "not_problems_only"
    row = classify_target("manager", args.manager_target, auto_unstick=args.auto_unstick, role="manager", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=disabled_reason, report=report)
    return row if row.status in {"error", "not_codex", "stuck_input"} else None

def registry_prune(args: Args, completed: set[str]) -> int:
    if not completed or not args.registry.exists():
        return 0
    data = read_json(args.registry, {"sessions": []})
    raw_sessions_obj = data.get("sessions", [])
    if not isinstance(raw_sessions_obj, list):
        return 0
    raw_sessions = cast(list[object], raw_sessions_obj)
    kept: list[object] = []
    for item in raw_sessions:
        if isinstance(item, dict):
            typed_item = cast(dict[str, object], item)
            if str(typed_item.get("task_file", "")) in completed:
                continue
        kept.append(cast(object, item))
    removed = len(raw_sessions) - len(kept)
    if removed <= 0:
        return 0
    backup = args.registry.with_name(f"{args.registry.name}.bak")
    _ = shutil.copy2(args.registry, backup)
    data["sessions"] = kept
    write_json_private(args.registry, data)
    return removed


def format_summary(rows: list[StatusRow], completed_stale_count: int, pruned_count: int) -> str:
    counts: dict[str, int] = {"not_codex": 0, "running": 0, "blocked_idle": 0, "error": 0, "ready": 0, "stuck_input": 0, "human_request": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = [
        f"agent-status: not_codex={counts['not_codex']} running={counts['running']} blocked_idle={counts['blocked_idle']} error={counts['error']} ready={counts['ready']} stuck_input={counts['stuck_input']} human_request={counts['human_request']} done-registry-stale={completed_stale_count} pruned={pruned_count}",
    ]
    for row in sorted(rows, key=lambda item: (item.status != "error", item.status, item.task_file)):
        lines.append(f"{row.status}: task={row.task_file} evidence={row.evidence}")
    return "\n".join(lines)


PROBLEM_STATUSES = {"blocked_idle", "error", "human_request", "manager_compaction", "not_codex", "ready", "stuck_input"}


def is_parked_persistent_blocked_row(row: StatusRow) -> bool:
    return row.persistent_role and row.task_status == "blocked" and row.status == "ready"


def format_problem_summary(rows: list[StatusRow], completed_stale: set[str]) -> str:
    problem_rows = [row for row in rows if row.status in PROBLEM_STATUSES and not is_parked_persistent_blocked_row(row)]
    if not problem_rows and not completed_stale:
        return ""
    counts: dict[str, int] = {"not_codex": 0, "blocked_idle": 0, "error": 0, "human_request": 0, "manager_compaction": 0, "ready": 0, "stuck_input": 0}
    for row in problem_rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    parts = [f"{status}={counts[status]}" for status in ("not_codex", "blocked_idle", "error", "human_request", "manager_compaction", "ready", "stuck_input") if counts[status]]
    if completed_stale:
        parts.append(f"done-registry-stale={len(completed_stale)}")
    lines = [f"agent-problems: {' '.join(parts)}"]
    if counts["blocked_idle"]:
        lines.append("manager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker")
    if counts["manager_compaction"]:
        lines.append("manager-action: manager_compaction>0 reread MANAGER.md after compaction unless the compaction summary already included it")
    for row in sorted(problem_rows, key=lambda item: (item.status, item.task_file)):
        lines.append(f"{row.status}: task={row.task_file} evidence={row.evidence}")
    unstuck: dict[str, str] = {}
    for row in problem_rows:
        if row.unstick == "sent_enter" and row.target:
            unstuck.setdefault(row.target, row.task_file)
    for target, task_file in sorted(unstuck.items()):
        lines.append(f"unstuck: target={target} task={task_file} action=sent_enter")
    for task_file in sorted(completed_stale):
        lines.append(f"done-stale: task={task_file} evidence=session registry still has a completed task")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        current, done, _human_pending = load_task_state(args.root)
        records = session_records(args.registry)
        tasks = list(current.values())
        if args.problems_only:
            tasks = [task for task in tasks if task.status == "running"]
        auto_unstick = args.problems_only and args.auto_unstick
        auto_unstick_disabled_reason = "no_auto_unstick" if args.problems_only and not args.auto_unstick else "not_problems_only"
        unstick_by_target: dict[str, str] = {}
        rows = [classify_task(task, choose_session(task, records), auto_unstick, unstick_by_target, args.manager_target, auto_unstick_disabled_reason) for task in tasks]
        inspected_targets = {display_target(task, choose_session(task, records)) for task in tasks}
        untracked_rows = current_untracked_task_rows(args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
        rows.extend(untracked_rows)
        inspected_targets.update(row.target for row in untracked_rows if row.target)
        if args.problems_only:
            rows.extend(pending_task_item_rows(args.root))
            blocked_idle_rows = blocked_idle_vl_task_rows(args.root)
            rows.extend(blocked_idle_rows)
            inspected_targets.update(row.target for row in blocked_idle_rows if row.target)
            standby_tasks = persistent_blocked_task_lines(args.root)
            rows.extend(classify_task(task, choose_session(task, records), auto_unstick, unstick_by_target, args.manager_target, auto_unstick_disabled_reason) for task in standby_tasks)
            inspected_tasks = [*tasks, *standby_tasks]
            inspected_targets.update(display_target(task, choose_session(task, records)) for task in inspected_tasks)
            unmanaged_rows = registry_unmanaged_problem_rows(args, records, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(unmanaged_rows)
            inspected_targets.update(row.target for row in unmanaged_rows if row.target)
            todo_rows = todo_unmanaged_problem_rows(args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(todo_rows)
            inspected_targets.update(row.target for row in todo_rows if row.target)
            tmux_rows = tmux_unmanaged_problem_rows(args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(tmux_rows)
            inspected_targets.update(row.target for row in tmux_rows if row.target)
            manager_row = manager_problem_row(args, inspected_targets, unstick_by_target)
            if manager_row is not None:
                rows.append(manager_row)
        else:
            rows.extend(blocked_idle_vl_task_rows(args.root))
        completed_stale = {record.task_file for record in records if record.task_file in done}
        pruned_count = registry_prune(args, completed_stale) if args.prune_completed else 0
        if args.problems_only:
            text = format_problem_summary(rows, completed_stale)
            if not text:
                return 0
            print(text)
            return 3
        print(format_summary(rows, len(completed_stale), pruned_count))
        if args.exit_code_if_active and any(row.status != "blocked_idle" for row in rows):
            return 3
    except Exception as exc:
        print(f"omo_agent_status: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
