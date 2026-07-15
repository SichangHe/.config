#!/usr/bin/env python3
"""Create/link a markdown task and optionally start a Codex tmux window."""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from omo_manager.omo_codex_status import current_block, status, tail
    from omo_manager.omo_agent_status import parse_task_metadata
except ModuleNotFoundError:
    from omo_codex_status import current_block, status, tail
    from omo_agent_status import parse_task_metadata

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
HELPER_DIR = Path(__file__).resolve().parent
DEFAULT_WORKER_INSTRUCTIONS = HELPER_DIR / "WORKER_DEFAULTS.md"
VL_WORKER_INSTRUCTIONS = HELPER_DIR / "VL_WORKER_DEFAULTS.md"
PCODX_WRAPPER = HELPER_DIR / "pcodx"
COMMAND_BY_TOOL = {
    "codex": ("bunx", "@openai/codex", "--dangerously-bypass-approvals-and-sandbox"),
    "pcodx": (str(PCODX_WRAPPER),),
}
DEFAULT_TOOL = "codex"
TASK_FRONTMATTER_VERSION = "v1.0.0"
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
BULLET_MARKERS = ("- ", "* ")
PENDING_TASK_ITEMS_MARKER = "(above are pending task items)"
TASK_METADATA_PREFIXES = ("managerat:",)
CODEX_LAUNCH_STARTED = "started"
CODEX_LAUNCH_UPDATED = "updated"
CODEX_LAUNCH_MARKER_PREFIX = "[omo:"
CODEX_LAUNCH_MARKER_DRY_RUN = f"{CODEX_LAUNCH_MARKER_PREFIX}DRY]"
CODEX_UPDATE_PROMPT_MARKERS = ("update available!", "update now", "press enter to continue")
CODEX_UPDATE_SUCCESS_MARKERS = ("update ran successfully", "please restart codex")


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    tmux_session: str
    tmux_window: str
    tool: str
    workdir: Path | None
    window_name: str
    prompt_file: Path | None
    no_link: bool
    dry_run: bool
    session_id: str
    reasoning_effort: str
    codex_flags: tuple[str, ...]
    tool_explicit: bool = False
    manager_target: str = ""
    prelaunch_source: Path | None = None
    is_manager: bool = False
    migrate_manager_owner: bool = False
    old_manager_target: str = ""
    new_manager_target: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    tmux_session: str = ""
    tmux_window: str = ""
    tool: str = DEFAULT_TOOL
    workdir: Path | None = None
    window_name: str = ""
    prompt_file: Path | None = None
    no_link: bool = False
    dry_run: bool = False
    session_id: str = ""
    reasoning_effort: str = ""
    codex_flag: list[str] | None = None
    manager_target: str = ""
    prelaunch_source: Path | None = None
    is_manager: bool = False
    migrate_manager_owner: bool = False
    old_manager_target: str = ""
    new_manager_target: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Ownership migration: omo_task.py --root ROOT --task-file TASK.md --migrate-manager-owner --old-manager-target OLD --new-manager-target NEW [--dry-run]"),
    )
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--tmux-session", default="")
    _ = parser.add_argument("--tmux-window", default="")
    _ = parser.add_argument("--pane", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--tool", default=DEFAULT_TOOL)
    _ = parser.add_argument("--workdir", type=Path)
    _ = parser.add_argument("--window-name", default="")
    _ = parser.add_argument("--prompt-file", type=Path)
    _ = parser.add_argument("--no-link", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true", help="Print the planned launch or ownership migration without changing files or tmux.")
    _ = parser.add_argument("--session-id", default="", help="Codex session id to resume in a new worker window.")
    _ = parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), default="", help="Start Codex with `model_reasoning_effort` for this worker.")
    _ = parser.add_argument("--codex-flag", action="append", help="Extra raw Codex argv token. Repeat for flags and values; use `--codex-flag=--flag` when the token starts with `--`.")
    _ = parser.add_argument("--manager-target", default="", help="Optional manager owner target to write as `managerat:` task metadata.")
    _ = parser.add_argument("--prelaunch-source", type=Path, help="Readable shell script to source before launching the worker command.")
    _ = parser.add_argument("--is-manager", action="store_true", help="Mark the task as a manager task in frontmatter.")
    _ = parser.add_argument(
        "--migrate-manager-owner", action="store_true", help="Atomically migrate only `managerat` on one existing task; requires explicit old and new targets and performs no launch or TODO action."
    )
    _ = parser.add_argument("--old-manager-target", default="", help="Existing `managerat` value required by --migrate-manager-owner.")
    _ = parser.add_argument("--new-manager-target", default="", help="Replacement `managerat` value required by --migrate-manager-owner.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.pane:
        parser.error("pane selection is no longer supported; pane 0 is implied.")
    if parsed.tool not in COMMAND_BY_TOOL:
        parser.error("only --tool codex or --tool pcodx is supported.")
    tool_explicit = any(arg == "--tool" or arg.startswith("--tool=") for arg in argv)
    if not parsed.migrate_manager_owner and (parsed.old_manager_target or parsed.new_manager_target):
        parser.error("--old-manager-target and --new-manager-target require --migrate-manager-owner.")
    if parsed.migrate_manager_owner:
        if not parsed.old_manager_target or not parsed.new_manager_target:
            parser.error("--migrate-manager-owner requires --old-manager-target OLD and --new-manager-target NEW.")
        if any(
            (
                parsed.tmux_session,
                parsed.tmux_window,
                parsed.workdir,
                parsed.window_name,
                parsed.prompt_file,
                parsed.no_link,
                parsed.session_id,
                parsed.reasoning_effort,
                parsed.codex_flag,
                tool_explicit,
                parsed.manager_target,
                parsed.prelaunch_source,
                parsed.is_manager,
            )
        ):
            parser.error("--migrate-manager-owner only accepts --root, --task-file, explicit old/new manager targets, and optional --dry-run.")
    if parsed.workdir is not None and not parsed.tmux_session:
        parser.error("--workdir requires --tmux-session.")
    prelaunch_source = parsed.prelaunch_source.resolve() if parsed.prelaunch_source is not None else None
    return Args(
        parsed.root.resolve(),
        parsed.task_file,
        parsed.tmux_session,
        parsed.tmux_window,
        parsed.tool,
        parsed.workdir,
        parsed.window_name,
        parsed.prompt_file,
        parsed.no_link,
        parsed.dry_run,
        parsed.session_id,
        parsed.reasoning_effort,
        tuple(parsed.codex_flag or ()),
        tool_explicit,
        parsed.manager_target,
        prelaunch_source,
        parsed.is_manager,
        parsed.migrate_manager_owner,
        parsed.old_manager_target,
        parsed.new_manager_target,
    )


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("task file escapes root")
    return path


def task_ref(root: Path, task_file: str) -> str:
    return task_path(root, task_file).relative_to(root.resolve()).as_posix()


def canonical_tmux_pane(tmux_target: str) -> tuple[str, int, int]:
    session, window_and_pane = tmux_target.split(":", 1)
    window, dot, pane = window_and_pane.partition(".")
    return session, int(window), int(pane) if dot else 0


def manager_owner_migration_text(text: str, old_owner: str, new_owner: str) -> str:
    """Return valid task text with only the exact frontmatter owner value changed."""
    for label, owner in (("old", old_owner), ("new", new_owner)):
        if TMUX_TARGET_RE.fullmatch(owner) is None:
            raise ValueError(f"{label} manager target must be a full tmux target like `SESSION:WINDOW`.")
    if canonical_tmux_pane(old_owner) == canonical_tmux_pane(new_owner):
        raise ValueError("old and new manager targets must identify different tmux panes.")
    metadata = parse_task_metadata(text)
    if metadata is None:
        raise ValueError("ownership migration requires an existing task with valid frontmatter.")
    if metadata.managerat != old_owner:
        raise ValueError(f"existing managerat {metadata.managerat} does not equal --old-manager-target {old_owner}.")
    if metadata.runat != "retired" and canonical_tmux_pane(new_owner) == canonical_tmux_pane(metadata.runat):
        raise ValueError("new manager target must be different from task `runat`.")

    lines = text.splitlines(keepends=True)
    closing_idx = next(idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    owner_indexes = [idx for idx, line in enumerate(lines[1:closing_idx], start=1) if line.rstrip("\r\n").partition(":")[0] == "managerat"]
    if len(owner_indexes) != 1:
        raise ValueError("ownership migration requires exactly one frontmatter `managerat` field.")
    owner_idx = owner_indexes[0]
    line = lines[owner_idx]
    content = line.rstrip("\r\n")
    line_ending = line[len(content) :]
    key, separator, value = content.partition(":")
    value_start = len(value) - len(value.lstrip())
    value_end = len(value.rstrip())
    lines[owner_idx] = f"{key}{separator}{value[:value_start]}{new_owner}{value[value_end:]}{line_ending}"
    updated = "".join(lines)
    updated_metadata = parse_task_metadata(updated)
    if updated_metadata is None or updated_metadata.managerat != new_owner:
        raise RuntimeError("updated task frontmatter did not retain the requested manager owner.")
    return updated


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mtime_ns == right.st_mtime_ns and left.st_size == right.st_size


def same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def atomic_replace_if_unchanged(path: Path, text: str, before: os.stat_result) -> None:
    """Atomically replace `path` only if it still matches the state that was read."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            tmp_path = Path(handle.name)
            _ = handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(before.st_mode & 0o7777)
        if not same_file_state(before, path.stat()):
            raise ValueError("task file changed while ownership migration was being prepared; retry after rereading it.")
        os.replace(tmp_path, path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def migrate_manager_owner(path: Path, old_owner: str, new_owner: str, dry_run_only: bool = False) -> None:
    """Validate and migrate one existing task owner without touching other state."""
    if not path.is_file():
        raise ValueError(f"ownership migration requires an existing task file: {path}")
    while True:
        with path.open("r", encoding="utf-8", newline="") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            before = os.fstat(handle.fileno())
            if not same_file_identity(before, path.stat()):
                continue
            existing = handle.read()
            updated = manager_owner_migration_text(existing, old_owner, new_owner)
            if dry_run_only:
                print(f"dry-run: would change only managerat from {old_owner} to {new_owner} in {path}; no files or tmux panes changed.")
                return
            atomic_replace_if_unchanged(path, updated, before)
            print(f"migrated only managerat from {old_owner} to {new_owner} in {path}")
            return


def target(args: Args) -> str:
    if args.tmux_session and args.tmux_window:
        return f"{args.tmux_session}:{args.tmux_window}"
    return args.tmux_session


def current_manager_target() -> str:
    for key in ("OMO_MANAGER_TMUX_TARGET", "OMO_AGENT_TMUX_TARGET"):
        target = os.environ.get(key, "").strip()
        if TMUX_TARGET_RE.fullmatch(target) is not None:
            return target
    if "TMUX" not in os.environ:
        return ""
    result = subprocess.run(["tmux", "display-message", "-p", "#S:#I"], capture_output=True, text=True, timeout=10, check=False)
    target = result.stdout.strip() if result.returncode == 0 else ""
    return target if TMUX_TARGET_RE.fullmatch(target) is not None else ""


def frontmatter_body_line_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return idx + 1
    return 0


def target_session(tmux_target: str) -> str:
    return tmux_target.split(":", 1)[0]


def is_vl_task_file(task_file: str) -> bool:
    return Path(task_file).name.startswith("vl_")


def is_vl_submanager_task_file(task_file: str) -> bool:
    name = Path(task_file).name
    return name.startswith("vl_submanager_current_") or name.startswith("vl_supervisor_current_")


def is_vl_agent(task_file: str, tmux_target: str) -> bool:
    return is_vl_task_file(task_file) or target_session(tmux_target) == "vl"


def header(tmux_target: str, tool: str) -> str:
    return f"runat: {tmux_target} {tool}" if tmux_target else ""


def target_aliases(tmux_target: str) -> set[str]:
    aliases = {tmux_target} if tmux_target else set()
    window_target, dot, _pane = tmux_target.rpartition(".")
    if dot and ":" in window_target:
        aliases.add(window_target)
    elif tmux_target and not dot:
        aliases.add(f"{tmux_target}.0")
    return aliases


def upsert_header(existing: str, first: str) -> str:
    if not first:
        return existing
    if not existing:
        return f"{first}\n"
    lines = existing.splitlines(keepends=True)
    if lines and is_runat_header(lines[0]):
        lines[0] = f"{first}\n"
        return "".join(lines)
    return f"{first}\n\n{existing}"


def first_non_metadata_index(lines: list[str]) -> int:
    idx = frontmatter_body_line_index(lines)
    if idx < len(lines) and is_runat_header(lines[idx]):
        idx += 1
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if any(stripped.startswith(prefix) for prefix in TASK_METADATA_PREFIXES):
            idx += 1
            continue
        break
    return idx


def managerat_line_error(line: str) -> str:
    stripped = line.strip()
    parts = stripped.split()
    if stripped.startswith("managerat:") and (len(parts) != 2 or parts[0] != "managerat:"):
        return "task `managerat:` metadata must be exactly `managerat: TARGET`."
    return ""


def managerat_value(text: str) -> str:
    lines = text.splitlines()
    for line in lines[:first_non_metadata_index(lines)]:
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "managerat:":
            return parts[1]
    return ""


def validate_managerat_metadata(text: str) -> None:
    lines = text.splitlines()
    for line in lines[:first_non_metadata_index(lines)]:
        if error := managerat_line_error(line):
            raise ValueError(error)


def upsert_managerat(text: str, manager_target: str) -> str:
    if not manager_target:
        return text
    lines = text.splitlines(keepends=True)
    metadata_end = first_non_metadata_index([line.rstrip("\n") for line in lines])
    for idx, line in enumerate(lines[:metadata_end]):
        if error := managerat_line_error(line):
            raise ValueError(error)
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "managerat:":
            lines[idx] = f"managerat: {manager_target}\n"
            return "".join(lines)
    insert_at = 1 if lines and is_runat_header(lines[0]) else 0
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] = f"{lines[insert_at - 1]}\n"
    lines.insert(insert_at, f"managerat: {manager_target}\n")
    return "".join(lines)


def is_bullet(line: str) -> bool:
    stripped = line.lstrip()
    return any(stripped.startswith(marker) for marker in BULLET_MARKERS)


def is_runat_header(line: str) -> bool:
    return line.strip().split(maxsplit=1)[0:1] == ["runat:"]


def runat_header_error(text: str) -> str:
    lines = text.splitlines()
    if not lines or not is_runat_header(lines[0]):
        return ""
    parts = lines[0].strip().split()
    if len(parts) != 3 or parts[2] not in COMMAND_BY_TOOL:
        return "task files starting with `runat:` must keep the first line exactly `runat: TARGET TOOL`."
    return ""


def validate_runat_header(text: str) -> None:
    if error := runat_header_error(text):
        raise ValueError(error)


def has_pending_task_items_marker(text: str) -> bool:
    return any(line.strip() == PENDING_TASK_ITEMS_MARKER for line in text.splitlines())


def insert_pending_task_items_marker(text: str) -> str:
    if has_pending_task_items_marker(text):
        return text

    lines = text.splitlines(keepends=True)
    goal_idx = first_goal_line_index([line.rstrip("\n") for line in lines])
    insert_idx = min(goal_idx + 1, len(lines))
    while insert_idx < len(lines) and is_bullet(lines[insert_idx]):
        insert_idx += 1
    lines.insert(insert_idx, f"{PENDING_TASK_ITEMS_MARKER}\n")
    return "".join(lines)


def first_goal_line_index(lines: list[str]) -> int:
    idx = frontmatter_body_line_index(lines)
    if idx < len(lines) and is_runat_header(lines[idx]):
        idx += 1
    while idx < len(lines) and any(lines[idx].strip().startswith(prefix) for prefix in TASK_METADATA_PREFIXES):
        idx += 1
    return idx


def runat_goal_tree_error(text: str) -> str:
    lines = text.splitlines()
    if not lines or (frontmatter_body_line_index(lines) == 0 and not is_runat_header(lines[0])):
        return ""
    goal_idx = first_goal_line_index(lines)
    if len(lines) <= goal_idx or not lines[goal_idx].strip():
        return "task files starting with `runat:` must put a high-level goal directly after the `runat:` line."
    if is_bullet(lines[goal_idx]):
        return "task files starting with `runat:` must use a plain high-level goal line before bullet subgoals."
    subgoal_idx = goal_idx + 1
    while subgoal_idx < len(lines) and not lines[subgoal_idx].strip():
        subgoal_idx += 1
    if len(lines) <= subgoal_idx or not is_bullet(lines[subgoal_idx]):
        return "task files starting with `runat:` must put at least one concrete bullet subgoal directly under the high-level goal."
    return ""


def validate_runat_goal_tree(text: str) -> None:
    if error := runat_goal_tree_error(text):
        raise ValueError(error)


def top_header_tool(text: str) -> str:
    first = text.splitlines()[0].strip().split() if text.splitlines() else []
    if len(first) >= 3 and first[0] == "runat:" and first[-1] in COMMAND_BY_TOOL:
        return first[-1]
    return ""


def runat_entries(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "runat:" and parts[-1] in COMMAND_BY_TOOL:
            entries.append((parts[1], parts[-1]))
    return entries


def task_file_tool(text: str, tmux_target: str) -> str:
    if tool := top_header_tool(text):
        return tool
    entries = runat_entries(text)
    aliases = target_aliases(tmux_target)
    for entry_target, tool in reversed(entries):
        if entry_target in aliases:
            return tool
    if entries:
        return entries[-1][1]
    return ""


def effective_tool(args: Args) -> str:
    return args.tool


def managerat_for_task(args: Args, runat: str) -> str:
    managerat = args.manager_target.strip() or current_manager_target()
    if not managerat:
        raise ValueError("--manager-target or OMO_AGENT_TMUX_TARGET is required to write task frontmatter.")
    if TMUX_TARGET_RE.fullmatch(managerat) is None:
        raise ValueError("task frontmatter `managerat` must be a tmux target.")
    if managerat in target_aliases(runat):
        raise ValueError("task frontmatter `managerat` must be different from `runat`.")
    return managerat


def task_frontmatter(args: Args, runat: str, managerat: str) -> str:
    is_manager = "true" if args.is_manager else "false"
    return "\n".join(
        [
            "---",
            f"version: {TASK_FRONTMATTER_VERSION}",
            "status: running",
            f"runat: {runat}",
            f"tool: {effective_tool(args)}",
            f"managerat: {managerat}",
            f"is_manager: {is_manager}",
            "pending_task_items: []",
            "---",
        ]
    )


def replace_frontmatter_fields(text: str, updates: dict[str, str], remove: set[str] | None = None) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    remove = remove or set()
    for closing_idx, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        body = lines[1:closing_idx]
        updated: list[str] = [lines[0]]
        pending_updates = dict(updates)
        for item in body:
            key, sep, _value = item.partition(":")
            if sep and key in remove:
                continue
            if sep and key in pending_updates:
                updated.append(f"{key}: {pending_updates.pop(key)}\n")
                continue
            updated.append(item)
        for key, value in pending_updates.items():
            updated.append(f"{key}: {value}\n")
        updated.extend(lines[closing_idx:])
        return "".join(updated)
    return text


def launched_frontmatter_text(existing: str, args: Args, tmux_target: str) -> str:
    updates = {
        "status": "running",
        "runat": tmux_target,
        "tool": effective_tool(args),
    }
    if args.manager_target:
        updates["managerat"] = args.manager_target
    if args.is_manager:
        updates["is_manager"] = "true"
    return replace_frontmatter_fields(existing, updates, {"blocked_on"})


def new_task_text(args: Args, tmux_target: str, validate_target: bool = True) -> str:
    if not tmux_target:
        raise ValueError("runat tmux target is required to write task frontmatter.")
    if validate_target and TMUX_TARGET_RE.fullmatch(tmux_target) is None:
        raise ValueError("runat tmux target must be a full tmux target like `SESSION:WINDOW`.")
    prompt = args.prompt_file.read_text(encoding="utf-8").rstrip() if args.prompt_file is not None else ""
    managerat = managerat_for_task(args, tmux_target)
    return f"{task_frontmatter(args, tmux_target, managerat)}\n{prompt}\n"


def prompt_input(prompt_file: Path | None, vl_agent: bool = False) -> str:
    if prompt_file is None:
        return ""
    if not DEFAULT_WORKER_INSTRUCTIONS.is_file():
        raise FileNotFoundError(f"worker defaults file not found: {DEFAULT_WORKER_INSTRUCTIONS}")
    paths = [DEFAULT_WORKER_INSTRUCTIONS]
    if vl_agent:
        if not VL_WORKER_INSTRUCTIONS.is_file():
            raise FileNotFoundError(f"VL worker defaults file not found: {VL_WORKER_INSTRUCTIONS}")
        paths.append(VL_WORKER_INSTRUCTIONS)
    if prompt_file is not None:
        paths.append(prompt_file)
    quoted_paths = " ".join(shlex.quote(str(path)) for path in paths)
    return f"\"$(cat -- {quoted_paths})\""


def codex_cmd(session_id: str = "", reasoning_effort: str = "", codex_flags: tuple[str, ...] = (), prompt_file: Path | None = None, tool: str = DEFAULT_TOOL, vl_agent: bool = False) -> str:
    try:
        args = list(COMMAND_BY_TOOL[tool])
    except KeyError as exc:
        raise ValueError(f"unsupported tool: {tool}") from exc
    if reasoning_effort:
        args.extend(("--config", f'model_reasoning_effort="{reasoning_effort}"'))
    args.extend(codex_flags)
    if session_id:
        args.extend(("resume", session_id))
    parts = [shlex.quote(arg) for arg in args]
    prompt = prompt_input(prompt_file, vl_agent)
    if prompt:
        parts.append(prompt)
    return " ".join(parts)


def shell_cmd(command: str) -> str:
    return "bash -lc " + shlex.quote(command)


def worker_command(command: str, tmux_target: str, prelaunch_source: Path | None = None, launch_marker: str = "") -> str:
    exports = {"OMO_AGENT_TMUX_TARGET": tmux_target}
    export_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in exports.items())
    marker = f" && printf '%s\\n' {shlex.quote(launch_marker)}" if launch_marker else ""
    launch = f"export {export_text}{marker} && exec {command}"
    if prelaunch_source is None:
        return launch
    return f"source {shlex.quote(str(prelaunch_source))} && {launch}"


def new_launch_marker() -> str:
    return f"{CODEX_LAUNCH_MARKER_PREFIX}{uuid.uuid4().hex[:6]}]"


def tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10, check=check)


def current_command(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{pane_current_command}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def wait_shell(target: str, timeout_s: float = 5.0) -> None:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        if current_command(target) in SHELL_COMMANDS:
            return
        time.sleep(0.25)
    raise RuntimeError(f"tmux target {target} did not return to shell after {timeout_s:g}s.")


def lines_after_launch_marker(lines: list[str], launch_marker: str) -> list[str] | None:
    if not launch_marker:
        return lines
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() == launch_marker:
            return lines[idx + 1 :]
    return None


def has_codex_update_prompt(lines: list[str]) -> bool:
    text = "\n".join(lines).casefold()
    return all(marker in text for marker in CODEX_UPDATE_PROMPT_MARKERS)


def has_codex_update_success(lines: list[str]) -> bool:
    text = "\n".join(lines).casefold()
    return all(marker in text for marker in CODEX_UPDATE_SUCCESS_MARKERS)


def wait_codex_update_finished(target: str, launch_marker: str, timeout_s: float = 120.0) -> str:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        lines = lines_after_launch_marker(tail(target, 200), launch_marker)
        if lines is not None and has_codex_update_success(lines):
            return CODEX_LAUNCH_UPDATED
        time.sleep(0.25)
    raise RuntimeError(f"Codex update did not finish after {timeout_s:g}s.")


def wait_command_started(target: str, timeout_s: float = 5.0, launch_marker: str = "") -> str:
    deadline_s = time.monotonic() + timeout_s
    last_command = ""
    last_status = "unknown"
    saw_non_shell = False
    while time.monotonic() < deadline_s:
        lines = lines_after_launch_marker(tail(target, 200), launch_marker)
        if lines is None:
            last_status = "launch marker not visible"
        else:
            if has_codex_update_prompt(lines):
                _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
                return wait_codex_update_finished(target, launch_marker)
            last_status = status(lines, current_block(lines))
            if last_status != "not_codex":
                return CODEX_LAUNCH_STARTED
        last_command = current_command(target)
        if last_command and last_command not in SHELL_COMMANDS:
            saw_non_shell = True
        time.sleep(0.05)
    if saw_non_shell:
        return CODEX_LAUNCH_STARTED
    raise RuntimeError(f"Codex launch not verified after {timeout_s:g}s: pane command={last_command or 'unknown'}, status={last_status}")


def new_window_command(args: Args) -> list[str]:
    name = args.window_name or Path(args.task_file).stem
    return ["new-window", "-P", "-F", "#{session_name}:#{window_index}", "-t", args.tmux_session, "-n", name, "-c", str(args.workdir)]


def start_codex(target: str, args: Args) -> None:
    vl_agent = is_vl_agent(args.task_file, target)
    if vl_agent and args.prompt_file is None:
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    command = codex_cmd(args.session_id, args.reasoning_effort, args.codex_flags, args.prompt_file, effective_tool(args), vl_agent)
    for attempt in range(2):
        launch_marker = new_launch_marker()
        shell_launch = shell_cmd(worker_command(command, target, args.prelaunch_source, launch_marker))
        _ = tmux(["send-keys", "-t", target, shell_launch, "Enter"], check=True)
        if wait_command_started(target, launch_marker=launch_marker) != CODEX_LAUNCH_UPDATED:
            return
        wait_shell(target, timeout_s=15.0)
    raise RuntimeError("Codex update completed but relaunch showed the update prompt again.")


def new_window(args: Args) -> str:
    if args.workdir is None:
        return target(args)
    out = tmux(new_window_command(args), check=True)
    tmux_target = out.stdout.strip()
    wait_shell(tmux_target)
    return tmux_target


def ensure_task_file(args: Args, tmux_target: str) -> Path:
    path = task_path(args.root, args.task_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    existing = path.read_text(encoding="utf-8") if existed else ""
    metadata = parse_task_metadata(existing) if existed else None
    if not existed:
        text = new_task_text(args, tmux_target)
        validate_runat_goal_tree(text)
    elif metadata is not None:
        text = launched_frontmatter_text(existing, args, tmux_target) if args.workdir is not None else existing
    else:
        text = upsert_header(existing, header(tmux_target, effective_tool(args))) if args.workdir is not None else existing
    if existed and args.manager_target and metadata is not None:
        text = replace_frontmatter_fields(text, {"managerat": args.manager_target})
    elif existed and args.manager_target:
        text = upsert_managerat(text, args.manager_target)
    if args.prompt_file is not None:
        if existed:
            sep = "" if not text or text.endswith("\n") else "\n"
            text += sep + args.prompt_file.read_text(encoding="utf-8").rstrip() + "\n"
    if text != existing or not existed:
        if existed:
            if parse_task_metadata(text) is None:
                validate_runat_header(text)
                validate_managerat_metadata(text)
        _ = path.write_text(text, encoding="utf-8")
    return path


def todo_line(args: Args, tmux_target: str) -> str:
    parts = [task_ref(args.root, args.task_file)]
    if tmux_target:
        parts.append(tmux_target)
    return " ".join(parts)


def refreshed_todo_entry(existing: str, ref: str, tmux_target: str) -> str:
    leading = existing[: len(existing) - len(existing.lstrip())]
    stripped = existing.strip()
    token, _sep, rest = stripped.partition(" ")
    rest = rest.lstrip()
    if not tmux_target:
        return f"{leading}{ref}" if not rest else f"{leading}{ref} {rest}"
    if not rest:
        return f"{leading}{ref} {tmux_target}"
    target_match = re.match(r"(?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?P<tail>.*)$", rest)
    if target_match is not None:
        return f"{leading}{ref} {tmux_target}{target_match.group('tail')}"
    loose_target_match = re.match(r"(?P<session>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<window>\d+)(?P<tail>.*)$", rest)
    if loose_target_match is not None:
        return f"{leading}{ref} {tmux_target}{loose_target_match.group('tail')}"
    return f"{leading}{ref} {tmux_target} {rest}"


def link_todo(args: Args, tmux_target: str) -> None:
    todo = args.root / "TODO.md"
    line = todo_line(args, tmux_target)
    lines = todo.read_text(encoding="utf-8").splitlines() if todo.exists() else ["current:", ""]
    ref = task_ref(args.root, args.task_file)
    aliases = {args.task_file, ref, str(task_path(args.root, args.task_file))}
    for idx, existing in enumerate(lines):
        stripped = existing.strip()
        if not stripped:
            continue
        token = stripped.split(maxsplit=1)[0]
        if token not in aliases:
            continue
        updated = refreshed_todo_entry(existing, ref, tmux_target)
        if existing != updated:
            lines[idx] = updated
            _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    try:
        current_idx = next(idx for idx, existing in enumerate(lines) if existing.strip() == "current:")
    except StopIteration:
        lines.extend(["", "current:", ""])
        current_idx = len(lines) - 2
    insert_at = current_idx + 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, line)
    _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dry_run(args: Args) -> None:
    tmux_target = f"{args.tmux_session}:DRYRUN" if args.workdir is not None else target(args)
    path = task_path(args.root, args.task_file)
    print(f"task_file: {path}")
    if not args.no_link:
        print(f"todo_line: {todo_line(args, tmux_target)}")
    if args.workdir is not None:
        if args.prelaunch_source is not None:
            print(f"prelaunch_source: {args.prelaunch_source}")
        command = ["tmux", *new_window_command(args)]
        print("tmux: " + " ".join(shlex.quote(part) for part in command))
        launch_target = f"{args.tmux_session}:DRYRUN"
        launch = ["tmux", "send-keys", "-t", launch_target, shell_cmd(worker_command(codex_cmd(args.session_id, args.reasoning_effort, args.codex_flags, args.prompt_file, effective_tool(args), is_vl_agent(args.task_file, launch_target)), launch_target, args.prelaunch_source, CODEX_LAUNCH_MARKER_DRY_RUN)), "Enter"]
        print("tmux: " + " ".join(shlex.quote(part) for part in launch))


def validate_inputs(args: Args) -> None:
    if args.prompt_file is not None and not args.prompt_file.is_file():
        raise ValueError(f"prompt file not found: {args.prompt_file}")
    if args.prelaunch_source is not None:
        if not args.prelaunch_source.is_file():
            raise ValueError(f"prelaunch source file not found: {args.prelaunch_source}")
        if not os.access(args.prelaunch_source, os.R_OK):
            raise ValueError(f"prelaunch source file is not readable: {args.prelaunch_source}")
    if any(not flag or "\0" in flag or "\n" in flag for flag in args.codex_flags):
        raise ValueError("codex flags must be non-empty single-line argv tokens.")
    if args.tool != "pcodx" and any("mcp_servers." in flag for flag in args.codex_flags):
        raise ValueError("MCP server config requires --tool pcodx.")
    if args.workdir is not None and args.prompt_file is None and is_vl_agent(args.task_file, target(args)):
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    if args.workdir is not None and is_vl_agent(args.task_file, target(args)) and not is_vl_submanager_task_file(args.task_file) and not args.manager_target:
        raise ValueError("VL worker launches require --manager-target for the owning submanager.")
    path = task_path(args.root, args.task_file)
    if path.exists() and args.manager_target:
        existing_text = path.read_text(encoding="utf-8")
        metadata = parse_task_metadata(existing_text)
        if metadata is not None:
            existing_manager_target = metadata.managerat
        else:
            validate_runat_header(existing_text)
            validate_managerat_metadata(existing_text)
            existing_manager_target = managerat_value(existing_text)
        if existing_manager_target and existing_manager_target != args.manager_target:
            raise ValueError(f"existing managerat {existing_manager_target} does not match --manager-target {args.manager_target}.")
    elif path.exists():
        existing_text = path.read_text(encoding="utf-8")
        if parse_task_metadata(existing_text) is None:
            validate_runat_header(existing_text)
            validate_managerat_metadata(existing_text)
    if path.exists():
        return
    tmux_target = "target" if args.workdir is not None else target(args)
    if args.workdir is None and TMUX_TARGET_RE.fullmatch(tmux_target) is None:
        raise ValueError("runat tmux target must be a full tmux target like `SESSION:WINDOW`.")
    text = new_task_text(args, tmux_target, validate_target=args.workdir is None)
    validate_runat_goal_tree(text)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.migrate_manager_owner:
            migrate_manager_owner(task_path(args.root, args.task_file), args.old_manager_target, args.new_manager_target, args.dry_run)
            return 0
        validate_inputs(args)
        if args.dry_run:
            dry_run(args)
            return 0
        existed = task_path(args.root, args.task_file).exists()
        tmux_target = new_window(args)
        path = ensure_task_file(args, tmux_target)
        if not args.no_link:
            link_todo(args, tmux_target)
        if args.workdir is not None:
            start_codex(tmux_target, args)
        print(path)
        if tmux_target:
            print(tmux_target)
        if not existed:
            print("reminder: fill pending_task_items in task frontmatter. Use omo_task_edit.py pending-add or omo_record_pending.py --task-file; do not hand-edit.")
    except Exception as exc:
        print(f"omo_task: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
