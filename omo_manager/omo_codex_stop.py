#!/usr/bin/env python3
"""Stop a Codex tmux pane and print the captured resume id if Codex exposes one."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass

SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RESUME_RE = re.compile(rf"(?i)\bcodex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b")
UUID_LINE_RE = re.compile(rf"(?i)\bresume\b[^\n\r]*\b({UUID_RE})\b")


@dataclass(frozen=True)
class Args:
    target: str
    wait_s: float
    lines: int
    dry_run: bool
    allow_self: bool


class ParsedArgs(argparse.Namespace):
    target: str = ""
    wait_s: float = 10.0
    lines: int = 2000
    dry_run: bool = False
    allow_self: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="tmux pane/window target, e.g. `cfg:2.0`.")
    _ = parser.add_argument("--wait-s", type=float, default=10.0)
    _ = parser.add_argument("--lines", type=int, default=2000)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--allow-self", action="store_true", help="Allow stopping the current tmux pane.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.wait_s < 0:
        parser.error("--wait-s must be non-negative.")
    if parsed.lines <= 0:
        parser.error("--lines must be positive.")
    return Args(parsed.target, parsed.wait_s, parsed.lines, parsed.dry_run, parsed.allow_self)


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
    for regex in (RESUME_RE, UUID_LINE_RE):
        matches = regex.findall(text)
        if matches:
            return matches[-1]
    return ""


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
    if args.dry_run:
        print(f"would send Ctrl-C to {args.target}")
        return ""
    _ = tmux(["send-keys", "-t", args.target, "C-c"], check=True)
    wait_shell(args.target, time.monotonic() + args.wait_s)
    return extract_resume_id(capture(args.target, args.lines))


def main(argv: list[str]) -> int:
    try:
        session_id = stop(parse_args(argv))
        if session_id:
            print(f"session_id: {session_id}")
            print(f"resume_cmd: codex resume {session_id}")
        else:
            print("session_id:")
    except Exception as exc:
        print(f"omo_codex_stop: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
