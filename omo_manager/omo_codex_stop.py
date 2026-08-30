#!/usr/bin/env python3
"""Stop a Codex tmux pane and print the captured resume id if Codex exposes one."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import yaml

try:
    from omo_manager.omo_codex_status import Args as StatusArgs
    from omo_manager.omo_codex_status import current_block, exact_pane_id, inspect, is_cursor_agent_capture, report_from_lines, status, tail, tail_pane_id
    from omo_manager.omo_task_lock import process_start_ticks, task_file_lock
except ModuleNotFoundError:
    from omo_codex_status import Args as StatusArgs
    from omo_codex_status import current_block, exact_pane_id, inspect, is_cursor_agent_capture, report_from_lines, status, tail, tail_pane_id
    from omo_task_lock import process_start_ticks, task_file_lock  # pyright: ignore[reportImplicitRelativeImport]

SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
EXIT_INTERRUPT_DELAY_S = 0.75
EXIT_INTERRUPT_ATTEMPTS = 4
DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_RESUME_TOOL = "pcodx"
RESUME_TOOLS = {"codex", "pcodx", "cursor"}
STOPPABLE_CODEX_STATUSES = {"error", "ready", "running", "stuck_input", "waiting_subagent"}
# PATH exposes this helper through a symlink in ~/.config/bin. Resolve the
# implementation location so both direct and package execution use the same
# owner-controlled manager configuration beside this source file.
LOCAL_ENV_PATH = Path(__file__).resolve().with_name("local.env")
HUMAN_CLOSE_SOURCE_RE = re.compile(r"manager_mail/[A-Za-z0-9_.-]+\.txt\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
HUMAN_CLOSE_DIRECTIVE_RE = re.compile(
    r"(?im)^\s*close\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?=$|[\s,.;:])"
)
HUMAN_CLOSE_REPLY_DIRECTIVE_RE = re.compile(r"(?im)^\s*cancel\s+this\s+task(?=$|[\s,.;:])")
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
    human_close_authorization_source: str = ""
    human_close_authorization_sha256: str = ""
    # Internal lifecycle handoff: preserve the symbolic human target while
    # stopping its already-pinned numeric pane id.
    human_close_authorized_target: str = ""
    # Internal lifecycle handoff: require this symbolic non-human target and
    # its pinned pane id to remain the same through every input and close.
    bound_symbolic_target: str = ""
    bound_pane_id: str = ""
    bound_close_proof_path: str = ""
    bound_close_audit_path: str = ""
    bound_close_proof_secret: str = ""
    bound_close_proof_commitment: str = ""
    # Appended after the original proof fields so positional construction by
    # existing lifecycle callers retains its historical argument meaning.
    bound_pane_pid: int = 0
    bound_pane_start_ticks: int = 0
    # Internal lifecycle handoff: a fresh guarded `/status` response must
    # identify this exact Codex session before the first interrupt.
    bound_expected_session_id: str = ""
    # Internal manager-replacement gate run after final Human authority checks
    # and immediately before the first protected-pane input.
    bound_pre_input_check: Callable[[], None] | None = None


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
    human_close_authorization_source: str = ""
    human_close_authorization_sha256: str = ""


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
epilog="""This is the lower-level stop helper used by
`omo_task_status.py TASK.md done`. Use it directly only for a non-task pane;
normal task closure goes through `omo_task_status.py`. The sole exception is
an exact `h*` task target with hash-bound human-close authority; it requires
`--task-file` and `--no-feedback`. Restart a running task in place with
`omo_codex_start.py --restart-running`.""",
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
    _ = parser.add_argument("--human-close-authorization-source", default="", help="Exact trusted manager_mail/<id>.txt record that directly authorizes closing this human-owned task target.")
    _ = parser.add_argument("--human-close-authorization-sha256", default="", help="Lowercase SHA-256 of the exact human-close authorization record.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.wait_s < 0:
        parser.error("--wait-s must be non-negative.")
    if parsed.lines <= 0:
        parser.error("--lines must be positive.")
    if parsed.task_file and not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    if parsed.feedback_wait_s < 0:
        parser.error("--feedback-wait-s must be non-negative.")
    authority_values = (parsed.human_close_authorization_source.strip(), parsed.human_close_authorization_sha256.strip())
    if any(authority_values) and (not all(authority_values) or not is_human_owned_target(parsed.target)):
        parser.error("human-close authorization requires both source and digest and an explicit human-owned h* target.")
    if authority_values[0] and HUMAN_CLOSE_SOURCE_RE.fullmatch(authority_values[0]) is None:
        parser.error("human-close authorization source must be manager_mail/<safe-name>.txt.")
    if authority_values[1] and SHA256_RE.fullmatch(authority_values[1]) is None:
        parser.error("human-close authorization digest must be a lowercase SHA-256 value.")
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
        authority_values[0],
        authority_values[1],
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


def configured_mail_root() -> Path:
    """Return the configured manager-mail directory only from safe local config."""

    try:
        info = LOCAL_ENV_PATH.lstat()
        payload = LOCAL_ENV_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("trusted manager-mail configuration is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise RuntimeError("trusted manager-mail configuration is unsafe")
    matches = re.findall(r'^export OMO_WORK_LOGS_ROOT="([^"\n]+)"$', payload, flags=re.MULTILINE)
    if len(matches) != 1 or not Path(matches[0]).is_absolute():
        raise RuntimeError("trusted manager-mail root is not configured exactly once")
    return Path(matches[0]) / "manager_mail"


def require_nonsymlink_directory(path: Path) -> Path:
    """Validate each component of a trusted absolute directory without links."""

    if not path.is_absolute():
        raise RuntimeError("trusted manager-mail root is not absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimeError("trusted manager-mail root is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("trusted manager-mail root contains a non-directory component")
    return current


def read_human_close_authorization(source: str, expected_sha256: str) -> bytes:
    """Read one exact owner-private human authority record without following links."""

    if HUMAN_CLOSE_SOURCE_RE.fullmatch(source) is None or SHA256_RE.fullmatch(expected_sha256) is None:
        raise RuntimeError("human-close authorization identity is invalid")
    mail_root = require_nonsymlink_directory(configured_mail_root())
    mail_info = mail_root.stat()
    if mail_info.st_uid != os.getuid() or stat.S_IMODE(mail_info.st_mode) & 0o077:
        raise RuntimeError("trusted manager-mail root is not owner-private")
    path = mail_root / source.removeprefix("manager_mail/")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError("human-close authorization source is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("human-close authorization source is unsafe")
        with os.fdopen(os.dup(fd), "rb") as stream:
            payload = stream.read()
    finally:
        os.close(fd)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("human-close authorization source bytes do not match the supplied SHA-256")
    return payload


def task_frontmatter_runat(task_text: str) -> str:
    """Return the sole frontmatter runat value, rejecting ambiguous task binding."""

    lines = task_text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError("human-close task must have frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise RuntimeError("human-close task frontmatter is unterminated") from exc
    runats = [line.partition(":")[2].strip() for line in lines[1:end] if line.partition(":")[0].strip() == "runat"]
    if len(runats) != 1 or not runats[0]:
        raise RuntimeError("human-close task must bind exactly one frontmatter runat target")
    return runats[0]


def human_authorized_target(args: Args) -> str:
    """Return the one symbolic h* target being authorized, if any."""

    return args.human_close_authorized_target or (args.target if is_human_owned_target(args.target) else "")


def reply_authorizes_bound_task(subject: str, body: str, target: str) -> bool:
    """Accept one direct task cancellation bound by quoted thread ownership."""

    if re.match(r"(?i)^re\s*:", subject) is None:
        return False
    direct_body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    if len(HUMAN_CLOSE_REPLY_DIRECTIVE_RE.findall(direct_body)) != 1 or HUMAN_CLOSE_DIRECTIVE_RE.search(direct_body) is not None:
        return False
    owner_binding = re.compile(
        r"(?i)\bfrom the responsible\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)"
        r"(?=$|[\s,.;:])(?:(?!\bfrom the responsible\b).)*?\bowner\b"
    )
    affirmative_binding = re.compile(
        r"(?i)This is a mailbox-compression summary of reports from the responsible\s+"
        r"([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\s+EDA/C\+\+ owner\.\s+"
        r"It does not change task ownership\.\s*"
    )
    quoted_lines: list[str] = []
    for line in body.splitlines():
        quoted = line.lstrip()
        if not quoted.startswith(">"):
            continue
        while quoted.startswith(">"):
            quoted = quoted[1:].lstrip()
        quoted_lines.append(quoted)
    bindings = [match.group(1) for line in quoted_lines for match in owner_binding.finditer(line)]
    affirmative = [match.group(1) for line in quoted_lines if (match := affirmative_binding.fullmatch(line)) is not None]
    return bindings == [target] and affirmative == [target]


def validate_human_close_authorization(args: Args) -> None:
    """Fail closed unless private human authority names this exact task and h* target."""

    target = human_authorized_target(args)
    if not target:
        return
    if not is_human_owned_target(target):
        raise RuntimeError("human-close authorization target must be an explicit human-owned h* target")
    source = args.human_close_authorization_source
    digest = args.human_close_authorization_sha256
    if not source or not digest:
        raise RuntimeError("refusing to stop human-owned target without exact human-close authorization")
    if not args.task_file:
        raise RuntimeError("human-close authorization requires the exact task file")
    task = task_path(args.root, args.task_file)
    task_reference = task_ref(args.root, args.task_file)
    task_text = task.read_text(encoding="utf-8")
    if task_frontmatter_runat(task_text) != target:
        raise RuntimeError("human-close task frontmatter does not bind the exact requested target")
    payload = read_human_close_authorization(source, digest)
    target_bytes = target.encode("utf-8")
    authority_text = payload.decode("utf-8", errors="replace")
    subject_lines = [line[len("Subject:") :].strip() for line in authority_text.splitlines() if line.startswith("Subject:")]
    task_token = re.compile(rf"(?<![A-Za-z0-9_./-]){re.escape(task_reference)}(?![A-Za-z0-9_./-])")
    body = authority_text.replace("\r\n", "\n").partition("\n\n")[2]
    subject_names_task = len(subject_lines) == 1 and task_token.search(subject_lines[0]) is not None
    reply_binds_task = len(subject_lines) == 1 and reply_authorizes_bound_task(subject_lines[0], body, target)
    if not subject_names_task and not reply_binds_task:
        raise RuntimeError("human-close authorization subject does not name the exact task file and reply does not bind it")
    directives = HUMAN_CLOSE_DIRECTIVE_RE.findall(body)
    if subject_names_task and (directives != [target] or target_bytes not in payload):
        raise RuntimeError("human-close authorization does not contain one exact direct close instruction for the target")


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


def frontmatter_tool(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            return ""
        key, sep, value = line.partition(":")
        if sep and key.strip() == "tool":
            return value.strip()
    return ""


def task_tool(args: Args) -> str:
    if not args.task_file or args.dry_run:
        return ""
    return frontmatter_tool(task_path(args.root, args.task_file).read_text(encoding="utf-8"))


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


def wait_shell(target: str, deadline_s: float, tmux_guard: tuple[str, str] | None = None, expected_pane_pid: int = 0) -> bool:
    while time.monotonic() < deadline_s:
        command = current_command(target) if tmux_guard is None else guarded_current_command(target, tmux_guard, expected_pane_pid)
        if command in SHELL_COMMANDS:
            return True
        time.sleep(0.25)
    command = current_command(target) if tmux_guard is None else guarded_current_command(target, tmux_guard, expected_pane_pid)
    return command in SHELL_COMMANDS


def paste_text(target: str, text: str) -> None:
    buffer_name = f"omo-codex-stop-{os.getpid()}-{time.monotonic_ns()}"
    _ = tmux(["set-buffer", "-b", buffer_name, text], check=True)
    try:
        _ = tmux(["paste-buffer", "-b", buffer_name, "-t", target], check=True)
    finally:
        _ = tmux(["delete-buffer", "-b", buffer_name])


def tmux_guard_condition(symbolic_target: str, expected_pane_id: str, expected_pane_pid: int = 0) -> str:
    """Return an exact symbolic/numeric identity predicate for tmux formats."""

    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?", symbolic_target)
    if match is None or re.fullmatch(r"%[0-9]+", expected_pane_id) is None:
        raise RuntimeError("invalid symbolic or numeric target for guarded tmux command")
    session, window, pane = match.groups()
    predicates = [
        f"#{{==:#{{pane_id}},{expected_pane_id}}}",
        f"#{{==:#{{session_name}},{session}}}",
        f"#{{==:#{{window_index}},{window}}}",
    ]
    if expected_pane_pid:
        if expected_pane_pid <= 0:
            raise RuntimeError("invalid expected pane pid for guarded tmux command")
        predicates.append(f"#{{==:#{{pane_pid}},{expected_pane_pid}}}")
    if pane is not None:
        predicates.append(f"#{{==:#{{pane_index}},{pane}}}")
    condition = predicates[-1]
    for predicate in reversed(predicates[:-1]):
        condition = f"#{{&&:{predicate},{condition}}}"
    return condition


def guarded_tmux_sequence(
    symbolic_target: str,
    expected_pane_id: str,
    commands: list[list[str]],
    expected_pane_pid: int = 0,
) -> str:
    """Return pane command output only from the same guarded tmux server queue."""

    accepted = f"OMO_GUARD_ACCEPTED_{os.getpid()}_{time.monotonic_ns()}"
    rejected = f"OMO_GUARD_REJECTED_{os.getpid()}_{time.monotonic_ns()}"
    condition = tmux_guard_condition(symbolic_target, expected_pane_id, expected_pane_pid) if expected_pane_pid else tmux_guard_condition(symbolic_target, expected_pane_id)
    guarded = " ; ".join([*(shlex.join(command) for command in commands), f"display-message -p {accepted}"])
    failure = f"display-message -p {rejected}"
    result = tmux(["if-shell", "-F", "-t", symbolic_target, condition, guarded, failure])
    suffix = accepted + "\n"
    if result.returncode != 0 or not result.stdout.endswith(suffix):
        raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")
    return result.stdout[: -len(suffix)]


def guarded_tmux_read(symbolic_target: str, expected_pane_id: str, command: list[str], expected_pane_pid: int = 0) -> str:
    """Return one pane command's output from the same guarded tmux server queue."""

    if expected_pane_pid:
        return guarded_tmux_sequence(symbolic_target, expected_pane_id, [command], expected_pane_pid)
    return guarded_tmux_sequence(symbolic_target, expected_pane_id, [command])


def guarded_tmux_command(symbolic_target: str, expected_pane_id: str, command: list[str], expected_pane_pid: int = 0) -> None:
    """Run one pane-mutating command only in the matching tmux server command queue."""

    output = guarded_tmux_read(symbolic_target, expected_pane_id, command, expected_pane_pid) if expected_pane_pid else guarded_tmux_read(symbolic_target, expected_pane_id, command)
    if output:
        raise RuntimeError("guarded tmux mutation produced unexpected output")


def bound_guarded_read(symbolic_target: str, expected_pane_id: str, command: list[str], expected_pane_pid: int = 0) -> str:
    """Preserve the legacy call shape when no process binding was requested."""

    return guarded_tmux_read(symbolic_target, expected_pane_id, command, expected_pane_pid) if expected_pane_pid else guarded_tmux_read(symbolic_target, expected_pane_id, command)


def guarded_capture(target: str, n_lines: int, tmux_guard: tuple[str, str], expected_pane_pid: int = 0) -> str:
    command = ["capture-pane", "-p", "-t", target, "-S", f"-{n_lines}"]
    return guarded_tmux_read(*tmux_guard, command, expected_pane_pid) if expected_pane_pid else guarded_tmux_read(*tmux_guard, command)


def guarded_current_command(target: str, tmux_guard: tuple[str, str], expected_pane_pid: int = 0) -> str:
    command = ["display-message", "-p", "-t", target, "#{pane_current_command}"]
    output = guarded_tmux_read(*tmux_guard, command, expected_pane_pid) if expected_pane_pid else guarded_tmux_read(*tmux_guard, command)
    return output.strip()


def guarded_paste_text(target: str, text: str, symbolic_target: str, expected_pane_id: str, expected_pane_pid: int = 0) -> None:
    """Paste text only while tmux itself sees the bound symbolic and numeric target."""

    buffer_name = f"omo-codex-stop-{os.getpid()}-{time.monotonic_ns()}"
    _ = tmux(["set-buffer", "-b", buffer_name, text], check=True)
    try:
        command = ["paste-buffer", "-b", buffer_name, "-t", target]
        if expected_pane_pid:
            guarded_tmux_command(symbolic_target, expected_pane_id, command, expected_pane_pid)
        else:
            guarded_tmux_command(symbolic_target, expected_pane_id, command)
    finally:
        _ = tmux(["delete-buffer", "-b", buffer_name])


def input_has_status_prompt(text: str) -> bool:
    return any(line.lstrip().startswith("› ") and "/status" in line for line in text.splitlines()[-20:])


def query_status_session_id(
    target: str,
    n_lines: int,
    wait_s: float,
    identity_is_current: Callable[[], bool] | None = None,
    tmux_guard: tuple[str, str] | None = None,
    strict_status_response: bool = False,
    expected_pane_pid: int = 0,
    pre_input_check: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Return a status UUID, optionally only from the newly submitted `/status`."""
    if identity_is_current is not None and not identity_is_current():
        raise RuntimeError("tmux pane identity changed before status query")
    before = capture(target, n_lines) if tmux_guard is None else guarded_capture(target, n_lines, tmux_guard, expected_pane_pid)
    if pre_input_check is not None:
        pre_input_check()
    if tmux_guard is None:
        paste_text(target, "/status")
    else:
        guarded_paste_text(target, "/status", *tmux_guard, expected_pane_pid)
    if identity_is_current is not None and not identity_is_current():
        raise RuntimeError("tmux pane identity changed before status submission")
    if tmux_guard is None:
        _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
    else:
        guarded_tmux_command(*tmux_guard, ["send-keys", "-t", target, "Enter"], expected_pane_pid)
    deadline_s = time.monotonic() + wait_s
    after = before
    fallback_sent = False
    while time.monotonic() < deadline_s:
        after = capture(target, n_lines) if tmux_guard is None else guarded_capture(target, n_lines, tmux_guard, expected_pane_pid)
        response = after.rsplit("/status", 1)[-1] if after.count("/status") > before.count("/status") else ""
        session_id = extract_status_session_id(response) if strict_status_response else extract_new_status_session_id(before, after)
        if session_id:
            return session_id, response if strict_status_response else after
        if not fallback_sent and input_has_status_prompt(after):
            if identity_is_current is not None and not identity_is_current():
                raise RuntimeError("tmux pane identity changed before fallback status submission")
            if tmux_guard is None:
                _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
            else:
                guarded_tmux_command(*tmux_guard, ["send-keys", "-t", target, "Enter"], expected_pane_pid)
            fallback_sent = True
        time.sleep(0.25)
    response = (after.rsplit("/status", 1)[-1] if after.count("/status") > before.count("/status") else "") if strict_status_response else after
    return "", response


def send_exit_keys(
    target: str,
    identity_is_current: Callable[[], bool] | None = None,
    tmux_guard: tuple[str, str] | None = None,
    expected_pane_pid: int = 0,
) -> None:
    for _attempt in range(EXIT_INTERRUPT_ATTEMPTS):
        if identity_is_current is not None and not identity_is_current():
            raise RuntimeError("tmux pane identity changed before interrupt")
        if tmux_guard is None:
            _ = tmux(["send-keys", "-t", target, "C-c"], check=True)
        elif expected_pane_pid:
            guarded_tmux_command(*tmux_guard, ["send-keys", "-t", target, "C-c"], expected_pane_pid)
        else:
            guarded_tmux_command(*tmux_guard, ["send-keys", "-t", target, "C-c"])
        time.sleep(EXIT_INTERRUPT_DELAY_S)
        command = current_command(target) if tmux_guard is None else guarded_current_command(target, tmux_guard, expected_pane_pid)
        if command in SHELL_COMMANDS:
            return


def close_tmux_target(target: str) -> None:
    if current_command(target) not in SHELL_COMMANDS:
        return
    if window_panes(target) == 1:
        _ = tmux(["kill-window", "-t", target], check=True)
    else:
        _ = tmux(["kill-pane", "-t", target], check=True)


def close_bound_tmux_target(
    target: str,
    identity_is_current: Callable[[], bool],
    symbolic_target: str,
    expected_pane_id: str,
    proof_path: str = "",
    audit_path: str = "",
    proof_secret: str = "",
    proof_commitment: str = "",
    expected_pane_pid: int = 0,
) -> None:
    """Close a non-human pane only while its symbolic and numeric identities agree."""

    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed before bound close")
    tmux_guard = (symbolic_target, expected_pane_id)
    if guarded_current_command(target, tmux_guard, expected_pane_pid) not in SHELL_COMMANDS:
        return
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed immediately before bound close")
    commands = [["kill-pane", "-t", target]]
    if proof_path or audit_path or proof_secret or proof_commitment:
        if (
            not Path(proof_path).is_absolute()
            or not Path(audit_path).is_absolute()
            or SHA256_RE.fullmatch(proof_secret) is None
            or SHA256_RE.fullmatch(proof_commitment) is None
            or hashlib.sha256(proof_secret.encode()).hexdigest() != proof_commitment
        ):
            raise RuntimeError("bound close proof identity is invalid")
        writer = shlex.join(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--write-bound-close-proof",
                proof_path,
                audit_path,
                proof_secret,
                proof_commitment,
            ]
        )
        commands.append(["run-shell", writer])
    output = guarded_tmux_sequence(symbolic_target, expected_pane_id, commands, expected_pane_pid) if expected_pane_pid else guarded_tmux_sequence(symbolic_target, expected_pane_id, commands)
    if output:
        raise RuntimeError("guarded tmux close produced unexpected output")


def write_bound_close_proof(path: Path, audit_path: Path, secret: str, commitment: str) -> None:
    """Persist a guarded close capability committed by one prepared park audit."""

    expected_path = audit_path.with_name(f".{audit_path.name}.owner-stopped")
    if (
        not path.is_absolute()
        or not audit_path.is_absolute()
        or path != expected_path
        or SHA256_RE.fullmatch(secret) is None
        or SHA256_RE.fullmatch(commitment) is None
        or hashlib.sha256(secret.encode()).hexdigest() != commitment
    ):
        raise RuntimeError("bound close proof identity is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    audit_fd: int | None = None
    try:
        audit_fd = os.open(audit_path, flags)
        audit_info = os.fstat(audit_fd)
        with os.fdopen(os.dup(audit_fd), "r", encoding="utf-8") as source:
            audit_text = source.read(65537)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("bound close proof audit is unavailable") from exc
    finally:
        if audit_fd is not None:
            os.close(audit_fd)
    try:
        audit = yaml.safe_load(audit_text)
    except yaml.YAMLError as exc:
        raise RuntimeError("bound close proof audit is invalid") from exc
    if (
        not stat.S_ISREG(audit_info.st_mode)
        or audit_info.st_uid != os.getuid()
        or stat.S_IMODE(audit_info.st_mode) != 0o600
        or len(audit_text.encode()) > 65536
        or not isinstance(audit, dict)
        or audit.get("operation") not in {"park-unlinked", "manager-replace"}
        or audit.get("state") != "prepared"
        or audit.get("close_proof_commitment") != commitment
    ):
        raise RuntimeError("bound close proof audit does not authorize this exact capability")
    if audit.get("operation") == "manager-replace":
        replacement_path_value = audit.get("audit_path")
        replacement_digest = audit.get("replacement_audit_sha256")
        if not isinstance(replacement_path_value, str) or not isinstance(replacement_digest, str):
            raise RuntimeError("manager replacement close authority is incomplete")
        replacement_path = Path(replacement_path_value)
        expected_authority = replacement_path.with_name(f".{replacement_path.name}.close-authority")
        if not replacement_path.is_absolute() or audit_path != expected_authority or SHA256_RE.fullmatch(replacement_digest) is None:
            raise RuntimeError("manager replacement close authority path is unbound")
        replacement_fd: int | None = None
        try:
            replacement_fd = os.open(replacement_path, flags)
            replacement_before = os.fstat(replacement_fd)
            chunks: list[bytes] = []
            remaining = 8 * 1024 * 1024 + 1
            while remaining:
                chunk = os.read(replacement_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            replacement_bytes = b"".join(chunks)
            replacement_after = os.fstat(replacement_fd)
            replacement_record = json.loads(replacement_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("manager replacement audit is unavailable or invalid") from exc
        finally:
            if replacement_fd is not None:
                os.close(replacement_fd)
        replacement_integrity = replacement_record.pop("record_sha256", None) if isinstance(replacement_record, dict) else None
        if (
            not stat.S_ISREG(replacement_before.st_mode)
            or replacement_before.st_uid != os.getuid()
            or stat.S_IMODE(replacement_before.st_mode) != 0o600
            or (
                replacement_before.st_dev,
                replacement_before.st_ino,
                replacement_before.st_size,
                replacement_before.st_mtime_ns,
                replacement_before.st_ctime_ns,
            )
            != (
                replacement_after.st_dev,
                replacement_after.st_ino,
                replacement_after.st_size,
                replacement_after.st_mtime_ns,
                replacement_after.st_ctime_ns,
            )
            or len(replacement_bytes) > 8 * 1024 * 1024
            or hashlib.sha256(replacement_bytes).hexdigest() != replacement_digest
            or not isinstance(replacement_record, dict)
            or replacement_integrity != hashlib.sha256(json.dumps(replacement_record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            or replacement_record.get("operation") != "manager-replace"
            or replacement_record.get("state") != "prepared"
            or replacement_record.get("close_proof_commitment") != commitment
            or replacement_record.get("old_target") != audit.get("old_target")
            or replacement_record.get("old_pane")
            != {
                "id": audit.get("old_pane_id"),
                "pid": audit.get("old_pane_pid"),
                "start_ticks": audit.get("old_pane_start_ticks"),
            }
        ):
            raise RuntimeError("manager replacement audit does not match its close authority")
    parent = path.parent
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("bound close proof directory must be owner-private")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(secret + "\n")
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def has_bound_close_proof(path: Path, commitment: str) -> bool:
    """Verify one exact durable close proof without targeting any pane."""

    if not path.is_absolute() or SHA256_RE.fullmatch(commitment) is None:
        return False
    try:
        parent_info = path.parent.stat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o077
    ):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as source:
            content = source.read(66)
    except (OSError, UnicodeDecodeError):
        return False
    finally:
        os.close(fd)
    secret = content[:-1] if len(content) == 65 and content.endswith("\n") else ""
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and SHA256_RE.fullmatch(secret) is not None
        and hashlib.sha256(secret.encode()).hexdigest() == commitment
    )


def close_authorized_human_pane(target: str, identity_is_current: Callable[[], bool]) -> None:
    """Close only the exact authorized pane, never its whole human window."""

    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed before authorized human-pane close")
    if current_command(target) not in SHELL_COMMANDS:
        raise RuntimeError("authorized human pane did not exit to a shell before close")
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed immediately before authorized human-pane close")
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
    if is_cursor_agent_capture(lines):
        return "not_codex"
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
    authorized_target = human_authorized_target(args)
    human_authorized = bool(authorized_target)
    if human_authorized:
        validate_human_close_authorization(args)
        if not args.no_feedback:
            raise RuntimeError("human-close authorization requires --no-feedback; do not send unrequested input to a human-owned pane")
    tmux_guard: tuple[str, str] | None = None
    if args.bound_symbolic_target or args.bound_pane_id or args.bound_expected_session_id:
        if (
            not args.bound_symbolic_target
            or not args.bound_pane_id
            or args.target != args.bound_symbolic_target
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?", args.bound_symbolic_target) is None
            or not re.fullmatch(r"%[0-9]+", args.bound_pane_id)
            or (is_human_owned_target(args.bound_symbolic_target) and not human_authorized)
            or bool(args.bound_pane_pid) != bool(args.bound_pane_start_ticks)
            or args.bound_pane_pid < 0
            or args.bound_pane_start_ticks < 0
            or (
                args.bound_expected_session_id
                and re.fullmatch(UUID_RE, args.bound_expected_session_id) is None
            )
        ):
            raise RuntimeError("bound target does not resolve to the exact pinned pane")
        tmux_guard = (args.bound_symbolic_target, args.bound_pane_id)
        initial_pane = bound_guarded_read(
            *tmux_guard,
            ["display-message", "-p", "-t", args.bound_symbolic_target, "#{pane_id}"],
            args.bound_pane_pid,
        ).strip()
        if initial_pane != args.bound_pane_id:
            raise RuntimeError("bound target does not resolve to the exact pinned pane")
        if args.bound_pane_start_ticks and process_start_ticks(args.bound_pane_pid) != args.bound_pane_start_ticks:
            raise RuntimeError("bound target process identity changed")
        target_pane = args.bound_pane_id
    else:
        target_pane = pane_id(args.target)
        if not target_pane:
            raise RuntimeError(f"tmux target not found: {args.target}")
    proof_fields = (
        args.bound_close_proof_path,
        args.bound_close_audit_path,
        args.bound_close_proof_secret,
        args.bound_close_proof_commitment,
    )
    if any(proof_fields) and (not all(proof_fields) or not args.bound_symbolic_target):
        raise RuntimeError("bound close proof requires an exact target, audit, secret, and commitment")
    if tmux_guard is not None and not args.no_feedback:
        raise RuntimeError("bound stop requires --no-feedback so every pane access remains server-guarded")
    if not args.allow_self and target_pane == current_pane_id():
        raise RuntimeError(f"refusing to stop the current pane: {args.target}")
    if args.task_file:
        _ = task_path(args.root, args.task_file)
    if tmux_guard is None:
        resolved_session = target_session_name(target_pane)
        numeric_target = pane_target(target_pane)
    else:
        resolved_session = bound_guarded_read(
            *tmux_guard,
            ["display-message", "-p", "-t", target_pane, "#{session_name}"],
            args.bound_pane_pid,
        ).strip()
        numeric_target = bound_guarded_read(
            *tmux_guard,
            [
                "display-message",
                "-p",
                "-t",
                target_pane,
                "#{session_name}:#{window_index}.#{pane_index}",
            ],
            args.bound_pane_pid,
        ).strip()
    if is_human_owned_target(resolved_session) and (not human_authorized or resolved_session != authorized_target.partition(":")[0]):
        raise RuntimeError(f"refusing to stop human-owned target: {args.target}")
    if human_authorized and numeric_target not in target_aliases(authorized_target):
        raise RuntimeError("human-close target no longer resolves to the exact authorized tmux pane")
    def identity_is_current() -> bool:
        if tmux_guard is not None:
            if args.bound_pane_start_ticks and process_start_ticks(args.bound_pane_pid) != args.bound_pane_start_ticks:
                return False
            try:
                identity = bound_guarded_read(
                    *tmux_guard,
                    [
                        "display-message",
                        "-p",
                        "-t",
                        target_pane,
                        "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}",
                    ],
                    args.bound_pane_pid,
                ).strip()
            except RuntimeError:
                return False
            return identity == f"{target_pane}\t{numeric_target}"
        if pane_id(target_pane) != target_pane or pane_target(target_pane) != numeric_target:
            return False
        if human_authorized:
            return (
                target_session_name(target_pane) == authorized_target.partition(":")[0]
                and numeric_target in target_aliases(authorized_target)
            )
        return True
    if tmux_guard is None:
        report = inspect(StatusArgs(numeric_target, 80)) if numeric_target else None
    else:
        initial_capture = guarded_capture(target_pane, 80, tmux_guard, args.bound_pane_pid)
        lines = [line.rstrip() for line in initial_capture.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        report = report_from_lines(lines)
    if report is None or report.status not in STOPPABLE_CODEX_STATUSES:
        actual = report.status if report is not None else "missing"
        raise RuntimeError(f"target is not a supported live Codex pane: {args.target} status={actual}")
    resolved_args = replace(args, target=target_pane)
    if resolved_args.dry_run:
        print(f"would send Ctrl-C to {resolved_args.target}")
        return ""
    maybe_request_feedback(resolved_args)
    if human_authorized:
        # Re-read both durable authority bindings after the pane is pinned and
        # immediately before the first human-pane input.
        validate_human_close_authorization(args)
    if args.bound_pre_input_check is not None and (not human_authorized or tmux_guard is None or not all(proof_fields)):
        raise RuntimeError("bound pre-input check requires an authorized guarded Human close capability")
    identity_check = identity_is_current if human_authorized or args.bound_symbolic_target else None
    if task_tool(args) == "cursor":
        before_close = (
            capture(resolved_args.target, resolved_args.lines)
            if tmux_guard is None
            else guarded_capture(resolved_args.target, resolved_args.lines, tmux_guard, resolved_args.bound_pane_pid)
        )
        session_id = ""
    elif identity_check is None:
        session_id, before_close = query_status_session_id(resolved_args.target, resolved_args.lines, resolved_args.wait_s)
    else:
        session_id, before_close = query_status_session_id(
            resolved_args.target,
            resolved_args.lines,
            resolved_args.wait_s,
            identity_check,
            tmux_guard,
            strict_status_response=bool(resolved_args.bound_expected_session_id),
            expected_pane_pid=resolved_args.bound_pane_pid,
            pre_input_check=resolved_args.bound_pre_input_check,
        )
    if (
        resolved_args.bound_expected_session_id
        and session_id.lower() != resolved_args.bound_expected_session_id.lower()
    ):
        raise RuntimeError(
            "bound Codex session id mismatch before interrupt: "
            f"expected {resolved_args.bound_expected_session_id.lower()}, found {session_id or '<missing>'}"
        )
    if (identity_check is not None and not identity_is_current()) or (identity_check is None and pane_id(resolved_args.target) != target_pane):
        raise RuntimeError(f"tmux target disappeared before interrupt: {args.target}")
    if identity_check is None:
        send_exit_keys(resolved_args.target)
    else:
        send_exit_keys(resolved_args.target, identity_check, tmux_guard, resolved_args.bound_pane_pid)
    _ = wait_shell(resolved_args.target, time.monotonic() + resolved_args.wait_s, tmux_guard, resolved_args.bound_pane_pid)
    after = (
        capture(resolved_args.target, resolved_args.lines)
        if tmux_guard is None
        else guarded_capture(resolved_args.target, resolved_args.lines, tmux_guard, resolved_args.bound_pane_pid)
    )
    if identity_check is not None and not identity_is_current():
        raise RuntimeError("tmux pane identity changed before close")
    if identity_check is not None and tmux_guard is not None:
        assert tmux_guard is not None
        close_bound_tmux_target(
            resolved_args.target,
            identity_check,
            *tmux_guard,
            args.bound_close_proof_path,
            args.bound_close_audit_path,
            args.bound_close_proof_secret,
            args.bound_close_proof_commitment,
            resolved_args.bound_pane_pid,
        )
    elif human_authorized:
        close_authorized_human_pane(resolved_args.target, identity_is_current)
    else:
        close_tmux_target(resolved_args.target)
    return session_id or extract_exit_resume_id(before_close, after)


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[0] == "--write-bound-close-proof":
        try:
            write_bound_close_proof(Path(argv[1]), Path(argv[2]), argv[3], argv[4])
        except Exception as exc:
            print(f"omo_codex_stop: {exc}", file=sys.stderr)
            return 1
        return 0
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
