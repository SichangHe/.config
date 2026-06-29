#!/usr/bin/env python3
"""Create/link a markdown task and optionally start a Codex tmux window."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from omo_manager.omo_codex_status import current_block, status, tail
except ModuleNotFoundError:
    from omo_codex_status import current_block, status, tail

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_WORKER_INSTRUCTIONS = Path(__file__).with_name("WORKER_DEFAULTS.md")
VL_WORKER_INSTRUCTIONS = Path(__file__).with_name("VL_WORKER_DEFAULTS.md")
PCODX_WRAPPER = Path(__file__).resolve().with_name("pcodx")
COMMAND_BY_TOOL = {
    "codex": ("bunx", "@openai/codex", "--dangerously-bypass-approvals-and-sandbox"),
    "pcodx": (str(PCODX_WRAPPER),),
}
DEFAULT_TOOL = "pcodx"
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
BULLET_MARKERS = ("- ", "* ")


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


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
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
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--session-id", default="", help="Codex session id to resume in a new worker window.")
    _ = parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="", help="Start Codex with `model_reasoning_effort` for this worker.")
    _ = parser.add_argument("--codex-flag", action="append", help="Extra raw Codex argv token. Repeat for flags and values; use `--codex-flag=--flag` when the token starts with `--`.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.pane:
        parser.error("pane selection is no longer supported; pane 0 is implied.")
    if parsed.workdir is not None and not parsed.tmux_session:
        parser.error("--workdir requires --tmux-session.")
    if parsed.tool not in COMMAND_BY_TOOL:
        parser.error("only --tool codex or --tool pcodx is supported.")
    tool_explicit = any(arg == "--tool" or arg.startswith("--tool=") for arg in argv)
    return Args(parsed.root.resolve(), parsed.task_file, parsed.tmux_session, parsed.tmux_window, parsed.tool, parsed.workdir, parsed.window_name, parsed.prompt_file, parsed.no_link, parsed.dry_run, parsed.session_id, parsed.reasoning_effort, tuple(parsed.codex_flag or ()), tool_explicit)


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("task file escapes root")
    return path


def target(args: Args) -> str:
    if args.tmux_session and args.tmux_window:
        return f"{args.tmux_session}:{args.tmux_window}"
    return args.tmux_session


def target_session(tmux_target: str) -> str:
    return tmux_target.split(":", 1)[0]


def is_vl_task_file(task_file: str) -> bool:
    return Path(task_file).name.startswith("vl_")


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
    if lines and lines[0].startswith("runat: "):
        lines[0] = f"{first}\n"
        return "".join(lines)
    return f"{first}\n\n{existing}"


def is_bullet(line: str) -> bool:
    stripped = line.lstrip()
    return any(stripped.startswith(marker) for marker in BULLET_MARKERS)


def is_runat_header(line: str) -> bool:
    return line.strip().split(maxsplit=1)[0:1] == ["runat:"]


def runat_goal_tree_error(text: str) -> str:
    lines = text.splitlines()
    if not lines or not is_runat_header(lines[0]):
        return ""
    if len(lines) < 2 or not lines[1].strip():
        return "task files starting with `runat:` must put a high-level goal directly after the `runat:` line."
    if is_bullet(lines[1]):
        return "task files starting with `runat:` must use a plain high-level goal line before bullet subgoals."
    if len(lines) < 3 or not is_bullet(lines[2]):
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
    if args.tool_explicit or not args.session_id:
        return args.tool
    path = task_path(args.root, args.task_file)
    if path.exists() and (tool := task_file_tool(path.read_text(encoding="utf-8"), target(args))):
        return tool
    return args.tool


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


def wait_command_started(target: str, timeout_s: float = 5.0) -> None:
    deadline_s = time.monotonic() + timeout_s
    last_command = ""
    last_status = "unknown"
    while time.monotonic() < deadline_s:
        lines = tail(target, 80)
        last_status = status(lines, current_block(lines))
        if last_status != "not_codex":
            return
        last_command = current_command(target)
        if last_command and last_command not in SHELL_COMMANDS:
            return
        time.sleep(0.05)
    raise RuntimeError(f"Codex launch not verified after {timeout_s:g}s: pane command={last_command or 'unknown'}, status={last_status}")


def new_window_command(args: Args) -> list[str]:
    name = args.window_name or Path(args.task_file).stem
    return ["new-window", "-P", "-F", "#{session_name}:#{window_index}", "-t", args.tmux_session, "-n", name, "-c", str(args.workdir)]


def start_codex(target: str, args: Args) -> None:
    vl_agent = is_vl_agent(args.task_file, target)
    if vl_agent and args.prompt_file is None:
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    command = shell_cmd(codex_cmd(args.session_id, args.reasoning_effort, args.codex_flags, args.prompt_file, effective_tool(args), vl_agent))
    _ = tmux(["send-keys", "-t", target, command, "Enter"], check=True)
    wait_command_started(target)


def new_window(args: Args) -> str:
    if args.workdir is None:
        return target(args)
    out = tmux(new_window_command(args), check=True)
    tmux_target = out.stdout.strip()
    wait_shell(tmux_target)
    start_codex(tmux_target, args)
    return tmux_target


def ensure_task_file(args: Args, tmux_target: str) -> Path:
    path = task_path(args.root, args.task_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    existing = path.read_text(encoding="utf-8") if existed else ""
    text = upsert_header(existing, header(tmux_target, effective_tool(args))) if args.workdir is not None or not existed else existing
    if args.prompt_file is not None:
        sep = "" if not text or text.endswith("\n") else "\n"
        text += sep + args.prompt_file.read_text(encoding="utf-8").rstrip() + "\n"
    if not existed:
        validate_runat_goal_tree(text)
    if text != existing or not existed:
        _ = path.write_text(text, encoding="utf-8")
    return path


def todo_line(args: Args, tmux_target: str) -> str:
    parts = [args.task_file]
    if tmux_target:
        parts.append(tmux_target)
    return " ".join(parts)


def link_todo(args: Args, tmux_target: str) -> None:
    todo = args.root / "TODO.md"
    line = todo_line(args, tmux_target)
    lines = todo.read_text(encoding="utf-8").splitlines() if todo.exists() else ["current:", ""]
    if any(existing.split(maxsplit=1)[0] == args.task_file for existing in lines if existing.strip()):
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
        command = ["tmux", *new_window_command(args)]
        print("tmux: " + " ".join(shlex.quote(part) for part in command))
        launch_target = f"{args.tmux_session}:DRYRUN"
        launch = ["tmux", "send-keys", "-t", launch_target, shell_cmd(codex_cmd(args.session_id, args.reasoning_effort, args.codex_flags, args.prompt_file, effective_tool(args), is_vl_agent(args.task_file, launch_target))), "Enter"]
        print("tmux: " + " ".join(shlex.quote(part) for part in launch))


def validate_inputs(args: Args) -> None:
    if args.prompt_file is not None and not args.prompt_file.is_file():
        raise ValueError(f"prompt file not found: {args.prompt_file}")
    if any(not flag or "\0" in flag or "\n" in flag for flag in args.codex_flags):
        raise ValueError("codex flags must be non-empty single-line argv tokens.")
    if args.workdir is not None and args.prompt_file is None and is_vl_agent(args.task_file, target(args)):
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    path = task_path(args.root, args.task_file)
    if path.exists():
        return
    tmux_target = "target" if args.workdir is not None else target(args)
    first = header(tmux_target, effective_tool(args))
    prompt = args.prompt_file.read_text(encoding="utf-8").rstrip() if args.prompt_file is not None else ""
    text = f"{first}\n{prompt}\n" if first else f"{prompt}\n"
    validate_runat_goal_tree(text)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        validate_inputs(args)
        if args.dry_run:
            dry_run(args)
            return 0
        tmux_target = new_window(args)
        path = ensure_task_file(args, tmux_target)
        if not args.no_link:
            link_todo(args, tmux_target)
        print(path)
        if tmux_target:
            print(tmux_target)
    except Exception as exc:
        print(f"omo_task: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
