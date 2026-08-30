#!/usr/bin/env python3
"""Recognize report-helper use in the latest visible completed Codex turn."""
from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from omo_manager.omo_codex_status import WORKED_RE

RAN_RE = re.compile(r"^\s*• Ran\s+(.+)$")
COMMAND_CONTINUATION_RE = re.compile(r"^\s*│\s?(.*)$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REPORT_HELPERS = {"email_me.py", "omo_report.sh"}
SHELLS = {"bash", "dash", "sh", "zsh"}
SHELL_OPTIONS_WITH_VALUE = {"-o", "--init-file", "--rcfile"}
PYTHONS = {"python", "python3", "python3.13"}
PREFIX_WRAPPERS = {"command", "exec", "env", "nice", "nohup", "setsid", "stdbuf", "sudo", "timeout"}
CONTROL_TOKENS = {"|", "||", "&", "&&", ";", ";;", "(", ")"}


@dataclass(frozen=True)
class VisibleTurn:
    lines: tuple[str, ...]
    fingerprint: str


def latest_visible_turn(lines: Sequence[str]) -> VisibleTurn | None:
    """Return the last prompt through its completed activity footer."""

    end = next((index for index in range(len(lines) - 1, -1, -1) if WORKED_RE.match(lines[index].rstrip())), -1)
    if end < 0:
        return None
    start = next((index for index in range(end - 1, -1, -1) if lines[index].startswith("› ")), -1)
    if start < 0:
        return None
    turn_lines = tuple(line.rstrip() for line in lines[start : end + 1])
    digest = hashlib.sha256("\n".join(turn_lines).encode("utf-8")).hexdigest()
    return VisibleTurn(turn_lines, digest)


def recent_visible_turns(lines: Sequence[str], limit: int = 2) -> tuple[VisibleTurn, ...]:
    """Return the latest bounded set of completed visible turns."""

    turns: list[VisibleTurn] = []
    cursor = len(lines)
    while cursor > 0 and len(turns) < limit:
        turn = latest_visible_turn(lines[:cursor])
        if turn is None:
            break
        turns.append(turn)
        first_line = turn.lines[0]
        cursor = next((index for index in range(cursor - 1, -1, -1) if lines[index].rstrip() == first_line), 0)
    turns.reverse()
    return tuple(turns)


def displayed_shell_commands(turn: VisibleTurn) -> tuple[str, ...]:
    """Extract only commands rendered as executed shell-tool activity."""

    commands: list[str] = []
    index = 0
    while index < len(turn.lines):
        match = RAN_RE.match(turn.lines[index])
        if match is None:
            index += 1
            continue
        command_lines = [match.group(1)]
        index += 1
        while index < len(turn.lines):
            continuation = COMMAND_CONTINUATION_RE.match(turn.lines[index])
            if continuation is None:
                break
            command_lines.append(continuation.group(1))
            index += 1
        commands.append("\n".join(command_lines))
    return tuple(commands)


def helper_name(token: str) -> str:
    return PurePosixPath(token.rstrip("/")).name


def wrapper_command_index(name: str, tokens: list[str], index: int) -> int:
    """Return the first command token after a supported prefix wrapper."""

    option_values = {
        "env": {"-C", "-u", "--chdir", "--unset"},
        "exec": {"-a"},
        "nice": {"-n", "--adjustment"},
        "stdbuf": {"-e", "-i", "-o"},
        "sudo": {"-C", "-g", "-h", "-p", "-r", "-t", "-u", "--chdir", "--group", "--host", "--prompt", "--role", "--type", "--user"},
        "timeout": {"-k", "-s", "--kill-after", "--signal"},
    }
    candidate = index + 1
    while candidate < len(tokens):
        token = tokens[candidate]
        if token == "--":
            candidate += 1
            break
        if name == "env" and ASSIGNMENT_RE.match(token):
            candidate += 1
            continue
        if token in option_values.get(name, set()):
            candidate += 2
            continue
        if token.startswith("-"):
            candidate += 1
            continue
        break
    if name == "timeout" and candidate < len(tokens):
        candidate += 1
    return candidate


def command_invokes_report_helper(command: str, report_helpers: frozenset[str] = frozenset(REPORT_HELPERS)) -> bool:
    """Recognize a helper in an executable position, not as quoted prose."""

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False

    command_start = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in CONTROL_TOKENS:
            command_start = token != ")"
            index += 1
            continue
        if not command_start:
            index += 1
            continue
        if ASSIGNMENT_RE.match(token):
            index += 1
            continue
        name = helper_name(token)
        if name in report_helpers:
            return True
        if name in SHELLS:
            candidate = index + 1
            while candidate < len(tokens) and tokens[candidate].startswith("-") and tokens[candidate] not in {"-c", "-lc"}:
                candidate += 2 if tokens[candidate] in SHELL_OPTIONS_WITH_VALUE else 1
            if candidate < len(tokens) and helper_name(tokens[candidate]) in report_helpers:
                return True
            script_index = next((candidate for candidate in range(index + 1, len(tokens)) if tokens[candidate] in {"-c", "-lc"}), -1)
            if script_index >= 0 and script_index + 1 < len(tokens) and command_invokes_report_helper(tokens[script_index + 1], report_helpers):
                return True
        if name in PYTHONS or name == "uv":
            candidate = index + 1
            while candidate < len(tokens) and (tokens[candidate].startswith("-") or name == "uv" and tokens[candidate] == "run"):
                candidate += 1
            if name == "uv" and candidate < len(tokens) and helper_name(tokens[candidate]) in PYTHONS:
                candidate += 1
                while candidate < len(tokens) and tokens[candidate].startswith("-"):
                    candidate += 1
            if candidate < len(tokens) and helper_name(tokens[candidate]) in report_helpers:
                return True
        if name in {"xargs"}:
            candidate = index + 1
            while candidate < len(tokens) and tokens[candidate].startswith("-"):
                candidate += 1
            if candidate < len(tokens) and helper_name(tokens[candidate]) in report_helpers:
                return True
        if name in {"find"}:
            for candidate in range(index + 1, len(tokens) - 1):
                if tokens[candidate] in {"-exec", "-execdir"} and helper_name(tokens[candidate + 1]) in report_helpers:
                    return True
        if name in PREFIX_WRAPPERS:
            index = wrapper_command_index(name, tokens, index)
            continue
        command_start = False
        index += 1
    return False


def turn_invoked_report_helper(turn: VisibleTurn) -> bool:
    return any(command_invokes_report_helper(command) for command in displayed_shell_commands(turn))
