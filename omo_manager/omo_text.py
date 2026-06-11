#!/usr/bin/env python3
"""Route manager text from named files to safe helper paths."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER_DIR = Path.home() / ".config" / "omo_manager"
TEMP_SUFFIX_BY_KIND = {
    "email-subject": ".txt",
    "email-body": ".md",
    "worker-prompt": ".md",
    "agent-message": ".md",
}


class ParsedArgs(argparse.Namespace):
    cmd: str = ""
    subject_file: Path | None = None
    body_file: Path | None = None
    target: str = ""
    enter: bool = False
    enter_count: int = 1
    ready_timeout_s: float = 0
    task_file: str = ""
    tmux_session: str = ""
    tmux_window: str = ""
    workdir: Path | None = None
    tool: str = "codex"
    window_name: str = ""
    session_id: str = ""
    reasoning_effort: str = ""
    codex_flag: list[str] | None = None
    dry_run: bool = False
    kind: str = ""


def validate_subject(path: Path) -> None:
    subject = read_text_file(path).rstrip("\n")
    if not subject.strip():
        raise ValueError("subject file is empty.")
    if "\n" in subject or "\r" in subject:
        raise ValueError("subject file must contain one line.")
    if "\x00" in subject:
        raise ValueError("subject file contains NUL.")


def read_text_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    return path.read_text(encoding="utf-8")


def materialize_body(path: Path) -> Path:
    if str(path) == "-":
        raise ValueError("--body-file must be a named file.")
    if not path.is_file():
        raise ValueError(f"body file not found: {path}")
    return path


def run(command: list[str], dry_run: bool) -> int:
    if dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command, check=False).returncode


def create_temp_text_path(kind: str) -> Path:
    suffix = TEMP_SUFFIX_BY_KIND[kind]
    fd, raw_path = tempfile.mkstemp(prefix=f"omo-{kind}-", suffix=suffix, text=True)
    os.close(fd)
    path = Path(raw_path)
    path.chmod(0o600)
    return path


def email_command(args: ParsedArgs, body_path: Path) -> list[str]:
    if args.subject_file is None:
        raise ValueError("email requires --subject-file.")
    validate_subject(args.subject_file)
    return [str(HELPER_DIR / "omo_email_human.sh"), "--subject-file", str(args.subject_file), "--message-file", str(body_path)]


def tmux_command(args: ParsedArgs, body_path: Path) -> list[str]:
    if not args.target:
        raise ValueError("tmux requires --target.")
    command = [str(HELPER_DIR / "omo_tmux_send.py"), "--target", args.target, "--message-file", str(body_path)]
    if args.enter:
        command.extend(["--enter", "--enter-count", str(args.enter_count), "--ready-timeout-s", str(args.ready_timeout_s)])
    return command


def task_command(args: ParsedArgs, body_path: Path) -> list[str]:
    if not args.task_file:
        raise ValueError("task requires --task-file.")
    command = [str(HELPER_DIR / "omo_task.py"), "--task-file", args.task_file, "--prompt-file", str(body_path), "--tool", args.tool]
    if args.tmux_session:
        command.extend(["--tmux-session", args.tmux_session])
    if args.tmux_window:
        command.extend(["--tmux-window", args.tmux_window])
    if args.workdir is not None:
        command.extend(["--workdir", str(args.workdir)])
    if args.window_name:
        command.extend(["--window-name", args.window_name])
    if args.session_id:
        command.extend(["--session-id", args.session_id])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    for flag in args.codex_flag or ():
        command.extend(["--codex-flag", flag])
    return command


def parse_args(argv: list[str]) -> ParsedArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    email = sub.add_parser("email", help="Send manager email using subject/body files.")
    _ = email.add_argument("--subject-file", type=Path, required=True)
    _ = email.add_argument("--body-file", type=Path, required=True, help="Body file.")
    _ = email.add_argument("--dry-run", action="store_true")
    tmux = sub.add_parser("tmux", help="Paste a prompt file into tmux.")
    _ = tmux.add_argument("--target", required=True)
    _ = tmux.add_argument("--body-file", type=Path, required=True, help="Prompt file.")
    _ = tmux.add_argument("--enter", action="store_true")
    _ = tmux.add_argument("--enter-count", type=int, default=1)
    _ = tmux.add_argument("--ready-timeout-s", type=float, default=0)
    _ = tmux.add_argument("--dry-run", action="store_true")
    task = sub.add_parser("task", help="Create/start a task using a prompt file.")
    _ = task.add_argument("--task-file", required=True)
    _ = task.add_argument("--body-file", type=Path, required=True, help="Prompt file.")
    _ = task.add_argument("--tmux-session", default="")
    _ = task.add_argument("--tmux-window", default="")
    _ = task.add_argument("--workdir", type=Path)
    _ = task.add_argument("--tool", choices=("codex", "pcodx"), default="codex")
    _ = task.add_argument("--window-name", default="")
    _ = task.add_argument("--session-id", default="")
    _ = task.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="")
    _ = task.add_argument("--codex-flag", action="append")
    _ = task.add_argument("--dry-run", action="store_true")
    temp = sub.add_parser("temp", help="Create a 0600 text file under TMPDIR or /tmp and print its path.")
    _ = temp.add_argument("--kind", choices=tuple(TEMP_SUFFIX_BY_KIND), required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.cmd in {"email", "tmux", "task"} and parsed.body_file is None:
        parser.error("--body-file is required.")
    if parsed.cmd == "tmux" and parsed.enter_count < 1:
        parser.error("--enter-count must be positive.")
    if parsed.cmd == "tmux" and parsed.ready_timeout_s < 0:
        parser.error("--ready-timeout-s must be non-negative.")
    if parsed.cmd == "email" and str(parsed.subject_file) == "-":
        parser.error("--subject-file must be a named file.")
    return parsed


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.cmd == "temp":
            print(create_temp_text_path(args.kind))
            return 0
        body = materialize_body(args.body_file)
        command_by_cmd = {"email": email_command, "tmux": tmux_command, "task": task_command}
        command = command_by_cmd[args.cmd](args, body)
        return run(command, args.dry_run)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f"omo_text.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
