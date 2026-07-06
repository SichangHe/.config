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
CODEX_FOOTER_RE = re.compile(r"^  gpt-")
ERROR_RE = re.compile(r"\b(failed|panic|traceback|exception)\b|\berror\b(?!\s*=\s*\d)", re.IGNORECASE)
SELECTED_MODEL_CAPACITY_RE = re.compile(r"^\s*(?:⚠\ufe0f?\s*)?Selected model is at capacity\. Please try a different model\.\s*$")
VISIBLE_ERROR_MARKER_RE = re.compile(r"^\s*(?:[■□▢▣▪▫◼◻▰▱▮▯]\s*|⚠\ufe0f?\s*)")
SEP_RE = re.compile(r"^─+$")
WORKED_RE = re.compile(r"^─ Worked for .+ ─+$")
READY_RE = re.compile(r"^› Use /skills to list available skills$")
INPUT_RE = re.compile(r"^› ")
BUSY_RE = re.compile(r"^• (?:Working|Messages to be submitted after next tool call)\b")
COMPACTING_RE = re.compile(r"^• Compacting\b", re.IGNORECASE)
BACKGROUND_RUNNING_RE = re.compile(r"^• .*?\b(?:Waiting for background terminal|[1-9][0-9]* background terminals? running)\b")
QUEUE_MESSAGE_FOOTER_RE = re.compile(r"\btab to queue message\b")
TERMINAL_ENTER_PROMPT_RE = re.compile(r"^\s*\[?press enter(?:/return)?(?: to continue)?(?:\.\.\.)?\]?\s*$", re.IGNORECASE)
PLAN_PROMPT_RE = re.compile(r"\bCreate a plan\?\s+shift\s+\+\s+tab\s+use Plan mode\s+esc dismiss\s*$")
WAITING_FOR_SUBAGENT_RE = re.compile(r"^• Waiting for [0-9a-fA-F][0-9a-fA-F-]{15,}$")
WORKING_INTERRUPT_RE = re.compile(r"^• Working \([^)]* • esc to interrupt\)$")
QUEUED_AFTER_TOOL_CALL_RE = re.compile(r"^• Messages to be submitted after next tool call \(press esc to interrupt and send immediately\)$")
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
    input_blocker: str = ""


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


def has_terminal_enter_prompt_after_codex_footer(lines: list[str]) -> bool:
    visible = [line.rstrip() for line in lines if line.strip()]
    return (
        len(visible) >= 3
        and TERMINAL_ENTER_PROMPT_RE.match(visible[-1]) is not None
        and CODEX_FOOTER_RE.match(visible[-2]) is not None
        and WORKED_RE.match(visible[-3]) is not None
    )


def has_plan_prompt(lines: list[str]) -> bool:
    return any(PLAN_PROMPT_RE.search(line) is not None for line in lines[-10:])


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


def final_assistant_output(lines: list[str]) -> list[str]:
    body = lines[:-1] if has_codex_model_footer(lines) or has_queued_message_footer(lines) else lines[:]
    worked_idx = -1
    for idx in range(len(body) - 1, -1, -1):
        if WORKED_RE.match(body[idx]):
            worked_idx = idx
            break
    if worked_idx < 0:
        return []
    start = 0
    for idx in range(worked_idx - 1, -1, -1):
        if SEP_RE.match(body[idx]) or WORKED_RE.match(body[idx]):
            start = idx + 1
            break
    output = [line.rstrip() for line in body[start:worked_idx]]
    while output and not output[0]:
        del output[0]
    while output and not output[-1]:
        output.pop()
    return output


def report_output(lines: list[str], block: Block, report_status: str) -> list[str]:
    if report_status == "ready":
        return final_assistant_output(lines)
    return block.lines


def current_input_text(lines: list[str]) -> str:
    body = lines[:-1] if has_codex_model_footer(lines) or has_queued_message_footer(lines) else lines[:]
    while body and not body[-1].strip():
        body.pop()
    for idx in range(len(body) - 1, -1, -1):
        line = body[idx].lstrip()
        if line.startswith("›"):
            input_lines = body[idx:]
            if any(after.startswith(("• ", "│", "└", "├", "─")) for after in input_lines[1:]):
                return ""
            while input_lines[1:] and (not input_lines[-1].strip() or PLAN_PROMPT_RE.search(input_lines[-1]) is not None):
                input_lines.pop()
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


def has_waiting_subagent_prompt(lines: list[str]) -> bool:
    """Detect a running Codex turn blocked in a parent-side subagent wait."""

    if not has_codex_model_footer(lines):
        return False
    body = lines[:-1]
    start = 0
    for idx in range(len(body) - 1, -1, -1):
        if SEP_RE.match(body[idx]):
            start = idx + 1
            break
    body = [line.rstrip() for line in body[start:]]
    while body and not body[0]:
        del body[0]
    while body and not body[-1]:
        body.pop()
    if body and WORKED_RE.match(body[-1]):
        return False
    for idx in range(len(body) - 1, -1, -1):
        if WORKED_RE.match(body[idx]):
            body = body[idx + 1 :]
            break
    for idx in range(len(body) - 2):
        if WAITING_FOR_SUBAGENT_RE.match(body[idx].rstrip()) and WORKING_INTERRUPT_RE.match(body[idx + 1].rstrip()) and QUEUED_AFTER_TOOL_CALL_RE.match(body[idx + 2].rstrip()):
            return True
    return False


def has_queued_running_input(lines: list[str]) -> bool:
    return has_queued_message_footer(lines) and has_visible_running_indicator(lines)


def has_idle_queued_input(lines: list[str], input_text: str) -> bool:
    return has_queued_message_footer(lines) and bool(input_text) and not has_visible_running_indicator(lines)


def has_selected_model_capacity_warning(lines: list[str]) -> bool:
    output_lines: list[str] = []
    for line in lines:
        if line.lstrip().startswith("›"):
            break
        output_lines.append(line)
    return any(SELECTED_MODEL_CAPACITY_RE.search(line) is not None for line in output_lines)


def visible_error_lines(lines: list[str], include_unmarked: bool = True) -> list[str]:
    found: list[str] = []
    for line in lines:
        if line.lstrip().startswith("›"):
            break
        marked = VISIBLE_ERROR_MARKER_RE.search(line) is not None
        if SELECTED_MODEL_CAPACITY_RE.search(line) is not None or (ERROR_RE.search(line) is not None and (include_unmarked or marked)):
            found.append(line.strip())
    return found


def current_input_follows_running_indicator(lines: list[str]) -> bool:
    body = lines[:-1] if has_codex_model_footer(lines) else lines[:]
    input_idx = -1
    for idx in range(len(body) - 1, -1, -1):
        if body[idx].lstrip().startswith("›"):
            input_idx = idx
            break
    if input_idx < 0:
        return False
    for line in reversed(body[:input_idx]):
        if SEP_RE.match(line):
            return False
        if BUSY_RE.search(line) is not None or BACKGROUND_RUNNING_RE.search(line) is not None or COMPACTING_RE.search(line) is not None:
            return True
        if WORKED_RE.match(line):
            return False
    return False


def is_empty_input_text(lines: list[str], input_text: str) -> bool:
    return is_stock_placeholder_input_text(input_text)


def is_stock_placeholder_input_text(input_text: str) -> bool:
    return input_text in CODEX_EMPTY_INPUT_TEXTS or input_text in CODEX_RUNNING_EMPTY_INPUT_TEXTS


def can_submit_stuck_input(lines: list[str]) -> bool:
    if has_terminal_enter_prompt_after_codex_footer(lines):
        return True
    if has_plan_prompt(lines):
        input_text = current_input_text(lines)
        return bool(input_text and not is_empty_input_text(lines, input_text))
    if has_queued_running_input(lines) or has_compacting_indicator(lines):
        return False
    input_text = current_input_text(lines)
    return bool((has_codex_model_footer(lines) or has_idle_queued_input(lines, input_text)) and input_text and not is_empty_input_text(lines, input_text))


def stuck_input_blocker(lines: list[str], input_text: str) -> str:
    if has_terminal_enter_prompt_after_codex_footer(lines):
        return ""
    if has_plan_prompt(lines):
        if not input_text:
            return "empty_input"
        if is_empty_input_text(lines, input_text):
            return "placeholder_input"
        return ""
    if not has_codex_model_footer(lines) and not has_idle_queued_input(lines, input_text):
        return "no_codex_footer"
    if has_compacting_indicator(lines):
        return "compacting"
    if has_queued_running_input(lines):
        return "queued_running_input"
    if not input_text:
        return "empty_input"
    if is_empty_input_text(lines, input_text):
        return "placeholder_input"
    return ""


def submit_stuck_input_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES, compaction_wait_timeout_s: float = DEFAULT_COMPACTION_WAIT_TIMEOUT_S) -> str:
    if report.status != "stuck_input":
        return ""
    try:
        latest = wait_while_compacting(target, n_lines, compaction_wait_timeout_s)
    except TimeoutError:
        return "not_safe:compacting"
    if latest.status != "stuck_input":
        return "not_stuck"
    if not latest.can_submit_input:
        return f"not_safe:{latest.input_blocker or 'unknown'}"
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent_enter" if result.returncode == 0 else "failed"


def interrupt_waiting_subagent_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES) -> str:
    if report.status != "waiting_subagent":
        return ""
    latest = report_from_lines(tail(target, n_lines), detect_waiting_subagent=True)
    if latest.status != "waiting_subagent":
        return "not_waiting_subagent"
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", target, "Escape"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent_escape" if result.returncode == 0 else "failed"


def status(lines: list[str], block: Block, *, detect_waiting_subagent: bool = False) -> str:
    if not lines:
        return "not_codex"
    if detect_waiting_subagent and has_waiting_subagent_prompt(lines):
        return "waiting_subagent"
    if not has_codex_model_footer(lines):
        if has_terminal_enter_prompt_after_codex_footer(lines):
            return "stuck_input"
        if has_plan_prompt(lines):
            return "stuck_input"
        if has_queued_running_input(lines):
            return "running"
        input_text = current_input_text(lines)
        if has_idle_queued_input(lines, input_text):
            return "ready" if is_stock_placeholder_input_text(input_text) else "stuck_input"
        return "not_codex"
    if has_selected_model_capacity_warning(block.lines or lines[-20:]):
        input_text = current_input_text(lines)
        if input_text and not is_stock_placeholder_input_text(input_text):
            return "stuck_input"
        return "error"
    if has_queued_running_input(lines):
        return "running"
    input_text = current_input_text(lines)
    if visible_error_lines(block.lines or lines[-20:], include_unmarked=False):
        return "error"
    if has_plan_prompt(lines):
        return "stuck_input"
    if input_text and not is_empty_input_text(lines, input_text):
        return "stuck_input"
    if input_text in CODEX_RUNNING_EMPTY_INPUT_TEXTS and current_input_follows_running_indicator(lines):
        return "running"
    if not input_text and block.has_footer:
        return "ready"
    if has_running_indicator(lines) and (not is_stock_placeholder_input_text(input_text) or current_input_follows_running_indicator(lines) or has_compacting_indicator(lines)):
        return "running"
    if block.has_footer or any(READY_RE.match(line) is not None or INPUT_RE.match(line) is not None for line in lines[-10:]):
        return "ready"
    if visible_error_lines(block.lines or lines[-20:]):
        return "error"
    return "running"


def report_from_lines(lines: list[str], *, detect_waiting_subagent: bool = False) -> Report:
    block = current_block(lines)
    input_text = current_input_text(lines)
    can_submit_input = can_submit_stuck_input(lines)
    report_status = status(lines, block, detect_waiting_subagent=detect_waiting_subagent)
    input_blocker = stuck_input_blocker(lines, input_text) if report_status == "stuck_input" and not can_submit_input else ""
    return Report(report_status, report_output(lines, block, report_status), input_text, can_submit_input, input_blocker)


def inspect(args: Args, *, detect_waiting_subagent: bool = False) -> Report:
    return report_from_lines(tail(args.target, args.n_lines), detect_waiting_subagent=detect_waiting_subagent)


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
