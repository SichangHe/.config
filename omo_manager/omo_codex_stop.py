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
import tempfile
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
HUMAN_CLOSE_DIRECTIVE_RE = re.compile(r"(?im)^\s*close\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?=$|[\s,.;:])")
HUMAN_CLOSE_REPLY_DIRECTIVE_RE = re.compile(r"(?im)^\s*cancel\s+this\s+task(?=$|[\s,.;:])")
HUMAN_REPLACE_DIRECTIVE_RE = re.compile(
    r"(?m)^Replace the failed PCODX manager (?P<task>[A-Za-z0-9_./-]+\.md) at "
    r"(?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) with one fresh plain-Codex manager "
    r"inheriting all tasks and comments\.[ \t]*$"
)
HUMAN_REPLACE_CANDIDATE_RE = re.compile(r"(?m)^Replace the failed PCODX manager\b.*$")
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RESUME_RE = re.compile(rf"(?i)\bcodex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b")
EXIT_RESUME_RE = re.compile(rf"(?i)\bTo\s+(?:resume|continue this session),\s+run\s+codex\s+resume\s+(?:--[\w-]+\s+)*({UUID_RE})\b")
STATUS_SESSION_RE = re.compile(rf"\bSession:\s*({UUID_RE})\b")
DONE_LIVE_CLOSE_OPERATION = "done-live-no-mail-close"
DONE_LIVE_CLOSE_AUDIT_KEYS = frozenset(
    {
        "version", "operation", "state", "task", "target", "manager_target",
        "task_sha256", "todo_sha256", "pane_id", "pane_pid",
        "pane_start_ticks", "session_id", "terminal_evidence_sha256",
        "terminal_capture_sha256", "close_proof_commitment", "close_note",
        "completed_task_sha256",
    }
)
DONE_LIVE_CONSUMED_AUDIT_KEYS = DONE_LIVE_CLOSE_AUDIT_KEYS | {"manager_consumed_receipt_sha256"}


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


@dataclass(frozen=True)
class ExitedCodexShell:
    session_id: str
    capture_sha256: str


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
    matches = [match.group(1) for line in after.splitlines() if line.strip() not in before_lines for match in EXIT_RESUME_RE.finditer(line)]
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
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
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
    replacements = [match for match in HUMAN_REPLACE_DIRECTIVE_RE.finditer(body) if match.group("task") == task_reference and match.group("target") in target_aliases(target)]
    replacement_candidates = HUMAN_REPLACE_CANDIDATE_RE.findall(body)
    body_nonempty_lines = [line.strip() for line in body.splitlines() if line.strip()]
    exact_replacement = len(replacement_candidates) == 1 and len(replacements) == 1 and body_nonempty_lines == [replacements[0].group(0).strip(), "Just do it"]
    subject_names_task = len(subject_lines) == 1 and task_token.search(subject_lines[0]) is not None
    reply_binds_task = len(subject_lines) == 1 and reply_authorizes_bound_task(subject_lines[0], body, target)
    if replacement_candidates and not exact_replacement:
        raise RuntimeError("human-close replacement authority must contain one exact task- and target-bound directive")
    if not subject_names_task and not reply_binds_task and not exact_replacement:
        raise RuntimeError("human-close authorization subject does not name the exact task file and reply does not bind it")
    directives = HUMAN_CLOSE_DIRECTIVE_RE.findall(body)
    if (subject_names_task or exact_replacement) and (directives != [target] or target_bytes not in payload) and not exact_replacement:
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
        return f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; session_id: `{session_id}`.)\n"
    return f"\n(manager closed Codex agent {stamp}; tmux target `{target}`; Codex session id not found in captured tmux output.)\n"


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
    if pre_input_check is not None:
        pre_input_check()
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
            if pre_input_check is not None:
                pre_input_check()
            if tmux_guard is None:
                _ = tmux(["send-keys", "-t", target, "Enter"], check=True)
            else:
                guarded_tmux_command(*tmux_guard, ["send-keys", "-t", target, "Enter"], expected_pane_pid)
            fallback_sent = True
        time.sleep(0.25)
    after = capture(target, n_lines) if tmux_guard is None else guarded_capture(target, n_lines, tmux_guard, expected_pane_pid)
    response = (after.rsplit("/status", 1)[-1] if after.count("/status") > before.count("/status") else "") if strict_status_response else after
    session_id = extract_status_session_id(response) if strict_status_response else extract_new_status_session_id(before, after)
    return session_id, response


def send_exit_keys(
    target: str,
    identity_is_current: Callable[[], bool] | None = None,
    tmux_guard: tuple[str, str] | None = None,
    expected_pane_pid: int = 0,
    pre_input_check: Callable[[], None] | None = None,
) -> None:
    for _attempt in range(EXIT_INTERRUPT_ATTEMPTS):
        if identity_is_current is not None and not identity_is_current():
            raise RuntimeError("tmux pane identity changed before interrupt")
        if pre_input_check is not None:
            pre_input_check()
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
    expected_pane_start_ticks: int = 0,
    pre_input_check: Callable[[], None] | None = None,
    proof_operation: str = "",
    proof_audit_sha256: str = "",
) -> None:
    """Close a non-human pane only while its symbolic and numeric identities agree."""

    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed before bound close")
    tmux_guard = (symbolic_target, expected_pane_id)
    if guarded_current_command(target, tmux_guard, expected_pane_pid) not in SHELL_COMMANDS:
        return
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed immediately before bound close")
    if pre_input_check is not None:
        pre_input_check()
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
        if proof_operation:
            if (
                proof_operation != DONE_LIVE_CLOSE_OPERATION
                or SHA256_RE.fullmatch(proof_audit_sha256) is None
                or expected_pane_pid <= 1
                or expected_pane_start_ticks <= 0
            ):
                raise RuntimeError("done-live bound close requires one exact audit and process binding")
        elif proof_audit_sha256:
            raise RuntimeError("bound close audit digest requires an explicit proof operation")
        if expected_pane_pid and expected_pane_start_ticks:
            writer = shlex.join(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--kill-bound-and-write-close-proof",
                    symbolic_target,
                    expected_pane_id,
                    str(expected_pane_pid),
                    str(expected_pane_start_ticks),
                    proof_path,
                    audit_path,
                    proof_secret,
                    proof_commitment,
                    *([proof_operation, proof_audit_sha256] if proof_operation else []),
                ]
            )
            commands = [["run-shell", writer]]
        else:
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
    try:
        output = guarded_tmux_sequence(symbolic_target, expected_pane_id, commands, expected_pane_pid) if expected_pane_pid else guarded_tmux_sequence(symbolic_target, expected_pane_id, commands)
    except RuntimeError:
        if proof_path and has_bound_close_proof(Path(proof_path), proof_commitment, proof_audit_sha256):
            return
        raise
    if output:
        raise RuntimeError("guarded tmux close produced unexpected output")


def done_live_close_audit_authorizes(
    audit_text: str,
    audit: object,
    commitment: str,
    *,
    target: str = "",
    pane_id_value: str = "",
    pane_pid: int = 0,
    pane_start_ticks: int = 0,
) -> bool:
    """Validate the exact terminalized audit permitted to close a done worker."""

    if not isinstance(audit, dict):
        return False
    record: dict[str, object] = {}
    for key, value in audit.items():
        if not isinstance(key, str):
            return False
        record[key] = value
    version = record.get("version")
    expected_keys = DONE_LIVE_CONSUMED_AUDIT_KEYS if version == "v2.0.0" else DONE_LIVE_CLOSE_AUDIT_KEYS
    if set(record) != expected_keys:
        return False
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    task = record.get("task")
    owner_target = record.get("target")
    manager_target = record.get("manager_target")
    audit_pane_id = record.get("pane_id")
    audit_pane_pid = record.get("pane_pid")
    audit_start_ticks = record.get("pane_start_ticks")
    task_path_value = Path(task) if isinstance(task, str) else Path()
    exact_target = r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?"
    if (
        audit_text != canonical
        or version not in {"v1.0.0", "v2.0.0"}
        or (version == "v1.0.0" and "manager_consumed_receipt_sha256" in record)
        or (
            version == "v2.0.0"
            and SHA256_RE.fullmatch(str(record.get("manager_consumed_receipt_sha256"))) is None
        )
        or record.get("operation") != DONE_LIVE_CLOSE_OPERATION
        or record.get("state") != "terminalized"
        or not isinstance(task, str)
        or not task.endswith(".md")
        or task_path_value.is_absolute()
        or not task_path_value.parts
        or any(part in {"", ".", ".."} for part in task_path_value.parts)
        or not isinstance(owner_target, str)
        or re.fullmatch(exact_target, owner_target) is None
        or owner_target.partition(":")[0].startswith("h")
        or not isinstance(manager_target, str)
        or re.fullmatch(exact_target, manager_target) is None
        or manager_target.partition(":")[0].startswith("h")
        or not isinstance(audit_pane_id, str)
        or re.fullmatch(r"%[0-9]+", audit_pane_id) is None
        or type(audit_pane_pid) is not int
        or audit_pane_pid <= 1
        or type(audit_start_ticks) is not int
        or audit_start_ticks <= 0
        or not isinstance(record.get("session_id"), str)
        or re.fullmatch(UUID_RE, str(record.get("session_id"))) is None
        or any(SHA256_RE.fullmatch(str(record.get(field))) is None for field in ("task_sha256", "todo_sha256", "terminal_evidence_sha256", "terminal_capture_sha256"))
        or record.get("close_proof_commitment") != commitment
        or record.get("close_note") != ""
        or record.get("completed_task_sha256") != ""
    ):
        return False
    if target and (owner_target, audit_pane_id, audit_pane_pid, audit_start_ticks) != (target, pane_id_value, pane_pid, pane_start_ticks):
        return False
    return True


def validate_done_live_close_audit_file(
    audit_path: Path,
    commitment: str,
    target: str,
    pane_id_value: str,
    pane_pid: int,
    pane_start_ticks: int,
    expected_audit_sha256: str,
) -> None:
    """Revalidate the done-live close authority inside the bound child."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(audit_path, flags)
        before = os.fstat(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as source:
            audit_text = source.read(65537)
        after = os.fstat(fd)
        current = audit_path.lstat()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("done-live close audit is unavailable inside the bound close") from exc
    finally:
        if fd is not None:
            os.close(fd)
    try:
        audit: object = json.loads(audit_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("done-live close audit is invalid inside the bound close") from exc
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    current_identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or len(audit_text.encode()) > 65536
        or SHA256_RE.fullmatch(expected_audit_sha256) is None
        or hashlib.sha256(audit_text.encode()).hexdigest() != expected_audit_sha256
        or identity != after_identity
        or identity != current_identity
        or not done_live_close_audit_authorizes(
            audit_text,
            audit,
            commitment,
            target=target,
            pane_id_value=pane_id_value,
            pane_pid=pane_pid,
            pane_start_ticks=pane_start_ticks,
        )
    ):
        raise RuntimeError("done-live close audit drifted before exact pane kill")


def done_live_close_started_path(audit_path: Path) -> Path:
    """Return the durable pre-kill marker path for one done-live audit."""

    return audit_path.with_name(f".{audit_path.name}.owner-close-started")


def done_live_close_marker_text(secret: str, audit_sha256: str) -> str:
    """Render one canonical proof marker bound to exact terminalized audit bytes."""

    record = {
        "audit_sha256": audit_sha256,
        "operation": DONE_LIVE_CLOSE_OPERATION,
        "secret": secret,
        "version": "v1.0.0",
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


def path_entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists without following symlinks."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def bound_close_secret(path: Path, commitment: str, expected_audit_sha256: str = "") -> str:
    """Read an exact owner-private close proof, returning its committed secret."""

    if (
        not path.is_absolute()
        or SHA256_RE.fullmatch(commitment) is None
        or (expected_audit_sha256 and SHA256_RE.fullmatch(expected_audit_sha256) is None)
    ):
        return ""
    try:
        parent_info = path.parent.stat()
    except OSError:
        return ""
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
        return ""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return ""
    try:
        before = os.fstat(fd)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as source:
            content = source.read(1025)
        after = os.fstat(fd)
        current = path.lstat()
    except (OSError, UnicodeDecodeError):
        return ""
    finally:
        os.close(fd)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or len(content.encode()) > 1024
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or identity != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    ):
        return ""
    secret = ""
    if expected_audit_sha256:
        try:
            record: object = json.loads(content)
        except json.JSONDecodeError:
            return ""
        if not isinstance(record, dict) or set(record) != {"audit_sha256", "operation", "secret", "version"}:
            return ""
        secret_value = record.get("secret")
        if (
            content != json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            or record.get("version") != "v1.0.0"
            or record.get("operation") != DONE_LIVE_CLOSE_OPERATION
            or record.get("audit_sha256") != expected_audit_sha256
            or not isinstance(secret_value, str)
        ):
            return ""
        secret = secret_value
    elif len(content) == 65 and content.endswith("\n"):
        secret = content[:-1]
    if SHA256_RE.fullmatch(secret) is None or hashlib.sha256(secret.encode()).hexdigest() != commitment:
        return ""
    return secret


def write_done_live_close_started(
    proof_path: Path,
    audit_path: Path,
    secret: str,
    commitment: str,
    expected_audit_sha256: str,
    target: str,
    pane_id_value: str,
    pane_pid: int,
    pane_start_ticks: int,
) -> Path:
    """Durably record the exact close intent before the pane kill."""

    started_path = done_live_close_started_path(audit_path)
    expected_proof_path = audit_path.with_name(f".{audit_path.name}.owner-stopped")
    if (
        not proof_path.is_absolute()
        or not audit_path.is_absolute()
        or proof_path != expected_proof_path
        or SHA256_RE.fullmatch(secret) is None
        or hashlib.sha256(secret.encode()).hexdigest() != commitment
    ):
        raise RuntimeError("done-live close-started identity is invalid")
    validate_done_live_close_audit_file(
        audit_path,
        commitment,
        target,
        pane_id_value,
        pane_pid,
        pane_start_ticks,
        expected_audit_sha256,
    )
    if path_entry_exists(proof_path):
        raise RuntimeError("done-live final close proof already exists before pane kill")
    existing = bound_close_secret(started_path, commitment, expected_audit_sha256)
    if existing:
        if existing != secret:
            raise RuntimeError("done-live close-started marker secret drifted")
        return started_path
    if path_entry_exists(started_path):
        raise RuntimeError("done-live close-started marker is malformed")
    parent_info = audit_path.parent.stat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise RuntimeError("done-live close-started directory must be owner-private")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=audit_path.parent, prefix=f".{audit_path.name}.close-started.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(done_live_close_marker_text(secret, expected_audit_sha256))
            output.flush()
            os.fsync(output.fileno())
        validate_done_live_close_audit_file(
            audit_path,
            commitment,
            target,
            pane_id_value,
            pane_pid,
            pane_start_ticks,
            expected_audit_sha256,
        )
        if path_entry_exists(proof_path):
            raise RuntimeError("done-live final close proof appeared before pane kill")
        try:
            os.link(temporary, started_path, follow_symlinks=False)
        except FileExistsError:
            existing = bound_close_secret(started_path, commitment, expected_audit_sha256)
            if existing != secret:
                raise RuntimeError("done-live close-started marker raced with different evidence") from None
        directory_fd = os.open(audit_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if bound_close_secret(started_path, commitment, expected_audit_sha256) != secret:
            raise RuntimeError("done-live close-started marker changed after durable creation")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return started_path


def write_bound_close_proof(path: Path, audit_path: Path, secret: str, commitment: str) -> None:
    """Persist a guarded close capability committed by one exact lifecycle audit."""

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
    legacy_authorized = (
        isinstance(audit, dict)
        and audit.get("operation") in {"park-unlinked", "manager-replace"}
        and audit.get("state") == "prepared"
        and audit.get("close_proof_commitment") == commitment
    )
    if (
        not stat.S_ISREG(audit_info.st_mode)
        or audit_info.st_uid != os.getuid()
        or stat.S_IMODE(audit_info.st_mode) != 0o600
        or len(audit_text.encode()) > 65536
        or not legacy_authorized
    ):
        raise RuntimeError("bound close proof audit does not authorize this exact capability")
    if audit.get("operation") == "manager-replace":
        replacement_path_value = audit.get("audit_path")
        replacement_digest = audit.get("replacement_audit_sha256")
        if not isinstance(replacement_path_value, str) or not isinstance(replacement_digest, str):
            raise RuntimeError("manager replacement close authority is incomplete")
        replacement_path = Path(replacement_path_value)
        expected_authority = replacement_path.with_name(f".{replacement_path.name}.close-authority")
        if not replacement_path.is_absolute() or SHA256_RE.fullmatch(replacement_digest) is None:
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
            or audit_path not in manager_replace_close_authority_paths(replacement_path, replacement_record, expected_authority)
            or not manager_replace_close_identity_matches(replacement_record, audit, commitment)
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


def manager_replace_close_identity_matches(replacement: dict[str, object], authority: dict[str, object], commitment: str) -> bool:
    expected = {
        "id": authority.get("old_pane_id"),
        "pid": authority.get("old_pane_pid"),
        "start_ticks": authority.get("old_pane_start_ticks"),
    }
    if replacement.get("old_target") == authority.get("old_target") and replacement.get("old_pane") == expected:
        return replacement.get("close_proof_commitment") == commitment
    descendants = replacement.get("descendants")
    commitments = replacement.get("descendant_close_commitments")
    if not isinstance(descendants, list) or not isinstance(commitments, list) or len(descendants) != len(commitments):
        return False
    for child, child_commitment in zip(descendants, commitments, strict=True):
        if not isinstance(child, dict):
            return False
        if (
            child.get("target") == authority.get("old_target")
            and child.get("pane_id") == expected["id"]
            and child.get("pane_pid") == expected["pid"]
            and child.get("pane_start_ticks") == expected["start_ticks"]
        ):
            return child_commitment == commitment
    return False


def manager_replace_close_authority_paths(replacement_path: Path, replacement: dict[str, object], manager_authority: Path) -> set[Path]:
    paths = {manager_authority}
    descendants = replacement.get("descendants")
    if not isinstance(descendants, list):
        return paths
    for child in descendants:
        if not isinstance(child, dict) or not isinstance(child.get("task"), str):
            return set()
        token = hashlib.sha256(child["task"].encode()).hexdigest()[:16]
        paths.add(replacement_path.with_name(f".{replacement_path.name}.descendant-{token}-close-authority"))
    return paths


def kill_bound_and_write_close_proof(
    symbolic_target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    proof_path: Path,
    audit_path: Path,
    secret: str,
    commitment: str,
    proof_operation: str = "",
    expected_audit_sha256: str = "",
) -> None:
    """Kill one revalidated pane with a crash-recoverable close capability."""

    # 🧑 "Atomically close only exact failed `guest_hees:0` ... Verify old owner absent"
    if proof_operation:
        if proof_operation != DONE_LIVE_CLOSE_OPERATION or SHA256_RE.fullmatch(expected_audit_sha256) is None:
            raise RuntimeError("bound close proof operation is unsupported")
        validate_done_live_close_audit_file(
            audit_path,
            commitment,
            symbolic_target,
            expected_pane_id,
            expected_pane_pid,
            expected_pane_start_ticks,
            expected_audit_sha256,
        )
    if (
        expected_pane_pid <= 0
        or expected_pane_start_ticks <= 0
        or pane_id(symbolic_target) != expected_pane_id
        or pane_id(expected_pane_id) != expected_pane_id
        or process_start_ticks(expected_pane_pid) != expected_pane_start_ticks
    ):
        raise RuntimeError("bound close identity changed before exact pane kill")
    if proof_operation:
        write_done_live_close_started(
            proof_path,
            audit_path,
            secret,
            commitment,
            expected_audit_sha256,
            symbolic_target,
            expected_pane_id,
            expected_pane_pid,
            expected_pane_start_ticks,
        )
        validate_done_live_close_audit_file(
            audit_path,
            commitment,
            symbolic_target,
            expected_pane_id,
            expected_pane_pid,
            expected_pane_start_ticks,
            expected_audit_sha256,
        )
        if (
            pane_id(symbolic_target) != expected_pane_id
            or pane_id(expected_pane_id) != expected_pane_id
            or process_start_ticks(expected_pane_pid) != expected_pane_start_ticks
        ):
            raise RuntimeError("bound close identity changed after durable close-started evidence")
        validate_done_live_close_audit_file(
            audit_path,
            commitment,
            symbolic_target,
            expected_pane_id,
            expected_pane_pid,
            expected_pane_start_ticks,
            expected_audit_sha256,
        )
        if bound_close_secret(done_live_close_started_path(audit_path), commitment, expected_audit_sha256) != secret:
            raise RuntimeError("done-live close-started marker drifted before exact pane kill")
        output = guarded_tmux_sequence(
            symbolic_target,
            expected_pane_id,
            [["kill-pane", "-t", expected_pane_id]],
            expected_pane_pid,
        )
        if output:
            raise RuntimeError("bound close produced unexpected output")
    else:
        _ = tmux(["kill-pane", "-t", expected_pane_id], check=True)
    deadline_s = time.monotonic() + 5.0
    while process_start_ticks(expected_pane_pid) is not None and time.monotonic() < deadline_s:
        time.sleep(0.05)
    if pane_id(symbolic_target) or pane_id(expected_pane_id) or process_start_ticks(expected_pane_pid) is not None:
        raise RuntimeError("bound close could not prove exact pane and process absence")
    if proof_operation:
        promote_done_live_close_started(
            proof_path,
            audit_path,
            commitment,
            expected_audit_sha256,
            symbolic_target,
            expected_pane_id,
            expected_pane_pid,
            expected_pane_start_ticks,
        )
    else:
        write_bound_close_proof(proof_path, audit_path, secret, commitment)


def has_bound_close_proof(path: Path, commitment: str, expected_audit_sha256: str = "") -> bool:
    """Verify one exact durable close proof without targeting any pane."""

    return bool(bound_close_secret(path, commitment, expected_audit_sha256))


def done_live_close_identity_is_absent(symbolic_target: str, expected_pane_id: str, expected_pane_pid: int) -> bool:
    """Require both tmux names and the pinned process identity to be absent."""

    return not pane_id(symbolic_target) and not pane_id(expected_pane_id) and process_start_ticks(expected_pane_pid) is None


def promote_done_live_close_started(
    proof_path: Path,
    audit_path: Path,
    commitment: str,
    expected_audit_sha256: str,
    symbolic_target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
) -> str:
    """Promote durable close intent after exact absence, tolerating link/unlink crashes."""

    expected_proof_path = audit_path.with_name(f".{audit_path.name}.owner-stopped")
    started_path = done_live_close_started_path(audit_path)
    if not proof_path.is_absolute() or not audit_path.is_absolute() or proof_path != expected_proof_path:
        raise RuntimeError("done-live close proof path is not bound to its audit")
    validate_done_live_close_audit_file(
        audit_path,
        commitment,
        symbolic_target,
        expected_pane_id,
        expected_pane_pid,
        expected_pane_start_ticks,
        expected_audit_sha256,
    )
    final_secret = bound_close_secret(proof_path, commitment, expected_audit_sha256)
    started_secret = bound_close_secret(started_path, commitment, expected_audit_sha256)
    if path_entry_exists(proof_path) and not final_secret:
        raise RuntimeError("done-live final close proof is malformed")
    if path_entry_exists(started_path) and not started_secret:
        raise RuntimeError("done-live close-started marker is malformed")
    if not final_secret and not started_secret:
        raise RuntimeError("done-live close lacks durable pre-kill evidence")
    if final_secret and started_secret:
        final_info = proof_path.lstat()
        started_info = started_path.lstat()
        if (final_info.st_dev, final_info.st_ino) != (started_info.st_dev, started_info.st_ino) or final_secret != started_secret:
            raise RuntimeError("done-live close proof and started marker are not one atomic promotion")
    if not done_live_close_identity_is_absent(symbolic_target, expected_pane_id, expected_pane_pid):
        raise RuntimeError("done-live close proof cannot advance while pane identity remains live")
    validate_done_live_close_audit_file(
        audit_path,
        commitment,
        symbolic_target,
        expected_pane_id,
        expected_pane_pid,
        expected_pane_start_ticks,
        expected_audit_sha256,
    )
    if not final_secret:
        try:
            os.link(started_path, proof_path, follow_symlinks=False)
        except FileExistsError:
            final_info = proof_path.lstat()
            started_info = started_path.lstat()
            if (final_info.st_dev, final_info.st_ino) != (started_info.st_dev, started_info.st_ino):
                raise RuntimeError("done-live close proof raced with different evidence") from None
        directory_fd = os.open(audit_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        final_secret = bound_close_secret(proof_path, commitment, expected_audit_sha256)
        if final_secret != started_secret:
            raise RuntimeError("done-live close proof promotion lost its exact evidence")
    if not done_live_close_identity_is_absent(symbolic_target, expected_pane_id, expected_pane_pid):
        raise RuntimeError("done-live pane identity appeared during close-proof promotion")
    if path_entry_exists(started_path):
        final_info = proof_path.lstat()
        started_info = started_path.lstat()
        if (final_info.st_dev, final_info.st_ino) != (started_info.st_dev, started_info.st_ino):
            raise RuntimeError("done-live close-started marker changed before cleanup")
        started_path.unlink()
        directory_fd = os.open(audit_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    if bound_close_secret(proof_path, commitment, expected_audit_sha256) != final_secret:
        raise RuntimeError("done-live close proof changed after durable promotion")
    return final_secret


def close_authorized_human_pane(target: str, identity_is_current: Callable[[], bool]) -> None:
    """Close only the exact authorized pane, never its whole human window."""

    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed before authorized human-pane close")
    if current_command(target) not in SHELL_COMMANDS:
        raise RuntimeError("authorized human pane did not exit to a shell before close")
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed immediately before authorized human-pane close")
    _ = tmux(["kill-pane", "-t", target], check=True)


def _terminalize_bound_codex_to_shell(
    target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    expected_session_id: str,
    terminal_evidence: str,
    evidence_is_current: Callable[[], None],
    *,
    accepted_terminal_report: bool = False,
    wait_s: float = 10.0,
    n_lines: int = 2000,
) -> ExitedCodexShell:
    """Exit one exact non-human Codex process while preserving its shell pane."""

    if not re.fullmatch(r"%[0-9]+", expected_pane_id):
        raise RuntimeError("expected pane id must be an exact numeric tmux pane id")
    if expected_pane_pid <= 1 or expected_pane_start_ticks <= 0:
        raise RuntimeError("expected pane process identity must be exact")
    if not re.fullmatch(UUID_RE, expected_session_id):
        raise RuntimeError("session id must be an exact Codex UUID")
    evidence = terminal_evidence.strip()
    if len(evidence) < 12:
        raise RuntimeError("terminal evidence must be a specific nonempty report token")
    if wait_s < 0 or n_lines <= 0:
        raise RuntimeError("terminalization bounds are invalid")
    if is_human_owned_target(target) or is_human_owned_target(target_session_name(expected_pane_id)):
        raise RuntimeError(f"refusing to terminalize human-owned target: {target}")
    if pane_id(target) != expected_pane_id or pane_id(expected_pane_id) != expected_pane_id:
        raise RuntimeError(f"target no longer resolves to expected pane {expected_pane_id}")
    if expected_pane_id == current_pane_id():
        raise RuntimeError(f"refusing to terminalize the current pane: {expected_pane_id}")
    if process_start_ticks(expected_pane_pid) != expected_pane_start_ticks:
        raise RuntimeError("bound target process identity changed")
    tmux_guard = (target, expected_pane_id)
    numeric_target = bound_guarded_read(
        *tmux_guard,
        ["display-message", "-p", "-t", expected_pane_id, "#{session_name}:#{window_index}.#{pane_index}"],
        expected_pane_pid,
    ).strip()

    def identity_is_current() -> bool:
        if process_start_ticks(expected_pane_pid) != expected_pane_start_ticks:
            return False
        try:
            identity = bound_guarded_read(
                *tmux_guard,
                ["display-message", "-p", "-t", expected_pane_id, "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"],
                expected_pane_pid,
            ).strip()
        except RuntimeError:
            return False
        return identity == f"{expected_pane_id}\t{numeric_target}"

    initial_capture = guarded_capture(expected_pane_id, n_lines, tmux_guard, expected_pane_pid)
    lines = [line.rstrip() for line in initial_capture.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    report = report_from_lines(lines)
    if report.status not in STOPPABLE_CODEX_STATUSES:
        raise RuntimeError(f"target is not a supported live Codex pane: {target} status={report.status}")
    marker_count = initial_capture.count("Conversation interrupted")
    if marker_count > 1:
        raise RuntimeError("bound Codex pane has ambiguous interruption markers")
    marker_at = initial_capture.rfind("Conversation interrupted")
    if marker_count == 1 and EXIT_RESUME_RE.search(initial_capture[marker_at:]):
        raise RuntimeError("bound Codex pane contains a completed prior exit marker")
    evidence_region = initial_capture if marker_count == 0 else initial_capture[:marker_at]
    compact_capture = re.sub(r"\s+", "", evidence_region)
    accepted_at = compact_capture.rfind('"accepted":true')
    if not accepted_terminal_report and (accepted_at < 0 or evidence not in compact_capture[accepted_at:]):
        raise RuntimeError("accepted terminal report evidence is absent before terminalization")
    evidence_is_current()
    session_id, before_close = query_status_session_id(
        expected_pane_id,
        n_lines,
        wait_s,
        identity_is_current,
        tmux_guard,
        strict_status_response=True,
        expected_pane_pid=expected_pane_pid,
        pre_input_check=evidence_is_current,
    )
    if session_id.lower() != expected_session_id.lower():
        raise RuntimeError(f"bound Codex session id mismatch before interrupt: expected {expected_session_id.lower()}, found {session_id or '<missing>'}")
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed before interrupt")
    send_exit_keys(expected_pane_id, identity_is_current, tmux_guard, expected_pane_pid, evidence_is_current)
    if not wait_shell(expected_pane_id, time.monotonic() + wait_s, tmux_guard, expected_pane_pid):
        raise RuntimeError("bound Codex pane did not exit to a shell")
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed after terminalization")
    capture_sha256 = _validate_exited_codex_shell(
        target,
        expected_pane_id,
        session_id,
        evidence,
        n_lines,
        accepted_terminal_report=accepted_terminal_report,
    )
    if not identity_is_current():
        raise RuntimeError("tmux pane identity changed during shell authentication")
    evidence_is_current()
    if not before_close:
        raise RuntimeError("bound Codex status response was empty")
    return ExitedCodexShell(session_id.lower(), capture_sha256)


def terminalize_bound_codex_to_shell(
    target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    expected_session_id: str,
    terminal_evidence: str,
    evidence_is_current: Callable[[], None],
    *,
    wait_s: float = 10.0,
    n_lines: int = 2000,
) -> ExitedCodexShell:
    """Exit one exact Codex process after its accepted report is visible."""

    return _terminalize_bound_codex_to_shell(
        target, expected_pane_id, expected_pane_pid, expected_pane_start_ticks,
        expected_session_id, terminal_evidence, evidence_is_current, wait_s=wait_s, n_lines=n_lines,
    )


def terminalize_bound_codex_to_shell_with_consumed_report(
    target: str,
    expected_pane_id: str,
    expected_pane_pid: int,
    expected_pane_start_ticks: int,
    expected_session_id: str,
    terminal_evidence: str,
    evidence_is_current: Callable[[], None],
    *,
    wait_s: float = 10.0,
    n_lines: int = 2000,
) -> ExitedCodexShell:
    """Exit one exact Codex process after external manager acceptance is authenticated."""

    return _terminalize_bound_codex_to_shell(
        target, expected_pane_id, expected_pane_pid, expected_pane_start_ticks,
        expected_session_id, terminal_evidence, evidence_is_current,
        accepted_terminal_report=True, wait_s=wait_s, n_lines=n_lines,
    )


def _validate_exited_codex_shell(
    target: str,
    expected_pane_id: str,
    session_id: str,
    terminal_evidence: str,
    n_lines: int = 2000,
    *,
    accepted_terminal_report: bool = False,
) -> str:
    """Authenticate one unchanged shell pane and return its exact capture digest."""

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
    compact_report = re.sub(r"\s+", "", before[:interrupted_at])
    accepted_at = compact_report.rfind('"accepted":true')
    if before.count("Conversation interrupted") != 1 or (
        not accepted_terminal_report and (accepted_at < 0 or evidence not in compact_report[accepted_at:])
    ):
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
    return hashlib.sha256(before.encode()).hexdigest()


def validate_exited_codex_shell(target: str, expected_pane_id: str, session_id: str, terminal_evidence: str, n_lines: int = 2000) -> str:
    """Authenticate a shell pane retaining exact visible accepted-report evidence."""

    return _validate_exited_codex_shell(target, expected_pane_id, session_id, terminal_evidence, n_lines)


def validate_exited_codex_shell_with_consumed_report(
    target: str,
    expected_pane_id: str,
    session_id: str,
    terminal_evidence: str,
    n_lines: int = 2000,
) -> str:
    """Authenticate a shell pane whose report acceptance was externally authenticated."""

    return _validate_exited_codex_shell(
        target, expected_pane_id, session_id, terminal_evidence, n_lines,
        accepted_terminal_report=True,
    )


def close_exited_codex_shell(
    target: str,
    expected_pane_id: str,
    session_id: str,
    terminal_evidence: str,
    n_lines: int = 2000,
    *,
    expected_capture_sha256: str = "",
    evidence_is_current: Callable[[], bool] | None = None,
) -> None:
    """Close one unchanged shell pane that retains exact terminal Codex evidence."""

    capture_sha256 = validate_exited_codex_shell(target, expected_pane_id, session_id, terminal_evidence, n_lines)
    if expected_capture_sha256 and capture_sha256 != expected_capture_sha256:
        raise RuntimeError("terminal shell capture changed after its durable close intent")
    if evidence_is_current is not None and not evidence_is_current():
        raise RuntimeError("bound lifecycle evidence changed before exited-shell close")
    close_tmux_target(expected_pane_id)
    if pane_id(expected_pane_id):
        raise RuntimeError(f"exact stale shell pane remained live after close: {expected_pane_id}")


def close_exited_codex_shell_with_task_receipt(
    target: str,
    expected_pane_id: str,
    session_id: str,
    task_payload: bytes,
    expected_task_sha256: str,
    task_receipt: str,
    accepted_message_id: str,
    *,
    session_payload: bytes,
    expected_session_sha256: str,
    expected_completion_command: str,
    n_lines: int = 2000,
) -> None:
    """Close one exited shell by cross-binding task, session, and pane evidence."""

    if SHA256_RE.fullmatch(expected_task_sha256) is None or hashlib.sha256(task_payload).hexdigest() != expected_task_sha256:
        raise RuntimeError("exited-shell task bytes do not match the supplied immutable digest")
    try:
        task_text = task_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("exited-shell task evidence is not UTF-8") from exc
    receipt = task_receipt.strip()
    message_id = accepted_message_id.strip()
    if len(receipt) < 12 or len(message_id) < 12:
        raise RuntimeError("exited-shell task receipt and Message-ID must be specific evidence tokens")
    compact_task = re.sub(r"\s+", "", task_text)
    if receipt not in compact_task or f"acceptedasMessage-ID<{message_id}>" not in compact_task:
        raise RuntimeError("immutable task bytes do not bind the accepted receipt and Message-ID")
    if SHA256_RE.fullmatch(expected_session_sha256) is None or hashlib.sha256(session_payload).hexdigest() != expected_session_sha256:
        raise RuntimeError("exited-shell session bytes do not match the supplied immutable digest")
    try:
        session_text = session_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("exited-shell session evidence is not UTF-8") from exc
    records: list[dict[str, object]] = []
    try:
        for line in session_text.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RuntimeError("exited-shell session JSONL contains a non-object record")
            records.append(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("exited-shell session evidence is not valid JSONL") from exc
    session_metadata = [value.get("payload") for value in records if value.get("type") == "session_meta"]
    if len(session_metadata) != 1 or not isinstance(session_metadata[0], dict) or session_metadata[0].get("id") != session_id:
        raise RuntimeError("exited-shell session evidence does not bind one exact session id")
    accepted_indexes: list[int] = []
    completion_indexes: list[int] = []
    expected_completion_output = f"Emailed the human\nMessage-ID: <{message_id}>\n"
    for index, value in enumerate(records):
        payload = value.get("payload")
        if value.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution" or item.get("status") != "completed":
            continue
        stdout = item.get("stdout")
        if isinstance(stdout, str):
            try:
                accepted = json.loads(stdout)
            except json.JSONDecodeError:
                accepted = None
            if (
                isinstance(accepted, dict)
                and accepted.get("accepted") is True
                and accepted.get("manager_acknowledged") is True
                and accepted.get("reason") == "manager acknowledged routed report"
                and accepted.get("receipt_id") == receipt
            ):
                if item.get("exit_code") != 0 or item.get("aggregated_output") != stdout:
                    raise RuntimeError("accepted task receipt command did not complete cleanly")
                accepted_indexes.append(index)
        if stdout == expected_completion_output:
            if item.get("exit_code") != 0 or item.get("aggregated_output") != expected_completion_output or item.get("command") != ["/usr/bin/zsh", "-lc", expected_completion_command]:
                raise RuntimeError("completion notice command does not match the bound successful invocation")
            completion_indexes.append(index)
    if len(accepted_indexes) != 1 or len(completion_indexes) != 1 or accepted_indexes[0] >= completion_indexes[0]:
        raise RuntimeError("immutable session does not bind one accepted receipt followed by one completion notice")
    if not re.fullmatch(r"%[0-9]+", expected_pane_id):
        raise RuntimeError("expected pane id must be an exact numeric tmux pane id")
    if is_human_owned_target(target) or is_human_owned_target(target_session_name(expected_pane_id)):
        raise RuntimeError(f"refusing to close human-owned target: {target}")
    if not re.fullmatch(UUID_RE, session_id):
        raise RuntimeError("session id must be an exact Codex UUID")
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
    if before.count("Conversation interrupted") != 1 or interrupted_at < 0:
        raise RuntimeError("terminal transcript lacks one unambiguous final Codex exit marker")
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
        raise RuntimeError("pane identity or cross-bound evidence changed during recovery; retry")
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
        'then run `omo_report.sh --status done --message-file "$REPORT_FILE"`. '
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
            or (args.bound_expected_session_id and re.fullmatch(UUID_RE, args.bound_expected_session_id) is None)
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
            return target_session_name(target_pane) == authorized_target.partition(":")[0] and numeric_target in target_aliases(authorized_target)
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
    if args.bound_pre_input_check is not None and (tmux_guard is None or not all(proof_fields) or (not human_authorized and not args.bound_expected_session_id)):
        raise RuntimeError("bound pre-input check requires a session-bound guarded close capability")
    identity_check = identity_is_current if human_authorized or args.bound_symbolic_target else None
    if task_tool(args) == "cursor":
        before_close = (
            capture(resolved_args.target, resolved_args.lines) if tmux_guard is None else guarded_capture(resolved_args.target, resolved_args.lines, tmux_guard, resolved_args.bound_pane_pid)
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
    if resolved_args.bound_expected_session_id and session_id.lower() != resolved_args.bound_expected_session_id.lower():
        raise RuntimeError(f"bound Codex session id mismatch before interrupt: expected {resolved_args.bound_expected_session_id.lower()}, found {session_id or '<missing>'}")
    if (identity_check is not None and not identity_is_current()) or (identity_check is None and pane_id(resolved_args.target) != target_pane):
        raise RuntimeError(f"tmux target disappeared before interrupt: {args.target}")
    if identity_check is None:
        send_exit_keys(resolved_args.target)
    else:
        send_exit_keys(
            resolved_args.target,
            identity_check,
            tmux_guard,
            resolved_args.bound_pane_pid,
            resolved_args.bound_pre_input_check,
        )
    _ = wait_shell(resolved_args.target, time.monotonic() + resolved_args.wait_s, tmux_guard, resolved_args.bound_pane_pid)
    after = capture(resolved_args.target, resolved_args.lines) if tmux_guard is None else guarded_capture(resolved_args.target, resolved_args.lines, tmux_guard, resolved_args.bound_pane_pid)
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
            resolved_args.bound_pane_start_ticks,
            resolved_args.bound_pre_input_check,
        )
    elif human_authorized:
        close_authorized_human_pane(resolved_args.target, identity_is_current)
    else:
        close_tmux_target(resolved_args.target)
    return session_id or extract_exit_resume_id(before_close, after)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--kill-bound-and-write-close-proof":
        if len(argv) not in {9, 11}:
            print("omo_codex_stop.py: bound close child arguments are incomplete", file=sys.stderr)
            return 1
        try:
            kill_bound_and_write_close_proof(
                argv[1],
                argv[2],
                int(argv[3]),
                int(argv[4]),
                Path(argv[5]),
                Path(argv[6]),
                argv[7],
                argv[8],
                argv[9] if len(argv) == 11 else "",
                argv[10] if len(argv) == 11 else "",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"omo_codex_stop.py: {exc}", file=sys.stderr)
            return 1
        return 0
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
