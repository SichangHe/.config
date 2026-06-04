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
READY_RE = re.compile(r"^› Use /skills to list available skills$")
BUSY_RE = re.compile(r"^• (?:Working|Messages to be submitted after next tool call)\b")


@dataclass(frozen=True)
class Args:
    target: str
    n_lines: int


@dataclass(frozen=True)
class Block:
    lines: list[str]
    has_footer: bool


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


def current_block(lines: list[str]) -> Block:
    body = lines[:-1] if lines and CODEX_RE.search(lines[-1]) is not None else lines[:]
    start = 0
    for idx in range(len(body) - 1, -1, -1):
        if SEP_RE.match(body[idx]):
            start = idx + 1
            break
    block = [line.rstrip() for line in body[start:]]
    has_footer = bool(block and WORKED_RE.match(block[-1]))
    if has_footer:
        block.pop()
    while block and not block[0]:
        del block[0]
    while block and not block[-1]:
        block.pop()
    return Block(block, has_footer)


def last_output(lines: list[str]) -> list[str]:
    return current_block(lines).lines


def status(lines: list[str], block: Block) -> str:
    if not lines or CODEX_RE.search(lines[-1]) is None:
        return "not_codex"
    text = "\n".join(block.lines or lines[-20:])
    if ERROR_RE.search(text) is not None:
        return "error"
    if any(BUSY_RE.search(line) is not None for line in lines[-20:]):
        return "running"
    if block.has_footer or any(READY_RE.match(line) is not None for line in lines[-10:]):
        return "ready"
    return "running"


def inspect(args: Args) -> Report:
    lines = tail(args.target, args.n_lines)
    block = current_block(lines)
    return Report(status(lines, block), block.lines)


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
