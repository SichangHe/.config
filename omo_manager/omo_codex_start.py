#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""Start, resume, or recover tracked Codex work in an existing tmux pane."""

from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from omo_manager.omo_codex_status import Args as StatusArgs
    from omo_manager.omo_codex_status import current_block, inspect, tail
    from omo_manager.omo_codex_status import status as classify_status
    from omo_manager.omo_codex_stop import query_status_session_id
    from omo_manager.omo_task import capture_pane, has_codex_trust_prompt, has_live_codex_launch
    from omo_manager.omo_task_lock import task_file_lock, task_target_lock
    from omo_manager.omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, parse_task_metadata
except ModuleNotFoundError:
    from omo_codex_status import Args as StatusArgs  # pyright: ignore[reportImplicitRelativeImport]
    from omo_codex_status import current_block, inspect, tail  # pyright: ignore[reportImplicitRelativeImport]
    from omo_codex_status import status as classify_status  # pyright: ignore[reportImplicitRelativeImport]
    from omo_codex_stop import query_status_session_id  # pyright: ignore[reportImplicitRelativeImport]
    from omo_task import capture_pane, has_codex_trust_prompt, has_live_codex_launch  # pyright: ignore[reportImplicitRelativeImport]
    from omo_task_lock import task_file_lock, task_target_lock  # pyright: ignore[reportImplicitRelativeImport]
    from omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, parse_task_metadata  # pyright: ignore[reportImplicitRelativeImport]

HELPER_DIR = Path(__file__).resolve().parent
WORKER_DEFAULTS = HELPER_DIR / "WORKER_DEFAULTS.md"
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
SUCCESS_STATUSES = {"ready", "running"}
RESTARTABLE_STATUSES = {"error", "ready", "running", "stuck_input", "waiting_subagent"}
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
CODEX_LAUNCH_MARKER_RE = re.compile(r"^\[omo:[0-9a-f]{32}\]$")
ACTIVE_TASK_STATUSES = frozenset(TASK_FRONTMATTER_STATUSES - {"done"})
DIRECTORY_TRUST_RECOVERY_STATUSES = frozenset({"blocked", "running"})


class StartError(RuntimeError):
    """A same-pane launch precondition or operation failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    target: str
    model: str
    reasoning_effort: str
    session_id: str
    prompt_file: Path | None
    startup_timeout_s: float
    confirm_empty_shell: bool
    dry_run: bool
    restart_running: bool = False
    confirm_directory_trust: bool = False


class ParsedArgs(argparse.Namespace):
    if TYPE_CHECKING:
        root: Path = Path()
        task_file: str = ""
        target: str = ""
        model: str = ""
        reasoning_effort: str = ""
        session_id: str = ""
        prompt_file: Path | None = None
        startup_timeout_s: float = 45.0
        confirm_empty_shell: bool = False
        restart_running: bool = False
        confirm_directory_trust: bool = False
        dry_run: bool = False


@dataclass(frozen=True)
class Pane:
    target: str
    pane_id: str
    window_id: str
    command: str
    workdir: Path


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--task-file", required=True, help="Active tracked task whose `runat` names the target pane.")
    _ = parser.add_argument("--target", required=True, help="Exact existing shell pane: SESSION:WINDOW[.PANE].")
    _ = parser.add_argument("--model", default="")
    _ = parser.add_argument("--reasoning-effort", default="", choices=EFFORTS)
    _ = parser.add_argument("--session-id", default="", help="Existing Codex session to resume without a new prompt.")
    _ = parser.add_argument("--prompt-file", type=Path, help="Task-local prompt for a fresh Codex session.")
    _ = parser.add_argument("--startup-timeout-s", type=float, default=45.0)
    _ = parser.add_argument(
        "--confirm-empty-shell",
        action="store_true",
        help="Confirm the target shell has no input to preserve; the helper sends Ctrl-C before launch.",
    )
    _ = parser.add_argument("--restart-running", action="store_true", help="Capture the current Codex session and atomically respawn it in this exact pane.")
    _ = parser.add_argument("--confirm-directory-trust", action="store_true", help="Confirm the exact trust prompt in an existing tracked Codex pane.")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    recovery_conflicts = (
        "--model",
        "--reasoning-effort",
        "--session-id",
        "--prompt-file",
        "--confirm-empty-shell",
        "--restart-running",
    )
    supplied_recovery_conflict = next(
        (option for option in recovery_conflicts if any(token == option or token.startswith(f"{option}=") for token in argv)),
        "",
    )
    if parsed.confirm_directory_trust and supplied_recovery_conflict:
        parser.error(f"--confirm-directory-trust does not accept {supplied_recovery_conflict}.")
    if not parsed.confirm_directory_trust and (not parsed.model or not parsed.reasoning_effort):
        parser.error("--model and --reasoning-effort are required unless --confirm-directory-trust is used.")
    if parsed.model and MODEL_RE.fullmatch(parsed.model) is None:
        parser.error("--model contains unsupported characters.")
    if parsed.session_id and UUID_RE.fullmatch(parsed.session_id) is None:
        parser.error("--session-id must be a Codex UUID.")
    if parsed.restart_running and (parsed.prompt_file or parsed.session_id):
        parser.error("--restart-running captures the live session and does not accept --prompt-file or --session-id.")
    if not parsed.confirm_directory_trust and not parsed.restart_running and bool(parsed.session_id) == bool(parsed.prompt_file):
        parser.error("provide exactly one of --session-id or --prompt-file.")
    if not math.isfinite(parsed.startup_timeout_s) or parsed.startup_timeout_s <= 0:
        parser.error("--startup-timeout-s must be finite and positive.")
    if not parsed.confirm_directory_trust and not parsed.restart_running and not parsed.confirm_empty_shell:
        parser.error("--confirm-empty-shell is required because tmux cannot inspect a shell's input buffer.")
    return Args(
        root=parsed.root.expanduser().resolve(),
        task_file=parsed.task_file,
        target=parsed.target,
        model=parsed.model,
        reasoning_effort=parsed.reasoning_effort,
        session_id=parsed.session_id,
        prompt_file=parsed.prompt_file.expanduser().resolve() if parsed.prompt_file else None,
        startup_timeout_s=parsed.startup_timeout_s,
        confirm_empty_shell=parsed.confirm_empty_shell,
        dry_run=parsed.dry_run,
        restart_running=parsed.restart_running,
        confirm_directory_trust=parsed.confirm_directory_trust,
    )


def run(command: list[str], *, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)


def target_identity(target: str) -> tuple[str, int, int | None] | None:
    if TARGET_RE.fullmatch(target) is None:
        return None
    session, window_and_pane = target.split(":", 1)
    window, separator, pane = window_and_pane.partition(".")
    return session, int(window), int(pane) if separator else None


def resolve_pane(target: str) -> Pane:
    requested_identity = target_identity(target)
    if requested_identity is None:
        raise StartError(f"tmux target must be exact SESSION:WINDOW[.PANE]: {target}")
    result = run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{window_id}\t#{pane_current_command}\t#{pane_current_path}",
        ]
    )
    if result.returncode != 0:
        raise StartError(f"tmux target does not exist: {target}")
    if result.stdout == ":.\t\t\t\t\n":
        raise StartError(f"tmux target does not exist: {target}")
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 5 or not fields[1].startswith("%") or not fields[2].startswith("@"):
        raise StartError(f"tmux returned invalid identity for target: {target}")
    resolved_identity = target_identity(fields[0])
    if resolved_identity is None:
        raise StartError(f"tmux returned invalid identity for target: {target}")
    requested_session, requested_window, requested_pane = requested_identity
    resolved_session, resolved_window, resolved_pane = resolved_identity
    if (requested_session, requested_window) != (resolved_session, resolved_window) or (requested_pane is not None and requested_pane != resolved_pane):
        raise StartError(f"tmux target does not exist exactly as requested: {target}")
    return Pane(fields[0], fields[1], fields[2], fields[3], Path(fields[4]))


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve()
    if not path.is_relative_to(root) or path.parent != root:
        raise StartError("--task-file must name one file directly under --root.")
    return path


def current_todo_entries(text: str) -> set[str]:
    section = ""
    entries: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.rstrip(":").casefold() in {"current", "previous", "human pending", "low priority"}:
            section = stripped.rstrip(":").casefold()
        elif section == "current" and stripped:
            entries.add(stripped)
    return entries


def validate_task(args: Args, pane: Pane, *, allowed_statuses: frozenset[str] = ACTIVE_TASK_STATUSES) -> bool:
    path = task_path(args.root, args.task_file)
    if not path.is_file():
        raise StartError(f"task file does not exist: {path}")
    metadata = parse_task_metadata(path.read_text(encoding="utf-8"), args.root)
    if metadata is None:
        raise StartError("task file requires valid frontmatter.")
    if metadata.status not in allowed_statuses:
        raise StartError(f"task status is not active for this operation: {metadata.status}")
    if metadata.tool != "codex":
        raise StartError(f"same-pane start supports only `tool: codex`, got {metadata.tool!r}.")
    if resolve_pane(metadata.runat).pane_id != pane.pane_id:
        raise StartError(f"task `runat` {metadata.runat} does not identify target {pane.target}.")
    todo = args.root / "TODO.md"
    expected = f"{path.name} {metadata.runat}"
    if not todo.is_file() or expected not in current_todo_entries(todo.read_text(encoding="utf-8")):
        raise StartError(f"TODO `current` does not contain exact task entry: {expected}")
    return metadata.is_manager


def prompt_text(args: Args, is_manager: bool) -> str:
    if args.prompt_file is None:
        return ""
    sources = [WORKER_DEFAULTS]
    if is_manager:
        sources.append(args.root / "MANAGER.md")
    sources.append(args.prompt_file)
    for source in sources:
        if not source.is_file():
            raise StartError(f"required prompt source is not readable: {source}")
    return "\n\n".join(source.read_text(encoding="utf-8").rstrip() for source in sources) + "\n"


def launch_command(args: Args, pane: Pane, prompt_path: Path | None, marker: str, *, replace_process: bool = False) -> str:
    codex = [
        "bunx",
        "@openai/codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        args.model,
        "--config",
        f'model_reasoning_effort="{args.reasoning_effort}"',
    ]
    if args.session_id:
        codex.extend(("resume", args.session_id))
    rendered = shlex.join(codex)
    if prompt_path is not None:
        rendered += f' "$(cat -- {shlex.quote(str(prompt_path))})"'
    exports = f"export OMO_AGENT_TMUX_TARGET={shlex.quote(pane.target)}"
    announce = f"printf '%s\\n' {shlex.quote(marker)}"
    execution = f"exec {rendered}" if replace_process else rendered
    return f"{exports}; cd {shlex.quote(str(pane.workdir))} && {announce} && {execution}"


def verify_same_pane(expected: Pane) -> None:
    current = resolve_pane(expected.target)
    if current.pane_id != expected.pane_id or current.window_id != expected.window_id:
        raise StartError("tmux pane or window identity changed during launch.")


def require_same_shell(expected: Pane) -> None:
    current = resolve_pane(expected.target)
    if current.pane_id != expected.pane_id or current.window_id != expected.window_id:
        raise StartError("tmux pane or window identity changed before launch.")
    if current.command not in SHELL_COMMANDS:
        raise StartError(f"target {current.target} is running {current.command or 'unknown'}, not an empty shell.")


def send_shell_command(pane: Pane, command: str) -> None:
    buffer_name = f"omo-codex-start-{os.getpid()}"
    loaded = run(["tmux", "set-buffer", "-b", buffer_name, "--", command])
    if loaded.returncode != 0:
        raise StartError(f"failed to load launch command into tmux: {loaded.stderr.strip()}")
    cleared = run(["tmux", "send-keys", "-t", pane.pane_id, "C-c"])
    if cleared.returncode != 0:
        _ = run(["tmux", "delete-buffer", "-b", buffer_name])
        raise StartError(f"failed to clear target shell input: {cleared.stderr.strip()}")
    pasted = run(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", pane.pane_id])
    if pasted.returncode != 0:
        _ = run(["tmux", "delete-buffer", "-b", buffer_name])
        raise StartError(f"failed to paste launch command: {pasted.stderr.strip()}")
    submitted = run(["tmux", "send-keys", "-t", pane.pane_id, "Enter"])
    if submitted.returncode != 0:
        raise StartError(f"failed to submit launch command: {submitted.stderr.strip()}")


def respawn_codex(pane: Pane, command: str) -> None:
    result = run(["tmux", "respawn-pane", "-k", "-t", pane.pane_id, "-c", str(pane.workdir), command])
    if result.returncode != 0:
        raise StartError(f"failed to respawn Codex in {pane.target}: {result.stderr.strip()}")
    verify_same_pane(pane)


def require_restartable_codex(pane: Pane) -> None:
    verify_same_pane(pane)
    report = inspect(StatusArgs(pane.target, 80))
    verify_same_pane(pane)
    if report.status not in RESTARTABLE_STATUSES:
        raise StartError(f"target {pane.target} is not a supported live Codex pane: {report.status}")


def post_marker_lines(pane: Pane, marker: str) -> list[str] | None:
    lines = tail(pane.target, 200)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == marker:
            return lines[index + 1 :]
    return None


def wait_started(pane: Pane, marker: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        verify_same_pane(pane)
        lines = post_marker_lines(pane, marker)
        classification = "not_codex" if lines is None else classify_status(lines, current_block(lines))
        if classification in SUCCESS_STATUSES:
            return classification
        if classification == "error":
            raise StartError("Codex startup reached an error state.")
        time.sleep(0.25)
    raise StartError("timed out waiting for Codex to become running or ready.")


def directory_trust_prompt_frame(lines: list[str]) -> list[str] | None:
    """Return the sole exact bottom prompt, optionally adjacent to its launch marker."""

    segments: list[tuple[bool, list[str]]] = []
    segment_start = 0
    follows_marker = False
    has_marker = False
    for index, line in enumerate(lines):
        if CODEX_LAUNCH_MARKER_RE.fullmatch(line) is not None:
            has_marker = True
            segments.append((follows_marker, lines[segment_start:index]))
            segment_start = index + 1
            follows_marker = True
    segments.append((follows_marker, lines[segment_start:]))

    frames: list[tuple[int, list[str]]] = []
    for segment_index, (follows_marker, segment) in enumerate(segments):
        if has_marker and not follows_marker:
            continue
        starts = [index for index, line in enumerate(segment) if line.strip() and has_codex_trust_prompt(segment[index:])]
        if len(starts) != 1:
            continue
        start = starts[0]
        if any(line.strip() for line in segment[:start]) or (follows_marker and start != 0):
            continue
        frames.append((segment_index, segment[start:]))
    return frames[0][1] if len(frames) == 1 and frames[0][0] == len(segments) - 1 else None


def require_directory_trust_recovery(args: Args, pane: Pane) -> None:
    """Validate one locked snapshot of the tracked trust prompt and process."""

    verify_same_pane(pane)
    _ = validate_task(args, pane, allowed_statuses=DIRECTORY_TRUST_RECOVERY_STATUSES)
    verify_same_pane(pane)
    lines = capture_pane(pane.pane_id, 200)
    verify_same_pane(pane)
    if directory_trust_prompt_frame(lines) is None:
        raise StartError(f"target {pane.target} does not show the exact Codex directory-trust prompt.")
    if not has_live_codex_launch(pane.pane_id):
        raise StartError(f"target {pane.target} does not contain exactly one live Codex launch process.")
    verify_same_pane(pane)


def send_directory_trust_enter(pane: Pane) -> None:
    submitted = run(["tmux", "send-keys", "-t", pane.pane_id, "Enter"])
    if submitted.returncode != 0:
        raise StartError(f"failed to confirm directory trust: {submitted.stderr.strip()}")


def wait_directory_trust_recovery(pane: Pane, timeout_s: float) -> str:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        verify_same_pane(pane)
        lines = capture_pane(pane.pane_id, 200)
        verify_same_pane(pane)
        classification = classify_status(lines, current_block(lines))
        if classification in SUCCESS_STATUSES:
            return classification
        if classification == "error":
            raise StartError("Codex directory-trust recovery reached an error state.")
        time.sleep(0.25)
    raise StartError("timed out waiting for Codex directory-trust recovery to become running or ready.")


def start(args: Args) -> str:
    pane = resolve_pane(args.target)
    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("omo_codex_start cannot modify a human-owned `h*` tmux session; use the human-authorized task launcher.")
    if os.environ.get("TMUX_PANE") == pane.pane_id:
        raise StartError("run this helper from a different pane than the target.")
    if not args.restart_running and not args.confirm_directory_trust:
        require_same_shell(pane)
    path = task_path(args.root, args.task_file)
    with task_target_lock(args.root, pane.target), task_file_lock(path):
        verify_same_pane(pane)
        if args.confirm_directory_trust:
            require_directory_trust_recovery(args, pane)
            if args.dry_run:
                print(f"target: {pane.target}")
                print("mode: confirm-directory-trust")
                return "dry-run"
            require_directory_trust_recovery(args, pane)
            send_directory_trust_enter(pane)
            return wait_directory_trust_recovery(pane, args.startup_timeout_s)
        if not args.restart_running:
            require_same_shell(pane)
        is_manager = validate_task(args, pane)
        if args.restart_running:
            require_restartable_codex(pane)
        effective_args = args
        if args.restart_running and not args.session_id:
            if args.dry_run:
                effective_args = replace(args, session_id="00000000-0000-4000-8000-000000000000")
            else:
                session_id, _ = query_status_session_id(pane.pane_id, 240, min(10.0, args.startup_timeout_s))
                if not session_id:
                    raise StartError("could not capture the current Codex session id; the pane was not replaced.")
                require_restartable_codex(pane)
                effective_args = replace(args, session_id=session_id)
        text = prompt_text(effective_args, is_manager)
        prompt_path: Path | None = None
        try:
            if text:
                fd, raw_path = tempfile.mkstemp(prefix="omo-codex-start-prompt-", suffix=".txt")
                os.close(fd)
                prompt_path = Path(raw_path)
                prompt_path.chmod(0o600)
                prompt_path.write_text(text, encoding="utf-8")
            marker = f"[omo-codex-start:{os.getpid()}:{time.time_ns()}]"
            command = launch_command(effective_args, pane, prompt_path, marker, replace_process=args.restart_running)
            if args.dry_run:
                print(f"target: {pane.target}")
                print(f"mode: {'restart-running' if args.restart_running else 'resume' if args.session_id else 'fresh'}")
                print(f"command: {command}")
                return "dry-run"
            if args.restart_running:
                respawn_codex(pane, command)
            else:
                require_same_shell(pane)
                send_shell_command(pane, command)
            return wait_started(pane, marker, args.startup_timeout_s)
        finally:
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    try:
        result = start(parse_args(sys.argv[1:] if argv is None else argv))
    except (OSError, StartError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"omo_codex_start: {error}", file=sys.stderr)
        return 1
    print(f"omo_codex_start: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
