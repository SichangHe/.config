#!/usr/bin/env python3
"""Load launch instructions through the public instruction command."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


GETAGENTSMD = Path(__file__).resolve().parent.parent / "getagentsmd"
GETAGENTSMD_TIMEOUT_S = 15.0


class AgentInstructionsError(RuntimeError):
    """An instruction command did not produce usable output."""


def command_output(*args: str) -> bytes:
    command = (str(GETAGENTSMD), *args)
    try:
        result = subprocess.run(command, capture_output=True, timeout=GETAGENTSMD_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentInstructionsError(f"failed to run `{shlex.join(command)}`: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise AgentInstructionsError(f"`{shlex.join(command)}` exited {result.returncode}{suffix}")
    try:
        output = result.stdout.decode()
    except UnicodeDecodeError as exc:
        raise AgentInstructionsError(f"`{shlex.join(command)}` output is not UTF-8") from exc
    if not output.strip():
        raise AgentInstructionsError(f"`{shlex.join(command)}` produced no agent instructions")
    if not output.endswith("\n"):
        output += "\n"
    return f"$ {shlex.join(command)}\n{output}".encode()


# 🧑 “change the agent-spawning scripts to show which commands it runs and the content from running those commands, as opposed to the current file inclusion”
def launch_instructions(manager_role: str | None = None) -> bytes:
    """Return command transcripts for a worker, main manager, or submanager."""

    if manager_role not in {None, "main_manager", "submanager"}:
        raise ValueError(f"unknown manager role: {manager_role}")
    parts = [command_output()]
    if manager_role is not None:
        parts.extend((command_output("get", "agent_manager"), command_output("get", manager_role)))
    return b"\n".join(parts)
