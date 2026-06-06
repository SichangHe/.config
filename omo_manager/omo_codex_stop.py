#!/usr/bin/env python3
"""Stop a Codex tmux pane and print the captured resume id if Codex exposes one."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RESUME_RE = re.compile(rf"(?i)\bcodex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b")


@dataclass(frozen=True)
class Args:
    target: str
    wait_s: float
    lines: int
    dry_run: bool
    allow_self: bool
    root: Path = DEFAULT_ROOT
    task_file: str = ""


class ParsedArgs(argparse.Namespace):
    target: str = ""
    wait_s: float = 10.0
    lines: int = 2000
    dry_run: bool = False
    allow_self: bool = False
    root: Path = DEFAULT_ROOT
    task_file: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="tmux pane/window target, e.g. `cfg:2.0`.")
    _ = parser.add_argument("--wait-s", type=float, default=10.0)
    _ = parser.add_argument("--lines", type=int, default=2000)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--allow-self", action="store_true", help="Allow stopping the current tmux pane.")
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", default="", help="Append a durable close note to this task markdown file.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.wait_s < 0:
        parser.error("--wait-s must be non-negative.")
    if parsed.lines <= 0:
        parser.error("--lines must be positive.")
    if parsed.task_file and not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    return Args(
        parsed.target,
        parsed.wait_s,
        parsed.lines,
        parsed.dry_run,
        parsed.allow_self,
        parsed.root.resolve(),
        parsed.task_file,
    )


def tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5, check=check)


def pane_id(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{pane_id}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def current_pane_id() -> str:
    out = tmux(["display-message", "-p", "#{pane_id}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def current_command(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{pane_current_command}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def capture(target: str, n_lines: int) -> str:
    out = tmux(["capture-pane", "-p", "-t", target, "-S", f"-{n_lines}"])
    return out.stdout if out.returncode == 0 else ""


def extract_resume_id(text: str) -> str:
    matches = RESUME_RE.findall(text)
    return matches[-1] if matches else ""


def post_interrupt_output(before: str, after: str) -> str:
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    for n_lines in range(min(len(before_lines), len(after_lines)), 0, -1):
        if before_lines[-n_lines:] == after_lines[:n_lines]:
            return "".join(after_lines[n_lines:])
    before_text = {line.strip() for line in before_lines if line.strip()}
    after_text = {line.strip() for line in after_lines if line.strip()}
    if before_text.isdisjoint(after_text):
        return after
    return ""


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if path != root and root not in path.parents:
        raise RuntimeError("task file escapes root")
    if not path.is_file():
        raise RuntimeError(f"task file not found: {path}")
    return path


def close_note(target: str, session_id: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now().astimezone()).strftime("%m-%d %H:%M %Z")
    if session_id:
        return (
            f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; "
            f"session_id: `{session_id}`.)\n"
        )
    return (
        f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; "
        "Codex session id not found in captured tmux output.)\n"
    )


def record_close(args: Args, session_id: str) -> None:
    if not args.task_file or args.dry_run:
        return
    path = task_path(args.root, args.task_file)
    with path.open("a", encoding="utf-8") as handle:
        _ = handle.write(close_note(args.target, session_id))


def wait_shell(target: str, deadline_s: float) -> None:
    while time.monotonic() < deadline_s:
        if current_command(target) in SHELL_COMMANDS:
            return
        time.sleep(0.25)


def stop(args: Args) -> str:
    target_pane = pane_id(args.target)
    if not target_pane:
        raise RuntimeError(f"tmux target not found: {args.target}")
    if not args.allow_self and target_pane == current_pane_id():
        raise RuntimeError(f"refusing to stop the current pane: {args.target}")
    if args.task_file:
        _ = task_path(args.root, args.task_file)
    if args.dry_run:
        print(f"would send Ctrl-C to {args.target}")
        return ""
    before = capture(args.target, args.lines)
    _ = tmux(["send-keys", "-t", args.target, "C-c"], check=True)
    wait_shell(args.target, time.monotonic() + args.wait_s)
    after = capture(args.target, args.lines)
    return extract_resume_id(post_interrupt_output(before, after))


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        session_id = stop(args)
        record_close(args, session_id)
        if session_id:
            print(f"session_id: {session_id}")
            print(f"resume_cmd: codex resume {session_id}")
        else:
            print("session_id:")
            if args.task_file and not args.dry_run:
                print("warning: Codex resume session id not found in captured tmux output", file=sys.stderr)
    except Exception as exc:
        print(f"omo_codex_stop: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
