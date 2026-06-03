#!/usr/bin/env python3
"""Create/link a markdown task and optionally start a Codex tmux window."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
CODEX_CMD = "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox"


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


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    tmux_session: str = ""
    tmux_window: str = ""
    tool: str = "codex"
    workdir: Path | None = None
    window_name: str = ""
    prompt_file: Path | None = None
    no_link: bool = False
    dry_run: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--tmux-session", default="")
    _ = parser.add_argument("--tmux-window", default="")
    _ = parser.add_argument("--pane", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--tool", default="codex")
    _ = parser.add_argument("--workdir", type=Path)
    _ = parser.add_argument("--window-name", default="")
    _ = parser.add_argument("--prompt-file", type=Path)
    _ = parser.add_argument("--no-link", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.pane:
        parser.error("pane selection is no longer supported; pane 0 is implied.")
    if parsed.workdir is not None and not parsed.tmux_session:
        parser.error("--workdir requires --tmux-session.")
    if parsed.tool != "codex":
        parser.error("only --tool codex is supported.")
    return Args(parsed.root.resolve(), parsed.task_file, parsed.tmux_session, parsed.tmux_window, parsed.tool, parsed.workdir, parsed.window_name, parsed.prompt_file, parsed.no_link, parsed.dry_run)


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("task file escapes root")
    return path


def target(args: Args) -> str:
    if args.tmux_session and args.tmux_window:
        return f"{args.tmux_session}:{args.tmux_window}"
    return args.tmux_session


def header(tmux_target: str, tool: str) -> str:
    return f"runat: {tmux_target} {tool}" if tmux_target else ""


def codex_cmd() -> str:
    return CODEX_CMD


def new_window(args: Args) -> str:
    if args.workdir is None:
        return target(args)
    name = args.window_name or Path(args.task_file).stem
    command = ["tmux", "new-window", "-P", "-F", "#{session_name}:#{window_index}", "-t", args.tmux_session, "-n", name, "-c", str(args.workdir), codex_cmd()]
    out = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    return out.stdout.strip()


def ensure_task_file(args: Args, tmux_target: str) -> Path:
    path = task_path(args.root, args.task_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    if not path.exists():
        first = header(tmux_target, args.tool)
        if first:
            chunks.append(f"{first}\n\n")
    if args.prompt_file is not None:
        chunks.append(args.prompt_file.read_text(encoding="utf-8").rstrip() + "\n")
    if chunks:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        _ = path.write_text(existing + sep + "".join(chunks), encoding="utf-8")
    elif not path.exists():
        _ = path.write_text("", encoding="utf-8")
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
        name = args.window_name or Path(args.task_file).stem
        command = ["tmux", "new-window", "-P", "-F", "#{session_name}:#{window_index}", "-t", args.tmux_session, "-n", name, "-c", str(args.workdir), codex_cmd()]
        print("tmux: " + " ".join(shlex.quote(part) for part in command))


def validate_inputs(args: Args) -> None:
    if args.prompt_file is not None and not args.prompt_file.is_file():
        raise ValueError(f"prompt file not found: {args.prompt_file}")


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
