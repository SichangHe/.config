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
from omo_manager.omo_codex_status import inspect
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
TASK_RE = re.compile(r"`?([A-Za-z0-9_./-]+\.md)`?")
TARGET_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
LOOSE_TARGET_RE = re.compile(r"\b([a-z][A-Za-z0-9_-]*)\s+(\d+)\b")
PORT_RE = re.compile(r"\bport [`']?(\d{2,5})[`']?")
STATUS_RE = re.compile(r"^\((pending|running|done|blocked)(?::[^)]*)?\)$")
RUNAT_RE = re.compile(r"^runat:\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
CLOSE_TARGET_RE = re.compile(r"\btmux target [`']?([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)[`']?")


@dataclass(frozen=True)
class Args:
    root: Path
    registry: Path
    prune_completed: bool


@dataclass(frozen=True)
class TaskLine:
    task_file: str
    section: str
    line: str
    target: str
    port: int | None


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


@dataclass(frozen=True)
class TaskState:
    status: str
    target: str
    port: int | None


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    registry: Path = DEFAULT_REGISTRY
    prune_completed: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--prune-completed", action="store_true", help="Remove completed/previous tasks from sessions.json after writing a .bak.TIMESTAMP backup.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.root.resolve(), parsed.registry, parsed.prune_completed)


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
    target = ""
    port: int | None = None
    for line in reversed(lines):
        stripped = line.strip()
        if not status:
            status_match = STATUS_RE.match(stripped)
            if status_match is not None:
                status = status_match.group(1)
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
    return TaskState(status, target, port) if status else None


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
        current[task.task_file] = TaskLine(task.task_file, "task-file", task.line, target, port)
    return current, done, human_pending


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


def classify_task(task: TaskLine, record: SessionRecord | None) -> StatusRow:
    target = display_target(task, record)
    if not target:
        return StatusRow(task.task_file, "not_codex", "target=")
    report = inspect(StatusArgs(target, 80))
    evidence = f"target={target}"
    if report.lines:
        evidence += " output=" + " / ".join(report.lines[-3:])
    return StatusRow(task.task_file, report.status, evidence)

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
    counts: dict[str, int] = {"not_codex": 0, "running": 0, "error": 0, "ready": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = [
        f"agent-status: not_codex={counts['not_codex']} running={counts['running']} error={counts['error']} ready={counts['ready']} done-registry-stale={completed_stale_count} pruned={pruned_count}",
    ]
    for row in sorted(rows, key=lambda item: (item.status != "error", item.status, item.task_file)):
        lines.append(f"{row.status}: task={row.task_file} evidence={row.evidence}")
    return "\n".join(lines)

def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        current, done, _human_pending = load_task_state(args.root)
        records = session_records(args.registry)
        rows = [classify_task(task, choose_session(task, records)) for task in current.values()]
        completed_stale = {record.task_file for record in records if record.task_file in done}
        pruned_count = registry_prune(args, completed_stale) if args.prune_completed else 0
        print(format_summary(rows, len(completed_stale), pruned_count))
    except Exception as exc:
        print(f"omo_agent_status: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
