#!/usr/bin/env python3
"""Report Codex TUI status from one tmux window tail."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

CODEX_RE = re.compile(r"  gpt-")
ERROR_RE = re.compile(r"\b(error|failed|panic|traceback|exception)\b", re.IGNORECASE)
SEP_RE = re.compile(r"^─+$")
WORKED_RE = re.compile(r"^─ Worked for .+ ─+$")


@dataclass(frozen=True)
class Args:
    target: str
    n_lines: int


@dataclass(frozen=True)
class Report:
    status: str
    lines: list[str]


class ParsedArgs(argparse.Namespace):
    target: str = ""
    n_lines: int = 80


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("target")
    _ = parser.add_argument("--lines", type=int, default=80)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.n_lines <= 0:
        parser.error("--lines must be positive.")
    return Args(parsed.target, parsed.n_lines)


def tail(target: str, n_lines: int) -> list[str]:
    out = subprocess.run(["tmux", "capture-pane", "-p", "-t", target, "-S", f"-{n_lines}"], capture_output=True, text=True, timeout=5, check=False)
    if out.returncode != 0:
        return []
    lines = [line.rstrip() for line in out.stdout.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def last_output(lines: list[str]) -> list[str]:
    end = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        if WORKED_RE.match(lines[idx]):
            end = idx
            break
    start = 0
    for idx in range(end - 1, -1, -1):
        if SEP_RE.match(lines[idx]):
            start = idx + 1
            break
    out = [line.rstrip() for line in lines[start:end]]
    while out and not out[0]:
        del out[0]
    while out and not out[-1]:
        out.pop()
    return out


def status(lines: list[str], output: list[str]) -> str:
    if not lines or CODEX_RE.search(lines[-1]) is None:
        return "not_codex"
    text = "\n".join(output or lines[-20:])
    if ERROR_RE.search(text) is not None:
        return "error"
    if any(WORKED_RE.match(line) for line in lines):
        return "ready"
    return "running"


def inspect(args: Args) -> Report:
    lines = tail(args.target, args.n_lines)
    output = last_output(lines)
    return Report(status(lines, output), output)


def main(argv: list[str]) -> int:
    try:
        report = inspect(parse_args(argv))
        print(f"status: {report.status}")
        print("last_output:")
        for line in report.lines:
            print(line)
    except Exception as exc:
        print(f"omo_codex_status: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
