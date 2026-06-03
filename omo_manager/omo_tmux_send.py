#!/usr/bin/env python3
"""Safely paste text into a tmux target without shell/key escaping.

The helper accepts message text from stdin or a file, copies it to a private
temporary file, loads that file into a tmux buffer, pastes the buffer into the
target pane/window, and optionally sends Enter. It intentionally avoids
`tmux send-keys MESSAGE` for arbitrary text because send-keys treats some
tokens specially and requires callers to get shell quoting exactly right.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Args:
    target: str
    message_file: Path | None
    enter: bool
    dry_run: bool


class ParsedArgs(argparse.Namespace):
    target: str = ""
    message_file: Path | None = None
    enter: bool = False
    dry_run: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="tmux target pane/window, e.g. cfg:1.0")
    _ = parser.add_argument("--message-file", type=Path, help="Read message text from this file instead of stdin.")
    enter_group = parser.add_mutually_exclusive_group()
    _ = enter_group.add_argument("--enter", dest="enter", action="store_true", help="Send Enter after pasting.")
    _ = enter_group.add_argument("--no-enter", dest="enter", action="store_false", help="Paste only; default.")
    _ = parser.add_argument("--dry-run", action="store_true", help="Validate inputs and print planned tmux actions without touching tmux.")
    parser.set_defaults(enter=False)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.target, parsed.message_file, parsed.enter, parsed.dry_run)


def read_message(args: Args) -> str:
    if args.message_file is None:
        return sys.stdin.read()
    if not args.message_file.is_file():
        raise RuntimeError(f"message file not found: {args.message_file}")
    return args.message_file.read_text(encoding="utf-8")


def write_private_temp(message: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="omo-tmux-send.", text=True)
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(message)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def run_tmux(args: Args, message: str) -> None:
    temp_path = write_private_temp(message)
    buffer_name = f"omo-tmux-send-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        if args.dry_run:
            _ = print(f"would load tmux buffer {buffer_name} from {temp_path}")
            _ = print(f"would paste buffer {buffer_name} to {args.target}")
            if args.enter:
                _ = print(f"would send Enter to {args.target}")
            return
        _ = subprocess.run(["tmux", "load-buffer", "-b", buffer_name, str(temp_path)], timeout=5, check=True)
        _ = subprocess.run(["tmux", "paste-buffer", "-b", buffer_name, "-t", args.target], timeout=5, check=True)
        if args.enter:
            _ = subprocess.run(["tmux", "send-keys", "-t", args.target, "Enter"], timeout=5, check=True)
    finally:
        temp_path.unlink(missing_ok=True)
        if not args.dry_run:
            _ = subprocess.run(["tmux", "delete-buffer", "-b", buffer_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        run_tmux(args, read_message(args))
    except Exception as exc:
        print(f"omo_tmux_send: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
