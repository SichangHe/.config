#!/usr/bin/env python3
"""Create a markdown task file and link it into `TODO.md`."""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    tmux_session: str
    pane: str
    tool: str
    prompt_file: Path | None
    no_link: bool


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    tmux_session: str = ""
    pane: str = ""
    tool: str = "opencode"
    prompt_file: Path | None = None
    no_link: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--tmux-session", default="")
    _ = parser.add_argument("--pane", default="")
    _ = parser.add_argument("--tool", default="opencode")
    _ = parser.add_argument("--prompt-file", type=Path)
    _ = parser.add_argument("--no-link", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.pane and not parsed.tmux_session:
        parser.error("--pane requires --tmux-session.")
    return Args(parsed.root.resolve(), parsed.task_file, parsed.tmux_session, parsed.pane, parsed.tool, parsed.prompt_file, parsed.no_link)


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("task file escapes root")
    return path


def header(args: Args) -> str:
    parts = [part for part in (args.tmux_session, args.pane, args.tool if args.tmux_session else "") if part]
    return " ".join(parts)


def ensure_task_file(args: Args) -> Path:
    path = task_path(args.root, args.task_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    if not path.exists():
        first = header(args)
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


def todo_line(args: Args) -> str:
    parts = [args.task_file]
    if args.tmux_session:
        parts.append(args.tmux_session)
    if args.pane:
        parts.append(args.pane)
    return " ".join(parts)


def link_todo(args: Args) -> None:
    todo = args.root / "TODO.md"
    line = todo_line(args)
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


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        path = ensure_task_file(args)
        if not args.no_link:
            link_todo(args)
        print(path)
    except Exception as exc:
        print(f"omo_task: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
