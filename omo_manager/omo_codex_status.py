#!/usr/bin/env python3
"""Report Codex or Cursor Agent TUI status from one tmux window tail."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_COMPACTION_WAIT_TIMEOUT_S = float(os.environ.get("OMO_CODEX_COMPACTION_WAIT_TIMEOUT_S", "300"))
COMPACTION_WAIT_INTERVAL_S = 0.5
FILE_SEARCH_RECOVERY_INTERVAL_S = 0.05
COMPACTION_WAIT_LINES = 2000
CODEX_RE = re.compile(r"  gpt-")
CODEX_FOOTER_RE = re.compile(r"^  gpt-")
ERROR_RE = re.compile(r"\b(failed|panic|traceback|exception)\b|\berror\b(?!\s*=\s*\d)", re.IGNORECASE)
SELECTED_MODEL_CAPACITY_RE = re.compile(
    r"^\s*(?:(?:⚠\ufe0f?\s*)?Selected model is at capacity\. Please try a different model\.|■\s*\{\"detail\":\"The '[A-Za-z0-9][A-Za-z0-9._-]*' model is not supported when using Codex with a ChatGPT account\.\"\})\s*$"
)
CONTENT_HIDDEN_RE = re.compile(r"^\s*ⓘ\s+This content can(?:not|['’]t) be shown[.!?]?\s*$", re.IGNORECASE)
CURSOR_USAGE_LIMIT_RE = re.compile(
    r"^\s*(?:Error:\s+Increase limits for faster responses|You're out of usage\. Switch to Auto, or ask your admin to increase your limit to continue\.)\s*$",
    re.IGNORECASE,
)
UNRELATED_FATAL_LINE_RE = re.compile(r"^\s*(?:(?:[A-Za-z][\w-]*\s+)?failed\b|error\b|exception\b|fatal\b|panic\b|traceback\b)", re.I)
WAKE_EXECUTION_BUDGET_REFUSAL_RE = re.compile(
    r"^\s*(?:•\s*)?I (?:can(?:not|[’']t)|am unable to) safely (?:complete|handle|execute) (?:(?:another|the|this|a) )?wake prompt (?:in|within) the remaining execution (?:budget|time)(?: available)?[.!]?\s*$",
    re.IGNORECASE,
)
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
PLAN_PROMPT_RE = re.compile(r"^\s*Create a plan\?\s+shift\s+\+\s+tab\s+use Plan mode\s+esc dismiss\s*$")
RESUME_PAUSED_GOAL_RE = re.compile(r"^\s*Resume (?:the )?paused goal\?\s*$", re.IGNORECASE)
PAUSED_GOAL_RE = re.compile(r"^\s*Goal:\s+\S", re.IGNORECASE)
RESUME_GOAL_CHOICE_RE = re.compile(r"^\s*(?P<selected>›\s*)?1\.\s+Resume goal\b", re.IGNORECASE)
LEAVE_PAUSED_CHOICE_RE = re.compile(r"^\s*(?P<selected>›\s*)?2\.\s+Leave paused\b", re.IGNORECASE)
CHOICE_CONFIRM_RE = re.compile(r"^\s*Press (?:enter|return) to confirm or esc(?:ape)? to go back\s*$", re.IGNORECASE)
SKILLS_TITLE_RE = re.compile(r"^\s*Skills\s*$")
SKILLS_ACTION_RE = re.compile(r"^\s*Choose an action\s*$")
SKILLS_LIST_RE = re.compile(r"^\s*›\s*1\.\s+List skills(?:\s+Tip: press @ to open this list directly\.)?\s*$")
SKILLS_TOGGLE_RE = re.compile(r"^\s*2\.\s+Enable/Disable Skills(?:\s+Enable or disable skills\.)?\s*$")
SESSION_MODEL_RESUME_RE = re.compile(r"\bThis session (?:was recorded|started) with model\b.+?\bis resuming with\b", re.IGNORECASE)
FILE_SEARCH_NO_MATCHES_RE = re.compile(r"^\s*no matches\s*$", re.IGNORECASE)
FILE_SEARCH_HELP_RE = re.compile(r"\benter insert\s*·\s*esc close\s*·\s*←/→ switch search modes\b")
FILE_SEARCH_MODES_RE = re.compile(r"\[All Results\]\s+Filesystem Only\s+Plugins\s*$")
WAITING_FOR_SUBAGENT_RE = re.compile(r"^• Waiting for [0-9a-fA-F][0-9a-fA-F-]{15,}$")
WORKING_INTERRUPT_RE = re.compile(r"^• Working \([^)]* • esc to interrupt\)$")
QUEUED_AFTER_TOOL_CALL_RE = re.compile(r"^• Messages to be submitted after next tool call \(press esc to interrupt and send immediately\)$")
CODEX_EMPTY_INPUT_TEXTS = {
    "Ask Codex to do anything",
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
CURSOR_AGENT_EMPTY_INPUT_TEXTS = {
    "Add a follow-up",
}
CURSOR_AGENT_FOOTER_RE = re.compile(r"^\s*Cursor \S.+·\s*[0-9]+(?:\.[0-9]+)?%")
CURSOR_AGENT_INPUT_PREFIX_RE = re.compile(r"^\s*→ ")
CURSOR_AGENT_STOP_HINT_RE = re.compile(r"[ \t]+ctrl\+c to stop\s*$")
CURSOR_AGENT_COMPOSER_BOTTOM_RE = re.compile(r"^\s*▀+\s*$")
CURSOR_AGENT_TASK_COUNT_RE = re.compile(r"^\s*[1-9]\d* tasks?\s*$")
CURSOR_FOLLOWUPS_HEADER_RE = re.compile(r"┌─ follow-ups")
CURSOR_FOLLOWUPS_SEND_NOW_RE = re.compile(r"enter send now", re.IGNORECASE)
TMUX_TARGET_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?$")
TMUX_PANE_ID_RE = re.compile(r"^%[0-9]+$")


@dataclass(frozen=True)
class Args:
    target: str
    n_lines: int
    dismiss_skills_menu: bool = False


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


@dataclass(frozen=True)
class PlanPromptRecovery:
    action: str
    before: str
    after: str


class ParsedArgs(argparse.Namespace):
    target: str = ""
    n_lines: int = 80
    dismiss_skills_menu: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("target")
    _ = parser.add_argument("--lines", type=int, default=80)
    _ = parser.add_argument("--dismiss-skills-menu", action="store_true", help="dismiss the exact active Codex Skills menu with one Escape")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.n_lines <= 0:
        parser.error("--lines must be positive.")
    return Args(parsed.target, parsed.n_lines, parsed.dismiss_skills_menu)


def exact_pane_id(target: str) -> str:
    """Return the pane id only when tmux resolves the exact numeric target."""
    match = TMUX_TARGET_RE.fullmatch(target)
    if match is None:
        return ""
    session, window, pane = match.group(1), match.group(2), match.group(3) or "0"
    canonical = f"{session}:{int(window)}.{int(pane)}"
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", canonical, "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    resolved, separator, pane_id = out.stdout.strip().partition("\t")
    return pane_id if separator and resolved == canonical and pane_id.startswith("%") else ""


def tail_pane_id(pane_id: str, n_lines: int) -> list[str]:
    if TMUX_PANE_ID_RE.fullmatch(pane_id) is None:
        return []
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", f"-{n_lines}"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    lines = [line.rstrip() for line in (out.stdout or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def tail(target: str, n_lines: int) -> list[str]:
    pane_id = exact_pane_id(target)
    return tail_pane_id(pane_id, n_lines) if pane_id else []


def exact_tail(target: str, n_lines: int) -> tuple[bool, list[str]]:
    """Capture one exact pane and reject disappearance or target rebinding."""

    pane_id = exact_pane_id(target)
    if not pane_id:
        return False, []
    lines = tail_pane_id(pane_id, n_lines)
    return (True, lines) if exact_pane_id(target) == pane_id else (False, [])


def exact_pane_process(target: str, pane_id: str) -> tuple[str, list[str]] | None:
    """Return the exact pane command and parsed start command."""

    if TMUX_PANE_ID_RE.fullmatch(pane_id) is None:
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}\t#{pane_current_command}\t#{pane_start_command}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or exact_pane_id(target) != pane_id:
        return None
    resolved_id, separator, process = out.stdout.rstrip("\r\n").partition("\t")
    current_command, separator, start_command = process.partition("\t")
    if resolved_id != pane_id or not separator:
        return None
    try:
        start_tokens = shlex.split(start_command)
        if len(start_tokens) == 1 and start_tokens[0] != start_command:
            start_tokens = shlex.split(start_tokens[0])
    except ValueError:
        return None
    if "exec" in start_tokens:
        start_tokens = start_tokens[start_tokens.index("exec") + 1 :]
    return current_command, start_tokens


def pane_has_exact_codex_process(target: str, pane_id: str) -> bool:
    """Confirm that an exact pane is still running a known Codex launcher."""

    process = exact_pane_process(target, pane_id)
    if process is None:
        return False
    current_command, start_tokens = process
    if current_command == "codex":
        return bool(start_tokens and os.path.basename(start_tokens[0]) == "codex")
    return current_command in {"bunx", "npx"} and len(start_tokens) >= 2 and os.path.basename(start_tokens[0]) == current_command and start_tokens[1] == "@openai/codex"


def pane_has_exact_cursor_process(target: str, pane_id: str) -> bool:
    """Confirm that an exact pane is still running Cursor Agent CLI."""

    process = exact_pane_process(target, pane_id)
    if process is None:
        return False
    current_command, start_tokens = process
    return current_command == "agent" and (not start_tokens or os.path.basename(start_tokens[0]) == "agent")


def pane_has_exact_managed_agent_process(target: str, pane_id: str) -> bool:
    return pane_has_exact_codex_process(target, pane_id) or pane_has_exact_cursor_process(target, pane_id)


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


def has_active_plan_prompt(lines: list[str]) -> bool:
    """Match only the terminal `Create a plan? ... esc dismiss` modal."""

    visible = [line.rstrip() for line in lines if line.strip()]
    matches = [index for index, line in enumerate(visible) if PLAN_PROMPT_RE.search(line) is not None]
    return matches == [len(visible) - 1]


def plan_prompt_classification(lines: list[str]) -> str:
    if not lines:
        return "capture_failed"
    if has_active_plan_prompt(lines) and report_from_lines(lines).status == "stuck_input":
        return "plan_prompt"
    return report_from_lines(lines).status


def has_active_skills_menu(lines: list[str]) -> bool:
    """Match only the complete Skills choice menu at the bottom of the pane."""

    visible = [line.rstrip() for line in lines if line.strip()]
    if len(visible) < 5:
        return False
    menu = visible[-5:]
    return all(
        pattern.fullmatch(line) is not None
        for pattern, line in zip(
            (SKILLS_TITLE_RE, SKILLS_ACTION_RE, SKILLS_LIST_RE, SKILLS_TOGGLE_RE, CHOICE_CONFIRM_RE),
            menu,
            strict=True,
        )
    )


def skills_menu_classification(lines: list[str]) -> str:
    if not lines:
        return "capture_failed"
    if has_active_skills_menu(lines):
        return "skills_menu"
    return report_from_lines(lines).status


def resume_paused_goal_selection(lines: list[str]) -> str:
    """Return the selected action for the currently visible paused-goal chooser."""

    visible = [line.rstrip() for line in lines if line.strip()]
    if not visible or CHOICE_CONFIRM_RE.fullmatch(visible[-1]) is None:
        return ""
    prompt_start = max(0, len(visible) - 30)
    prompt_idx = next((idx for idx in range(len(visible) - 2, prompt_start - 1, -1) if RESUME_PAUSED_GOAL_RE.fullmatch(visible[idx]) is not None), -1)
    if prompt_idx < 0:
        return ""
    capacity_idx = next((idx for idx in range(prompt_idx - 1, prompt_start - 1, -1) if SELECTED_MODEL_CAPACITY_RE.fullmatch(visible[idx]) is not None), -1)
    if capacity_idx < 0 or SESSION_MODEL_RESUME_RE.search(" ".join(line.strip() for line in visible[capacity_idx + 1 : prompt_idx])) is None:
        return ""
    goal_idx = next((idx for idx in range(prompt_idx + 1, len(visible) - 1) if PAUSED_GOAL_RE.match(visible[idx]) is not None), -1)
    if goal_idx < 0:
        return ""
    resume_idx = next((idx for idx in range(goal_idx + 1, len(visible) - 1) if RESUME_GOAL_CHOICE_RE.match(visible[idx]) is not None), -1)
    if resume_idx < 0:
        return ""
    leave_idx = next((idx for idx in range(resume_idx + 1, len(visible) - 1) if LEAVE_PAUSED_CHOICE_RE.match(visible[idx]) is not None), -1)
    if leave_idx < 0:
        return ""
    resume_match = RESUME_GOAL_CHOICE_RE.match(visible[resume_idx])
    leave_match = LEAVE_PAUSED_CHOICE_RE.match(visible[leave_idx])
    resume_selected = resume_match is not None and bool(resume_match.group("selected"))
    leave_selected = leave_match is not None and bool(leave_match.group("selected"))
    if resume_selected == leave_selected:
        return "unknown"
    if resume_selected:
        return "resume"
    if leave_selected:
        return "leave"
    raise AssertionError("unreachable")


def has_resume_paused_goal_prompt(lines: list[str]) -> bool:
    return bool(resume_paused_goal_selection(lines))


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


def has_cursor_agent_footer(lines: list[str]) -> bool:
    return any(CURSOR_AGENT_FOOTER_RE.search(line) is not None for line in lines[-8:])


def is_cursor_agent_capture(lines: list[str]) -> bool:
    return has_cursor_agent_footer(lines) and not has_codex_model_footer(lines)


def has_cursor_agent_running_indicator(lines: list[str]) -> bool:
    if not has_cursor_agent_footer(lines):
        return False
    if any(
        CURSOR_AGENT_INPUT_PREFIX_RE.match(line) is not None and CURSOR_AGENT_STOP_HINT_RE.search(line) is not None
        for line in lines[-12:]
    ):
        return True
    footer_idx = next((idx for idx in range(len(lines) - 1, -1, -1) if CURSOR_AGENT_FOOTER_RE.search(lines[idx]) is not None), -1)
    bottom_idx = next((idx for idx in range(footer_idx - 1, -1, -1) if CURSOR_AGENT_COMPOSER_BOTTOM_RE.match(lines[idx]) is not None), -1) if footer_idx > 0 else -1
    return bottom_idx >= 0 and footer_idx > bottom_idx and any(
        CURSOR_AGENT_TASK_COUNT_RE.fullmatch(lines[idx]) is not None for idx in range(bottom_idx + 1, footer_idx)
    )


def cursor_agent_input_text(lines: list[str]) -> str:
    if not has_cursor_agent_footer(lines):
        return ""
    prompt_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        if CURSOR_AGENT_INPUT_PREFIX_RE.match(lines[idx]) is None:
            continue
        if any(CURSOR_AGENT_FOOTER_RE.search(after) is not None for after in lines[idx + 1 :]):
            prompt_idx = idx
            break
    if prompt_idx < 0:
        return ""
    end = prompt_idx + 1
    while end < len(lines) and CURSOR_AGENT_COMPOSER_BOTTOM_RE.match(lines[end]) is None and CURSOR_AGENT_FOOTER_RE.search(lines[end]) is None:
        end += 1
    chunks: list[str] = []
    for offset, raw in enumerate(lines[prompt_idx:end]):
        stripped_hint = CURSOR_AGENT_STOP_HINT_RE.sub("", raw)
        if offset == 0:
            match = CURSOR_AGENT_INPUT_PREFIX_RE.match(stripped_hint)
            if match is None:
                return ""
            chunks.append(stripped_hint[match.end() :].rstrip())
            continue
        chunks.append(stripped_hint.rstrip())
    while chunks and not chunks[-1].strip():
        chunks.pop()
    return "\n".join(chunks).strip()


def has_cursor_followups_overlay(lines: list[str]) -> bool:
    if not has_cursor_agent_footer(lines):
        return False
    return any(CURSOR_FOLLOWUPS_HEADER_RE.search(line) is not None for line in lines) and any(CURSOR_FOLLOWUPS_SEND_NOW_RE.search(line) is not None for line in lines)


def current_input_text(lines: list[str]) -> str:
    if is_cursor_agent_capture(lines):
        return cursor_agent_input_text(lines)
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


def file_search_overlay_input_text(lines: list[str]) -> str:
    visible = [(idx, line.rstrip()) for idx, line in enumerate(lines) if line.strip()]
    if len(visible) < 3:
        return ""
    if FILE_SEARCH_HELP_RE.search(visible[-1][1]) is not None and FILE_SEARCH_MODES_RE.search(visible[-1][1]) is not None:
        no_matches = visible[-2]
    elif len(visible) >= 4 and FILE_SEARCH_HELP_RE.search(visible[-2][1]) is not None and FILE_SEARCH_MODES_RE.fullmatch(visible[-1][1]) is not None:
        no_matches = visible[-3]
    else:
        return ""
    if FILE_SEARCH_NO_MATCHES_RE.fullmatch(no_matches[1]) is None:
        return ""
    prompt_idx = -1
    for idx in range(no_matches[0] - 1, -1, -1):
        if lines[idx].lstrip().startswith("›"):
            prompt_idx = idx
            break
    if prompt_idx < 0:
        return ""
    prompt_lines = lines[prompt_idx : no_matches[0]]
    text = "\n".join([prompt_lines[0].lstrip()[1:].strip(), *(line.rstrip() for line in prompt_lines[1:])]).strip()
    return text if "@filename" in text else ""


def has_file_search_overlay(lines: list[str]) -> bool:
    return bool(file_search_overlay_input_text(lines))


def has_running_indicator(lines: list[str]) -> bool:
    return has_compacting_indicator(lines) or any(BUSY_RE.search(line) is not None or BACKGROUND_RUNNING_RE.search(line) is not None for line in lines[-20:])


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
    return has_queued_message_footer(lines) and current_input_follows_running_indicator(lines)


def has_idle_queued_input(lines: list[str], input_text: str) -> bool:
    return has_queued_message_footer(lines) and bool(input_text) and not current_input_follows_running_indicator(lines)


def latest_output_before_input(lines: list[str]) -> list[str]:
    cursor = is_cursor_agent_capture(lines)
    input_indices = [idx for idx, line in enumerate(lines) if line.lstrip().startswith("›") or (cursor and CURSOR_AGENT_INPUT_PREFIX_RE.match(line) is not None)]
    if input_indices:
        latest_input_idx = input_indices[-1]
        previous_input_idx = input_indices[-2] if len(input_indices) > 1 else -1
        output_start_idx = previous_input_idx + 1
        for idx in range(latest_input_idx - 1, output_start_idx - 1, -1):
            if WORKED_RE.match(lines[idx]):
                output_start_idx = idx + 1
                break
        return lines[output_start_idx:latest_input_idx]
    return lines


def has_selected_model_capacity_warning(lines: list[str]) -> bool:
    return any(SELECTED_MODEL_CAPACITY_RE.search(line) is not None for line in latest_output_before_input(lines))


def cursor_usage_limit_lines(lines: list[str]) -> list[str]:
    if not has_cursor_agent_footer(lines):
        return []
    footer_idx = next((idx for idx in range(len(lines) - 1, -1, -1) if CURSOR_AGENT_FOOTER_RE.search(lines[idx]) is not None), -1)
    if footer_idx < 0:
        return []
    bottom_idx = next((idx for idx in range(footer_idx - 1, -1, -1) if CURSOR_AGENT_COMPOSER_BOTTOM_RE.match(lines[idx]) is not None), -1)
    if bottom_idx < 0:
        return []
    return [line.strip() for line in lines[bottom_idx + 1 :] if CURSOR_USAGE_LIMIT_RE.search(line) is not None]


def ignorable_codex_apps_transport_lines(lines: list[str]) -> set[int]:
    """Return complete wrapped `codex_apps` no-account warning lines."""

    ignored: set[int] = set()
    start_index: int | None = None
    for index, line in enumerate(lines):
        compact_line = re.sub(r"\s+", "", line).replace("\ufe0f", "").casefold()
        if compact_line.startswith("⚠mcpclientfor"):
            start_index = index
            continue
        if start_index is None:
            continue
        warning_lines = lines[start_index : index + 1]
        warning = "\n".join(warning_lines)
        compact_warning = re.sub(r"\s+", "", warning).replace("\ufe0f", "").casefold()
        if not compact_warning.endswith("⚠mcpstartupincomplete(failed:codex_apps)"):
            continue
        statuses = re.findall(r"http(\d{3})", compact_warning)
        inner_lines = warning_lines[1:-1]
        has_unrelated_marked_line = any(
            VISIBLE_ERROR_MARKER_RE.search(inner) is not None and "mcp startup incomplete" not in inner.casefold()
            for inner in inner_lines
        )
        has_unrelated_fatal_line = any(UNRELATED_FATAL_LINE_RE.search(inner) is not None for inner in inner_lines)
        residual = compact_warning
        for expected in (
            "`codex_apps`failedtostart",
            "mcpstartupfailed",
            "mcpserverfailed",
            "sendmessageerrortransport",
            "]error:unexpected",
            '{"error":',
            '"type":"proxy_error"',
            "(failed:codex_apps)",
        ):
            residual = residual.replace(expected, "", 1)
        has_unrelated_fatal_text = re.search(r"(?:error|failed|exception|fatal|panic|traceback)", residual) is not None
        if (
            compact_warning.startswith("⚠mcpclientfor`codex_apps`failedtostart:")
            and "sendmessageerrortransport[rmcp::transport::worker::workertransport" in compact_warning
            and "noavailableaccounts" in compact_warning
            and statuses
            and set(statuses) == {"401"}
            and compact_warning.count("mcpclientfor") == 1
            and compact_warning.count("⚠") == 2
            and not has_unrelated_marked_line
            and not has_unrelated_fatal_line
            and not has_unrelated_fatal_text
        ):
            ignored.update(range(start_index, index + 1))
        start_index = None
    return ignored


def visible_error_lines(lines: list[str], include_unmarked: bool = True, *, allow_cursor_quota: bool = True) -> list[str]:
    if allow_cursor_quota and is_cursor_agent_capture(lines):
        return cursor_usage_limit_lines(lines)
    found: list[str] = []
    output = latest_output_before_input(lines)
    ignorable_transport = ignorable_codex_apps_transport_lines(output)
    for index, line in enumerate(output):
        if index in ignorable_transport:
            continue
        marked = VISIBLE_ERROR_MARKER_RE.search(line) is not None
        if (
            CONTENT_HIDDEN_RE.search(line) is not None
            or SELECTED_MODEL_CAPACITY_RE.search(line) is not None
            or WAKE_EXECUTION_BUDGET_REFUSAL_RE.search(line) is not None
            or (ERROR_RE.search(line) is not None and (include_unmarked or marked))
        ):
            stripped = line.strip()
            if stripped not in found:
                found.append(stripped)
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
    return input_text in CODEX_EMPTY_INPUT_TEXTS or input_text in CODEX_RUNNING_EMPTY_INPUT_TEXTS or input_text in CURSOR_AGENT_EMPTY_INPUT_TEXTS


def can_submit_stuck_input(lines: list[str]) -> bool:
    if has_file_search_overlay(lines):
        return True
    if resume_paused_goal_selection(lines) == "resume":
        return True
    if has_terminal_enter_prompt_after_codex_footer(lines):
        return True
    if has_plan_prompt(lines):
        input_text = current_input_text(lines)
        return bool(input_text and not is_empty_input_text(lines, input_text))
    if has_queued_running_input(lines) or has_compacting_indicator(lines):
        return False
    if has_cursor_followups_overlay(lines):
        return True
    input_text = current_input_text(lines)
    return bool(
        (has_codex_model_footer(lines) or has_cursor_agent_footer(lines) or has_idle_queued_input(lines, input_text))
        and input_text
        and not is_empty_input_text(lines, input_text)
    )


def stuck_input_blocker(lines: list[str], input_text: str) -> str:
    if has_file_search_overlay(lines):
        return ""
    if has_resume_paused_goal_prompt(lines):
        return "resume_goal_not_selected"
    if has_terminal_enter_prompt_after_codex_footer(lines):
        return ""
    if has_plan_prompt(lines):
        if not input_text:
            return "empty_input"
        if is_empty_input_text(lines, input_text):
            return "placeholder_input"
        return ""
    if not has_codex_model_footer(lines) and not has_cursor_agent_footer(lines) and not has_idle_queued_input(lines, input_text):
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
    pane_id = exact_pane_id(target)
    if not pane_id:
        return "failed"
    if has_file_search_overlay(latest.lines):
        try:
            recovered = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return "failed"
        if recovered.returncode != 0:
            return "failed"
        try:
            after = wait_for_file_search_overlay_transition(target, n_lines, compaction_wait_timeout_s)
        except TimeoutError:
            return "not_safe:file_search_overlay"
        if after.status in {"running", "waiting_subagent", "ready"}:
            return "sent_enter"
        if has_plan_prompt(after.lines):
            return "not_safe:plan_prompt"
        if after.status != "stuck_input" or not after.can_submit_input:
            return f"not_safe:{after.input_blocker or 'underlying_prompt_not_visible'}"
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent_enter" if result.returncode == 0 else "failed"


def interrupt_waiting_subagent_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES) -> str:
    if report.status != "waiting_subagent":
        return ""
    latest = report_from_lines(tail(target, n_lines), detect_waiting_subagent=True)
    if latest.status != "waiting_subagent":
        return "not_waiting_subagent"
    pane_id = exact_pane_id(target)
    if not pane_id:
        return "failed"
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent_escape" if result.returncode == 0 else "failed"


def dismiss_plan_prompt_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES) -> PlanPromptRecovery:
    """Send one Escape after exact, fresh verification of the plan modal."""

    before = "plan_prompt" if report.status == "stuck_input" and has_active_plan_prompt(report.lines) else report.status
    match = TMUX_TARGET_RE.fullmatch(target)
    if match is None:
        return PlanPromptRecovery("not_safe:ambiguous_target", before, "not_checked")
    if match.group(1).startswith("h"):
        return PlanPromptRecovery("not_safe:human_target", before, "not_checked")
    if before != "plan_prompt":
        return PlanPromptRecovery("not_safe:not_plan_prompt", before, "not_checked")
    try:
        pane_id = exact_pane_id(target)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:ambiguous_pane", before, "not_checked")
    if not pane_id:
        return PlanPromptRecovery("not_safe:ambiguous_pane", before, "not_checked")
    try:
        fresh_lines = tail_pane_id(pane_id, n_lines)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:capture_failed", before, "capture_failed")
    fresh = plan_prompt_classification(fresh_lines)
    if fresh != "plan_prompt":
        return PlanPromptRecovery("not_safe:stale_evidence", before, fresh)
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("failed", fresh, "not_checked")
    if result.returncode != 0:
        return PlanPromptRecovery("failed", fresh, "not_checked")
    try:
        after = plan_prompt_classification(tail_pane_id(pane_id, n_lines))
    except (OSError, subprocess.SubprocessError):
        after = "capture_failed"
    return PlanPromptRecovery("sent_escape", fresh, after)


def dismiss_skills_menu_if_present(target: str, report: Report, n_lines: int = COMPACTION_WAIT_LINES) -> PlanPromptRecovery:
    """Send one Escape after exact, fresh verification of the Skills menu."""

    before = "skills_menu" if has_active_skills_menu(report.lines) else report.status
    match = TMUX_TARGET_RE.fullmatch(target)
    if match is None:
        return PlanPromptRecovery("not_safe:ambiguous_target", before, "not_checked")
    if match.group(1).startswith("h"):
        return PlanPromptRecovery("not_safe:human_target", before, "not_checked")
    if before != "skills_menu":
        return PlanPromptRecovery("not_safe:not_skills_menu", before, "not_checked")
    try:
        pane_id = exact_pane_id(target)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:ambiguous_pane", before, "not_checked")
    if not pane_id:
        return PlanPromptRecovery("not_safe:ambiguous_pane", before, "not_checked")
    try:
        if not pane_has_exact_codex_process(target, pane_id):
            return PlanPromptRecovery("not_safe:not_codex_process", before, "not_checked")
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:not_codex_process", before, "not_checked")
    try:
        fresh_lines = tail_pane_id(pane_id, n_lines)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:capture_failed", before, "capture_failed")
    fresh = skills_menu_classification(fresh_lines)
    if fresh != "skills_menu":
        return PlanPromptRecovery("not_safe:stale_evidence", before, fresh)
    try:
        if exact_pane_id(target) != pane_id:
            return PlanPromptRecovery("not_safe:target_rebound", fresh, "not_checked")
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("not_safe:target_rebound", fresh, "not_checked")
    try:
        result = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Escape"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return PlanPromptRecovery("failed", fresh, "not_checked")
    if result.returncode != 0:
        return PlanPromptRecovery("failed", fresh, "not_checked")
    try:
        after = skills_menu_classification(tail_pane_id(pane_id, n_lines))
    except (OSError, subprocess.SubprocessError):
        after = "capture_failed"
    return PlanPromptRecovery("sent_escape", fresh, after)


def status(lines: list[str], block: Block, *, detect_waiting_subagent: bool = False) -> str:
    if not lines:
        return "not_codex"
    content_hidden = any(CONTENT_HIDDEN_RE.search(line) is not None for line in latest_output_before_input(lines))
    codex_ui = any(
        (
            has_codex_model_footer(lines),
            has_file_search_overlay(lines),
            has_resume_paused_goal_prompt(lines),
            has_terminal_enter_prompt_after_codex_footer(lines),
            has_plan_prompt(lines),
            has_queued_message_footer(lines),
        )
    )
    if content_hidden and codex_ui:
        return "error"
    if has_file_search_overlay(lines):
        return "stuck_input"
    if has_resume_paused_goal_prompt(lines):
        return "stuck_input"
    if detect_waiting_subagent and has_waiting_subagent_prompt(lines):
        return "waiting_subagent"
    if is_cursor_agent_capture(lines):
        if cursor_usage_limit_lines(lines[-40:]):
            return "error"
        if has_cursor_followups_overlay(lines[-40:]):
            return "stuck_input"
        input_text = current_input_text(lines)
        if input_text and not is_stock_placeholder_input_text(input_text):
            return "stuck_input"
        if has_cursor_agent_running_indicator(lines):
            return "running"
        return "ready"
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
    if visible_error_lines(block.lines or lines[-20:], include_unmarked=False, allow_cursor_quota=False):
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
    if visible_error_lines(block.lines or lines[-20:], allow_cursor_quota=False):
        return "error"
    return "running"


def report_from_lines(lines: list[str], *, detect_waiting_subagent: bool = False) -> Report:
    block = current_block(lines)
    input_text = file_search_overlay_input_text(lines) or current_input_text(lines)
    can_submit_input = can_submit_stuck_input(lines)
    report_status = status(lines, block, detect_waiting_subagent=detect_waiting_subagent)
    input_blocker = stuck_input_blocker(lines, input_text) if report_status == "stuck_input" and not can_submit_input else ""
    return Report(report_status, report_output(lines, block, report_status), input_text, can_submit_input, input_blocker)


def inspect(args: Args, *, detect_waiting_subagent: bool = False) -> Report:
    exists, lines = exact_tail(args.target, args.n_lines)
    if not exists:
        return Report("missing", [])
    report = report_from_lines(lines, detect_waiting_subagent=detect_waiting_subagent)
    if is_cursor_agent_capture(lines):
        pane_id = exact_pane_id(args.target)
        if not (pane_id and pane_has_exact_managed_agent_process(args.target, pane_id)):
            return Report("not_codex", report.lines, report.input_text, False, "")
        return report
    if report.status == "not_codex":
        pane_id = exact_pane_id(args.target)
        if pane_id and pane_has_exact_managed_agent_process(args.target, pane_id):
            return Report("running", report.lines, report.input_text, False, "")
    return report


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


def wait_for_file_search_overlay_transition(
    target: str,
    n_lines: int = COMPACTION_WAIT_LINES,
    timeout_s: float = DEFAULT_COMPACTION_WAIT_TIMEOUT_S,
    interval_s: float = FILE_SEARCH_RECOVERY_INTERVAL_S,
) -> Report:
    deadline_s = time.monotonic() + timeout_s
    while True:
        lines = tail(target, n_lines)
        if not file_search_overlay_input_text(lines):
            return report_from_lines(lines)
        if time.monotonic() >= deadline_s:
            raise TimeoutError(f"file search overlay did not transition after {timeout_s:g}s")
        time.sleep(min(interval_s, max(0.05, deadline_s - time.monotonic())))


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        report = inspect(args)
        if args.dismiss_skills_menu:
            recovery = dismiss_skills_menu_if_present(args.target, report, args.n_lines)
            print(f"action: {recovery.action}")
            print(f"before: {recovery.before}")
            print(f"after: {recovery.after}")
            return 0 if recovery.action == "sent_escape" else 1
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
