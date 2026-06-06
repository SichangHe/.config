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
STATUS_SESSION_RE = re.compile(rf"\bSession:\s*({UUID_RE})\b")


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


def extract_status_session_id(text: str) -> str:
    matches = STATUS_SESSION_RE.findall(text)
    return matches[-1] if matches else ""


def extract_new_status_session_id(before: str, after: str) -> str:
    session_id = extract_status_session_id(post_interrupt_output(before, after))
    if session_id:
        return session_id
    if after.count("/status") <= before.count("/status"):
        return ""
    return extract_status_session_id(after.rsplit("/status", 1)[-1])


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
    move_todo_to_previous(args.root, args.task_file)


def section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    start = -1
    for idx, line in enumerate(lines):
        if line.strip() == f"{name}:":
            start = idx
            break
    if start < 0:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.endswith(":") and stripped[:-1] in {"current", "previous", "human pending", "low priority"}:
            end = idx
            break
    return start, end


def done_todo_line(line: str) -> str:
    return line if "(done)" in line else f"{line} (done)"


def move_todo_to_previous(root: Path, task_file: str) -> None:
    todo = root / "TODO.md"
    if not todo.exists():
        return
    lines = todo.read_text(encoding="utf-8").splitlines()
    current = section_bounds(lines, "current")
    if current is None:
        return
    current_start, current_end = current
    source_idx = -1
    for idx in range(current_start + 1, current_end):
        stripped = lines[idx].strip()
        if stripped and stripped.split(maxsplit=1)[0] == task_file:
            source_idx = idx
            break
    if source_idx < 0:
        return
    moved = done_todo_line(lines.pop(source_idx).strip())
    previous = section_bounds(lines, "previous")
    if previous is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("previous:")
        previous = (len(lines) - 1, len(lines))
    previous_start, previous_end = previous
    for idx in range(previous_start + 1, previous_end):
        stripped = lines[idx].strip()
        if stripped and stripped.split(maxsplit=1)[0] == task_file:
            _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    lines.insert(previous_start + 1, moved)
    _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wait_shell(target: str, deadline_s: float) -> None:
    while time.monotonic() < deadline_s:
        if current_command(target) in SHELL_COMMANDS:
            return
        time.sleep(0.25)


def paste_text(target: str, text: str) -> None:
    buffer_name = f"omo-codex-stop-{os.getpid()}-{time.monotonic_ns()}"
    _ = tmux(["set-buffer", "-b", buffer_name, text], check=True)
    try:
        _ = tmux(["paste-buffer", "-b", buffer_name, "-t", target], check=True)
    finally:
        _ = tmux(["delete-buffer", "-b", buffer_name])


def query_status_session_id(target: str, n_lines: int, wait_s: float) -> tuple[str, str]:
    before = capture(target, n_lines)
    paste_text(target, "/status")
    _ = tmux(["send-keys", "-t", target, "Enter", "Enter", "Enter"], check=True)
    deadline_s = time.monotonic() + wait_s
    after = before
    while time.monotonic() < deadline_s:
        after = capture(target, n_lines)
        session_id = extract_new_status_session_id(before, after)
        if session_id:
            return session_id, after
        time.sleep(0.25)
    return "", after


def send_exit_keys(target: str) -> None:
    _ = tmux(["send-keys", "-t", target, "C-c"], check=True)
    time.sleep(0.5)
    if current_command(target) not in SHELL_COMMANDS:
        _ = tmux(["send-keys", "-t", target, "C-c"], check=True)


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
    session_id, before_close = query_status_session_id(args.target, args.lines, args.wait_s)
    send_exit_keys(args.target)
    wait_shell(args.target, time.monotonic() + args.wait_s)
    after = capture(args.target, args.lines)
    return session_id or extract_resume_id(post_interrupt_output(before_close, after))


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
