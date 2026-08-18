#!/usr/bin/env python3
"""Run one bounded Cursor Agent task and return its structured result."""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Args:
    workspace: Path
    prompt_file: Path
    prompt: str
    model: str
    reasoning_effort: str
    timeout_s: float
    resume: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded Cursor Agent task in print mode. This is a pilot task runner, not a managed tmux worker; "
            "its result returns to the caller."
        )
    )
    parser.add_argument("--workspace", required=True, type=Path, help="Existing workspace directory Cursor may inspect or edit.")
    parser.add_argument("--prompt-file", required=True, type=Path, help="Readable file containing the complete task prompt.")
    parser.add_argument("--model", required=True, help="Cursor model family without an effort suffix; normally use cursor-grok-4.6.")
    parser.add_argument("--reasoning-effort", required=True, choices=EFFORTS, help="Cursor model effort suffix; normally use xhigh, forming cursor-grok-4.6-xhigh.")
    parser.add_argument("--timeout-s", required=True, type=float, help="Maximum wall-clock seconds for this one-shot task.")
    parser.add_argument("--resume", default="", help="Optional Cursor session UUID returned by an earlier run.")
    parsed = parser.parse_args(argv)
    workspace = parsed.workspace.resolve()
    prompt_file = parsed.prompt_file.resolve()
    if not workspace.is_dir():
        parser.error(f"--workspace must be an existing directory: {workspace}")
    if not prompt_file.is_file():
        parser.error(f"--prompt-file must be a readable file: {prompt_file}")
    try:
        prompt = prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        parser.error(f"--prompt-file is not readable UTF-8 text: {exc}")
    if not prompt.strip():
        parser.error("--prompt-file must not be empty.")
    if not parsed.model.strip() or any(ch.isspace() for ch in parsed.model):
        parser.error("--model must be one nonempty Cursor model family.")
    if parsed.resume:
        try:
            _ = UUID(parsed.resume)
        except ValueError:
            parser.error("--resume must be a Cursor session UUID.")
    if not math.isfinite(parsed.timeout_s) or parsed.timeout_s <= 0:
        parser.error("--timeout-s must be positive.")
    return Args(workspace, prompt_file, prompt, parsed.model, parsed.reasoning_effort, parsed.timeout_s, parsed.resume)


def command(args: Args, executable: str) -> list[str]:
    result = [
        executable,
        "--print",
        "--output-format",
        "json",
        "--force",
        "--sandbox",
        "disabled",
        "--trust",
        "--workspace",
        str(args.workspace),
        "--model",
        f"{args.model}-{args.reasoning_effort}",
    ]
    if args.resume:
        result.extend(("--resume", args.resume))
    result.append(args.prompt)
    return result


def emit_result(ok: bool, **values: object) -> None:
    print(json.dumps({"schema": "amh-cursor-agent/v1", "ok": ok, **values}, ensure_ascii=False, separators=(",", ":")))


def valid_session_id(value: object) -> bool:
    try:
        _ = UUID(str(value))
    except ValueError:
        return False
    return True


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop a timed-out Cursor process and every tool process it started."""

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline_s = time.monotonic() + 2
    while time.monotonic() < deadline_s:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()


def execute(command_args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except BaseException:
        terminate_process_group(process)
        _ = process.communicate()
        raise
    return subprocess.CompletedProcess(command_args, process.returncode, stdout, stderr)


def run(args: Args) -> int:
    executable = shutil.which("agent")
    if executable is None:
        emit_result(False, error="Cursor Agent CLI `agent` is not installed or not on PATH.")
        return 127
    try:
        completed = execute(command(args, executable), args.timeout_s)
    except subprocess.TimeoutExpired:
        emit_result(False, error=f"Cursor Agent exceeded the {args.timeout_s:g}-second timeout.")
        return 124
    except OSError as exc:
        emit_result(False, error=f"Cursor Agent could not start: {exc}")
        return 1
    if completed.returncode != 0:
        emit_result(False, error="Cursor Agent exited unsuccessfully.", detail=completed.stderr or completed.stdout, returncode=completed.returncode)
        return completed.returncode
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        emit_result(False, error="Cursor Agent returned invalid JSON.", detail=completed.stdout)
        return 1
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "result"
        or payload.get("subtype") != "success"
        or payload.get("is_error") is not False
        or not isinstance(payload.get("result"), str)
        or not valid_session_id(payload.get("session_id"))
    ):
        emit_result(False, error="Cursor Agent did not return a complete successful result.", cursor=payload)
        return 1
    emit_result(True, result=payload["result"], session_id=payload["session_id"], cursor=payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
