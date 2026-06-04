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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from omo_manager.omo_codex_status import current_block, status, tail
except ModuleNotFoundError:
    from omo_codex_status import current_block, status, tail


@dataclass(frozen=True)
class Args:
    target: str
    message_file: Path | None
    enter_count: int
    enter_delay_s: float
    ready_timeout_s: float
    dry_run: bool


class ParsedArgs(argparse.Namespace):
    target: str = ""
    message_file: Path | None = None
    enter: bool = False
    enter_count: int = 1
    enter_delay_s: float = 0.15
    ready_timeout_s: float = 0
    dry_run: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="tmux target pane/window, e.g. cfg:1.0")
    _ = parser.add_argument("--message-file", type=Path, help="Read message text from this file instead of stdin.")
    enter_group = parser.add_mutually_exclusive_group()
    _ = enter_group.add_argument("--enter", dest="enter", action="store_true", help="Send Enter after pasting.")
    _ = enter_group.add_argument("--no-enter", dest="enter", action="store_false", help="Paste only; default.")
    _ = parser.add_argument("--enter-count", type=int, default=1, help="Number of Enter keys to send when submitting; default: 1.")
    _ = parser.add_argument("--enter-delay-s", type=float, default=0.15, help="Delay between repeated Enter keys; default: 0.15.")
    _ = parser.add_argument("--ready-timeout-s", type=float, default=0, help="When submitting to Codex, wait up to this many seconds for an idle input box before paste; default: 0.")
    _ = parser.add_argument("--dry-run", action="store_true", help="Validate inputs and print planned tmux actions without touching tmux.")
    parser.set_defaults(enter=False)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.enter_count < 1:
        parser.error("--enter-count must be positive.")
    if parsed.enter_delay_s < 0:
        parser.error("--enter-delay-s must be non-negative.")
    if parsed.ready_timeout_s < 0:
        parser.error("--ready-timeout-s must be non-negative.")
    return Args(parsed.target, parsed.message_file, parsed.enter_count if parsed.enter else 0, parsed.enter_delay_s, parsed.ready_timeout_s if parsed.enter else 0, parsed.dry_run)


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


def wait_ready(args: Args) -> None:
    if args.ready_timeout_s <= 0:
        return
    deadline_s = time.monotonic() + args.ready_timeout_s
    last_status = "unknown"
    while True:
        lines = tail(args.target, 80)
        last_status = status(lines, current_block(lines))
        if last_status in {"ready", "not_codex"}:
            return
        if time.monotonic() >= deadline_s:
            raise RuntimeError(f"target not ready after {args.ready_timeout_s:g}s: {last_status}")
        time.sleep(min(0.5, max(0.05, deadline_s - time.monotonic())))


def run_tmux(args: Args, message: str) -> None:
    temp_path = write_private_temp(message)
    buffer_name = f"omo-tmux-send-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        if args.dry_run:
            _ = print(f"would load tmux buffer {buffer_name} from {temp_path}")
            _ = print(f"would paste buffer {buffer_name} to {args.target}")
            for _ in range(args.enter_count):
                _ = print(f"would send Enter to {args.target}")
            return
        wait_ready(args)
        _ = subprocess.run(["tmux", "load-buffer", "-b", buffer_name, str(temp_path)], timeout=5, check=True)
        _ = subprocess.run(["tmux", "paste-buffer", "-b", buffer_name, "-t", args.target], timeout=5, check=True)
        for idx in range(args.enter_count):
            if idx:
                time.sleep(args.enter_delay_s)
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
