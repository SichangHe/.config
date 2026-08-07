#!/usr/bin/env python3
"""Stop a Codex tmux pane and print the captured resume id if Codex exposes one."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

try:
    from omo_manager.omo_codex_status import Args as StatusArgs
    from omo_manager.omo_codex_status import current_block, exact_pane_id, inspect, status, tail, tail_pane_id
    from omo_manager.omo_task_lock import task_file_lock
except ModuleNotFoundError:
    from omo_codex_status import Args as StatusArgs
    from omo_codex_status import current_block, exact_pane_id, inspect, status, tail, tail_pane_id
    from omo_task_lock import task_file_lock  # pyright: ignore[reportImplicitRelativeImport]

SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
EXIT_INTERRUPT_DELAY_S = 0.75
EXIT_INTERRUPT_ATTEMPTS = 4
DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_RESUME_TOOL = "pcodx"
RESUME_TOOLS = {"codex", "pcodx"}
STOPPABLE_CODEX_STATUSES = {"error", "ready", "running", "stuck_input", "waiting_subagent"}
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RESUME_RE = re.compile(rf"(?i)\bcodex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b")
EXIT_RESUME_RE = re.compile(
    rf"(?i)\bTo\s+(?:resume|continue this session),\s+run\s+codex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b"
)
STATUS_SESSION_RE = re.compile(rf"\bSession:\s*({UUID_RE})\b")


@dataclass(frozen=True)
class Args:
    target: str
    wait_s: float
    lines: int
    dry_run: bool
    allow_self: bool
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    no_feedback: bool = False
    feedback_wait_s: float = 180.0


class ParsedArgs(argparse.Namespace):
    target: str = ""
    wait_s: float = 10.0
    lines: int = 2000
    dry_run: bool = False
    allow_self: bool = False
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    no_feedback: bool = False
    feedback_wait_s: float = 180.0


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""This is the lower-level stop helper used by
`omo_task_status.py TASK.md done`. Use it directly only for a non-task pane;
normal task closure goes through `omo_task_status.py`. Restart a running task
in place with `omo_codex_start.py --restart-running`.""",
    )
    _ = parser.add_argument("--target", required=True, help="tmux pane/window target, e.g. `cfg:2.0`.")
    _ = parser.add_argument("--wait-s", type=float, default=10.0)
    _ = parser.add_argument("--lines", type=int, default=2000)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--allow-self", action="store_true", help="Allow stopping the current tmux pane.")
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", default="", help="Append a durable close note to this task markdown file.")
    _ = parser.add_argument("--no-feedback", action="store_true", help="Skip the default idle-worker feedback request.")
    _ = parser.add_argument("--feedback-wait-s", type=float, default=180.0, help="Seconds to wait for feedback response before closing.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.wait_s < 0:
        parser.error("--wait-s must be non-negative.")
    if parsed.lines <= 0:
        parser.error("--lines must be positive.")
    if parsed.task_file and not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.feedback_wait_s < 0:
        parser.error("--feedback-wait-s must be non-negative.")
    return Args(
        parsed.target,
        parsed.wait_s,
        parsed.lines,
        parsed.dry_run,
        parsed.allow_self,
        parsed.root.resolve(),
        parsed.task_file,
        parsed.no_feedback,
        parsed.feedback_wait_s,
    )


def tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5, check=check)


def pane_id(target: str) -> str:
    if re.fullmatch(r"%[0-9]+", target):
        out = tmux(["display-message", "-p", "-t", target, "#{pane_id}"])
        resolved = out.stdout.strip() if out.returncode == 0 else ""
        return resolved if resolved == target else ""
    return exact_pane_id(target)


def pane_target(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{session_name}:#{window_index}.#{pane_index}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def current_pane_id() -> str:
    """Return this process's pane, never an attached client's selected pane."""

    return os.environ.get("TMUX_PANE", "").strip()


def is_human_owned_target(target: str) -> bool:
    return target.partition(":")[0].startswith("h")


def target_session_name(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{session_name}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def current_command(target: str) -> str:
    out = tmux(["display-message", "-p", "-t", target, "#{pane_current_command}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def window_panes(target: str) -> int:
    out = tmux(["display-message", "-p", "-t", target, "#{window_panes}"])
    try:
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except ValueError:
        return 0


def capture(target: str, n_lines: int) -> str:
    out = tmux(["capture-pane", "-p", "-t", target, "-S", f"-{n_lines}"])
    return out.stdout if out.returncode == 0 else ""


def extract_resume_id(text: str) -> str:
    matches = RESUME_RE.findall(text)
    return matches[-1] if matches else ""


def extract_exit_resume_id(before: str, after: str) -> str:
    session_id = extract_resume_id(post_interrupt_output(before, after))
    if session_id:
        return session_id
    before_lines = {line.strip() for line in before.splitlines() if line.strip()}
    matches = [
        match.group(1)
        for line in after.splitlines()
        if line.strip() not in before_lines
        for match in EXIT_RESUME_RE.finditer(line)
    ]
    return matches[-1] if matches else ""


def extract_status_session_id(text: str) -> str:
    matches = STATUS_SESSION_RE.findall(text)
    return matches[-1] if matches else ""


def extract_new_status_session_id(before: str, after: str) -> str:
    session_id = extract_status_session_id(post_interrupt_output(before, after))
    if session_id:
        return session_id
    if after.count("/status") <= before.count("/status"):
        return ""
    return extract_status_session_id(after.rsplit("/status", 1)[-1])


def post_interrupt_output(before: str, after: str) -> str:
    if not before:
        return after
    if after.startswith(before):
        return after[len(before) :]
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    for n_lines in range(min(len(before_lines), len(after_lines)), 0, -1):
        if before_lines[-n_lines:] == after_lines[:n_lines]:
            return "".join(after_lines[n_lines:])
    before_text = {line.strip() for line in before_lines if line.strip()}
    after_text = {line.strip() for line in after_lines if line.strip()}
    if before_text.isdisjoint(after_text):
        return after
    return ""


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if path != root and root not in path.parents:
        raise RuntimeError("task file escapes root")
    if not path.is_file():
        raise RuntimeError(f"task file not found: {path}")
    return path


def task_ref(root: Path, task_file: str) -> str:
    return task_path(root, task_file).relative_to(root.resolve()).as_posix()


def target_aliases(target: str) -> set[str]:
    aliases = {target}
    window_target, dot, _pane = target.rpartition(".")
    if dot and ":" in window_target:
        aliases.add(window_target)
    elif not dot:
        aliases.add(f"{target}.0")
    return aliases


def runat_entries(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "runat:" and parts[-1] in RESUME_TOOLS:
            entries.append((parts[1], parts[-1]))
    return entries


def top_runat_entry(text: str) -> tuple[str, str] | None:
    first = text.splitlines()[0].strip().split() if text.splitlines() else []
    if len(first) >= 3 and first[0] == "runat:" and first[-1] in RESUME_TOOLS:
        return first[1], first[-1]
    return None


def resume_tool(args: Args) -> str:
    if not args.task_file or args.dry_run:
        return DEFAULT_RESUME_TOOL
    text = task_path(args.root, args.task_file).read_text(encoding="utf-8")
    top = top_runat_entry(text)
    aliases = target_aliases(args.target)
    if top and top[0] in aliases:
        return top[1]
    entries = runat_entries(text)
    legacy_entries = entries[1:] if top else entries
    for tmux_target, tool in reversed(legacy_entries):
        if tmux_target in aliases:
            return tool
    if top:
        return DEFAULT_RESUME_TOOL
    if entries:
        return entries[-1][1]
    return DEFAULT_RESUME_TOOL


def top_runat_tool(text: str) -> str:
    if entry := top_runat_entry(text):
        _, tool = entry
        return tool
    return ""


def resume_cmd(args: Args, session_id: str) -> str:
    return f"{resume_tool(args)} resume {session_id}"


def close_note(target: str, session_id: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now().astimezone()).strftime("%m-%d %H:%M %Z")
    if session_id:
        return (
            f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; "
            f"session_id: `{session_id}`.)\n"
        )
    return (
        f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; "
        "Codex session id not found in captured tmux output.)\n"
    )


def has_close_note(text: str, target: str, session_id: str) -> bool:
    stamp_pattern = r"\d{2}-\d{2} \d{2}:\d{2} [A-Za-z0-9_+\-]+"
    target_pattern = re.escape(target)
    if session_id:
        session_pattern = rf"session_id: `{re.escape(session_id)}`"
    else:
        session_pattern = "Codex session id not found in captured tmux output"
    pattern = re.compile(rf"^\(manager closed Codex agent {stamp_pattern}; tmux target `{target_pattern}`; {session_pattern}\.\)$")
    return any(pattern.fullmatch(line) for line in text.splitlines())


def record_close(args: Args, session_id: str) -> None:
    if not args.task_file or args.dry_run:
        return
    path = task_path(args.root, args.task_file)
    text = path.read_text(encoding="utf-8")
    if not has_close_note(text, args.target, session_id):
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write(close_note(args.target, session_id))
    move_todo_to_previous(args.root, args.task_file)


def section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    start = -1
    for idx, line in enumerate(lines):
        if line.strip() == f"{name}:":
            start = idx
            break
    if start < 0:
        return None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.endswith(":") and stripped[:-1] in {"current", "previous", "human pending", "low priority"}:
            end = idx
            break
    return start, end


def moved_todo_text(root: Path, task_file: str, text: str) -> str:
    """Return `TODO.md` text with the task moved from `current` to `previous`."""
    ref = task_ref(root, task_file)
    aliases = {task_file, ref, str(task_path(root, task_file))}
    lines = text.splitlines()
    current = section_bounds(lines, "current")
    if current is None:
        return text
    current_start, current_end = current
    source_idx = -1
    for idx in range(current_start + 1, current_end):
        stripped = lines[idx].strip()
        if stripped and stripped.split(maxsplit=1)[0] in aliases:
            source_idx = idx
            break
    if source_idx < 0:
        return text
    moved = lines.pop(source_idx).strip()
    moved_token = moved.split(maxsplit=1)[0]
    if moved_token != ref:
        moved = moved.replace(moved_token, ref, 1)
    previous = section_bounds(lines, "previous")
    if previous is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("previous:")
        previous = (len(lines) - 1, len(lines))
    previous_start, previous_end = previous
    for idx in range(previous_start + 1, previous_end):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        token = stripped.split(maxsplit=1)[0]
        if token in aliases:
            if token != ref:
                lines[idx] = lines[idx].replace(token, ref, 1)
            return "\n".join(lines) + "\n"
    lines.insert(previous_start + 1, moved)
    return "\n".join(lines) + "\n"


def move_todo_to_previous(root: Path, task_file: str) -> None:
    todo = root / "TODO.md"
    with task_file_lock(todo):
        if not todo.exists():
            return
        text = todo.read_text(encoding="utf-8")
        updated = moved_todo_text(root, task_file, text)
        if updated != text:
            _ = todo.write_text(updated, encoding="utf-8")


def wait_shell(target: str, deadline_s: float) -> bool:
    while time.monotonic() < deadline_s:
        if current_command(target) in SHELL_COMMANDS:
            return True
        time.sleep(0.25)
    return current_command(target) in SHELL_COMMANDS


def paste_text(target: str, text: str) -> None:
    buffer_name = f"omo-codex-stop-{os.getpid()}-{time.monotonic_ns()}"
    _ = tmux(["set-buffer", "-b", buffer_name, text], check=True)
    try:
        _ = tmux(["paste-buffer", "-b", buffer_name, "-t", target], check=True)
    finally:
        _ = tmux(["delete-buffer", "-b", buffer_name])


def input_has_status_prompt(text: str) -> bool:
    return any(line.lstrip().startswith("› ") and "/status" in line for line in text.splitlines()[-20:])


def query_status_session_id(target: str, n_lines: int, wait_s: float) -> tuple[str, str]:
    before = capture(target, n_lines)
    paste_text(target, "/status")
    _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
    deadline_s = time.monotonic() + wait_s
    after = before
    fallback_sent = False
    while time.monotonic() < deadline_s:
        after = capture(target, n_lines)
        session_id = extract_new_status_session_id(before, after)
        if session_id:
            return session_id, after
        if not fallback_sent and input_has_status_prompt(after):
            _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
            fallback_sent = True
        time.sleep(0.25)
    return "", after


def send_exit_keys(target: str) -> None:
    for _attempt in range(EXIT_INTERRUPT_ATTEMPTS):
        _ = tmux(["send-keys", "-t", target, "C-c"], check=True)
        time.sleep(EXIT_INTERRUPT_DELAY_S)
        if current_command(target) in SHELL_COMMANDS:
            return


def close_tmux_target(target: str) -> None:
    if current_command(target) not in SHELL_COMMANDS:
        return
    if window_panes(target) == 1:
        _ = tmux(["kill-window", "-t", target], check=True)
    else:
        _ = tmux(["kill-pane", "-t", target], check=True)


def close_exited_codex_shell(target: str, expected_pane_id: str, session_id: str, terminal_evidence: str, n_lines: int = 2000) -> None:
    """Close one unchanged shell pane that retains exact terminal Codex evidence."""

    if not re.fullmatch(r"%[0-9]+", expected_pane_id):
        raise RuntimeError("expected pane id must be an exact numeric tmux pane id")
    if is_human_owned_target(target) or is_human_owned_target(target_session_name(expected_pane_id)):
        raise RuntimeError(f"refusing to close human-owned target: {target}")
    if not re.fullmatch(UUID_RE, session_id):
        raise RuntimeError("session id must be an exact Codex UUID")
    evidence = terminal_evidence.strip()
    if len(evidence) < 12:
        raise RuntimeError("terminal evidence must be a specific nonempty report token")
    if pane_id(target) != expected_pane_id or pane_id(expected_pane_id) != expected_pane_id:
        raise RuntimeError(f"target no longer resolves to expected pane {expected_pane_id}")
    if expected_pane_id == current_pane_id():
        raise RuntimeError(f"refusing to close the current pane: {expected_pane_id}")
    numeric_target = pane_target(expected_pane_id)
    report = inspect(StatusArgs(numeric_target, 80)) if numeric_target else None
    if report is None or report.status != "not_codex" or current_command(expected_pane_id) not in SHELL_COMMANDS:
        actual = report.status if report is not None else "missing"
        raise RuntimeError(f"expected an exited non-Codex shell: {expected_pane_id} status={actual}")
    before = capture(expected_pane_id, n_lines)
    interrupted_at = before.rfind("Conversation interrupted")
    accepted_at = before.rfind('"accepted":true', 0, interrupted_at)
    if before.count("Conversation interrupted") != 1 or accepted_at < 0 or evidence not in before[accepted_at:interrupted_at]:
        raise RuntimeError("terminal report evidence is absent before the final Codex exit marker")
    exit_text = before[interrupted_at:]
    resume_matches = list(EXIT_RESUME_RE.finditer(exit_text))
    if len(resume_matches) != 1 or resume_matches[0].group(1) != session_id or extract_resume_id(exit_text) != session_id:
        raise RuntimeError("captured terminal Codex session does not match the supplied session id")
    shell_tail = exit_text[resume_matches[0].end() :].strip("\r\n")
    if not shell_tail or len(shell_tail.splitlines()) != 1:
        raise RuntimeError("pane contains shell activity after the terminal Codex exit")
    if (
        pane_id(target) != expected_pane_id
        or pane_id(expected_pane_id) != expected_pane_id
        or pane_target(expected_pane_id) != numeric_target
        or current_command(expected_pane_id) not in SHELL_COMMANDS
        or inspect(StatusArgs(numeric_target, 80)).status != "not_codex"
        or capture(expected_pane_id, n_lines) != before
    ):
        raise RuntimeError("pane identity or shell evidence changed during recovery; retry")
    close_tmux_target(expected_pane_id)
    if pane_id(expected_pane_id):
        raise RuntimeError(f"exact stale shell pane remained live after close: {expected_pane_id}")


def codex_status(target: str) -> str:
    lines = tail_pane_id(target, 80) if re.fullmatch(r"%[0-9]+", target) else tail(target, 80)
    return status(lines, current_block(lines))


def feedback_prompt(task_file: str) -> str:
    return (
        "Before the manager closes this session, please send concise process feedback if this was a non-trivial task. "
        "If there is anything worth preserving, first run `REPORT_FILE=$(omo_report.sh --alloc-message-file)`, "
        "write the report file through an editor, apply_patch, or another non-shell text channel, "
        "then run `omo_report.sh --status done --message-file \"$REPORT_FILE\"`. "
        "Do not use cat, heredocs, or shell text injection for report bodies. "
        "Say whether you had partial-compaction access, whether you used it, why or why not, and any feedback about the PCODX instructions, tools, or compaction triggers. "
        "Mention unclear instructions, routing/communication gaps, missing tooling/docs, check friction, or whether manager-triggered compaction would have helped you continue. "
        "If the partial-compaction feedback is substantial, include the relevant evidence paths, such as the task file, tmux target, session id, transcript path, or PCODX ledger path, so the manager can email the human and forward it to OPC partial-compaction work. "
        "Keep it to at most five short bullets. If there is no useful feedback, say so briefly."
    )


def wait_feedback(target: str, timeout_s: float) -> None:
    deadline_s = time.monotonic() + timeout_s
    saw_running = False
    while time.monotonic() < deadline_s:
        current = codex_status(target)
        if current == "running":
            saw_running = True
        elif saw_running and current == "ready":
            return
        elif current in {"error", "not_codex"}:
            return
        time.sleep(0.5)


def maybe_request_feedback(args: Args) -> None:
    if args.no_feedback or not args.task_file or args.feedback_wait_s <= 0:
        return
    if codex_status(args.target) != "ready":
        return
    paste_text(args.target, feedback_prompt(args.task_file))
    _ = tmux(["send-keys", "-t", args.target, "Enter"], check=True)
    wait_feedback(args.target, args.feedback_wait_s)


def stop(args: Args) -> str:
    if is_human_owned_target(args.target):
        raise RuntimeError(f"refusing to stop human-owned target: {args.target}")
    target_pane = pane_id(args.target)
    if not target_pane:
        raise RuntimeError(f"tmux target not found: {args.target}")
    if not args.allow_self and target_pane == current_pane_id():
        raise RuntimeError(f"refusing to stop the current pane: {args.target}")
    if args.task_file:
        _ = task_path(args.root, args.task_file)
    if is_human_owned_target(target_session_name(target_pane)):
        raise RuntimeError(f"refusing to stop human-owned target: {args.target}")
    numeric_target = pane_target(target_pane)
    report = inspect(StatusArgs(numeric_target, 80)) if numeric_target else None
    if report is None or report.status not in STOPPABLE_CODEX_STATUSES:
        actual = report.status if report is not None else "missing"
        raise RuntimeError(f"target is not a supported live Codex pane: {args.target} status={actual}")
    resolved_args = replace(args, target=target_pane)
    if resolved_args.dry_run:
        print(f"would send Ctrl-C to {resolved_args.target}")
        return ""
    maybe_request_feedback(resolved_args)
    session_id, before_close = query_status_session_id(resolved_args.target, resolved_args.lines, resolved_args.wait_s)
    if pane_id(resolved_args.target) != target_pane:
        raise RuntimeError(f"tmux target disappeared before interrupt: {args.target}")
    send_exit_keys(resolved_args.target)
    _ = wait_shell(resolved_args.target, time.monotonic() + resolved_args.wait_s)
    after = capture(resolved_args.target, resolved_args.lines)
    close_tmux_target(resolved_args.target)
    return session_id or extract_exit_resume_id(before_close, after)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        session_id = stop(args)
        record_close(args, session_id)
        if session_id:
            print(f"session_id: {session_id}")
            print(f"resume_cmd: {resume_cmd(args, session_id)}")
        else:
            print("session_id:")
            if args.task_file and not args.dry_run:
                print("warning: Codex resume session id not found in captured tmux output", file=sys.stderr)
    except Exception as exc:
        print(f"omo_codex_stop: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
