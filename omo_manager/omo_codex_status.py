#!/usr/bin/env python3
"""Report Codex TUI status from one tmux window tail."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_COMPACTION_WAIT_TIMEOUT_S = float(os.environ.get("OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S", "300"))
COMPACTION_WAIT_INTERVAL_S = 0.5
COMPACTION_WAIT_LINES = 2000
CODEX_RE = re.compile(r"  gpt-")
ERROR_RE = re.compile(r"\b(error|failed|panic|traceback|exception)\b", re.IGNORECASE)
SEP_RE = re.compile(r"^─+$")
WORKED_RE = re.compile(r"^─ Worked for .+ ─+$")
READY_RE = re.compile(r"^› Use /skills to list available skills$")
INPUT_RE = re.compile(r"^› ")
BUSY_RE = re.compile(r"^• (?:Working|Messages to be submitted after next tool call)\b")
COMPACTING_RE = re.compile(r"^• Compacting\b", re.IGNORECASE)
BACKGROUND_RUNNING_RE = re.compile(r"^• .*?\b(?:Waiting for background terminal|[1-9][0-9]* background terminals? running)\b")
QUEUE_MESSAGE_FOOTER_RE = re.compile(r"\btab to queue message\b")
CODEX_EMPTY_INPUT_TEXTS = {
    "Use /skills to list available skills",
    "Find and fix a bug in @filename",
    "Summarize recent commits",
    "Improve documentation in @filename",
    "Write tests for @filename",
    "Run /review on my current changes",
}
CODEX_RUNNING_EMPTY_INPUT_TEXTS = {
    "Explain this codebase",
    "Implement {feature}",
}


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
    input_text: str = ""
    can_submit_input: bool = False


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
    lines = [line.rstrip() for line in (out.stdout or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def has_codex_model_footer(lines: list[str]) -> bool:
    return bool(lines and CODEX_RE.search(lines[-1]) is not None)


def current_block(lines: list[str]) -> Block:
    body = lines[:-1] if has_codex_model_footer(lines) else lines[:]
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


def current_input_text(lines: list[str]) -> str:
    body = lines[:-1] if has_codex_model_footer(lines) else lines[:]
    while body and not body[-1].strip():
        body.pop()
    for idx in range(len(body) - 1, -1, -1):
        line = body[idx].lstrip()
        if line.startswith("›"):
            input_lines = body[idx:]
            if any(after.startswith(("• ", "│", "└", "├", "─")) for after in input_lines[1:]):
                return ""
            text_lines = [line[1:].strip()]
            text_lines.extend(after.rstrip() for after in input_lines[1:])
            return "\n".join(text_lines).strip()
    return ""


def has_running_indicator(lines: list[str]) -> bool:
    return has_compacting_indicator(lines) or any(BUSY_RE.search(line) is not None or BACKGROUND_RUNNING_RE.search(line) is not None for line in lines[-20:])


def has_visible_running_indicator(lines: list[str]) -> bool:
    return has_compacting_indicator(lines) or any(BUSY_RE.search(line) is not None or BACKGROUND_RUNNING_RE.search(line) is not None for line in lines)


def has_compacting_indicator(lines: list[str]) -> bool:
    if not has_codex_model_footer(lines):
        return False
    return any(COMPACTING_RE.search(line) is not None for line in current_block(lines).lines)


def has_queued_message_footer(lines: list[str]) -> bool:
    return bool(lines and QUEUE_MESSAGE_FOOTER_RE.search(lines[-1]) is not None)


def has_queued_running_input(lines: list[str]) -> bool:
    return has_queued_message_footer(lines) and has_visible_running_indicator(lines)


def is_empty_input_text(lines: list[str], input_text: str) -> bool:
    return input_text in CODEX_EMPTY_INPUT_TEXTS or (has_running_indicator(lines) and input_text in CODEX_RUNNING_EMPTY_INPUT_TEXTS)


def can_submit_stuck_input(lines: list[str]) -> bool:
    if has_queued_running_input(lines) or has_compacting_indicator(lines):
        return False
    input_text = current_input_text(lines)
    return bool(has_codex_model_footer(lines) and input_text and not is_empty_input_text(lines, input_text))


def submit_stuck_input_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES, compaction_wait_timeout_s: float = DEFAULT_COMPACTION_WAIT_TIMEOUT_S) -> str:
    if report.status != "stuck_input":
        return ""
    try:
        latest = wait_while_compacting(target, n_lines, compaction_wait_timeout_s)
    except TimeoutError:
        return "compacting"
    if latest.status != "stuck_input":
        return "not_stuck"
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent_enter" if result.returncode == 0 else "failed"


def status(lines: list[str], block: Block) -> str:
    if not lines:
        return "not_codex"
    if not has_codex_model_footer(lines):
        return "running" if has_queued_running_input(lines) else "not_codex"
    if has_queued_running_input(lines):
        return "running"
    input_text = current_input_text(lines)
    if input_text and not is_empty_input_text(lines, input_text):
        return "stuck_input"
    if has_running_indicator(lines):
        return "running"
    if block.has_footer or any(READY_RE.match(line) is not None or INPUT_RE.match(line) is not None for line in lines[-10:]):
        return "ready"
    text = "\n".join(block.lines or lines[-20:])
    if ERROR_RE.search(text) is not None:
        return "error"
    return "running"


def report_from_lines(lines: list[str]) -> Report:
    block = current_block(lines)
    return Report(status(lines, block), block.lines, current_input_text(lines), can_submit_stuck_input(lines))


def inspect(args: Args) -> Report:
    return report_from_lines(tail(args.target, args.n_lines))


def wait_while_compacting(target: str, n_lines: int = COMPACTION_WAIT_LINES, timeout_s: float = DEFAULT_COMPACTION_WAIT_TIMEOUT_S, interval_s: float = COMPACTION_WAIT_INTERVAL_S) -> Report:
    deadline_s = time.monotonic() + timeout_s
    latest = Report("unknown", [])
    while True:
        lines = tail(target, n_lines)
        latest = report_from_lines(lines)
        if not has_compacting_indicator(lines):
            return latest
        if time.monotonic() >= deadline_s:
            raise TimeoutError(f"target still compacting after {timeout_s:g}s")
        time.sleep(min(interval_s, max(0.05, deadline_s - time.monotonic())))


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
