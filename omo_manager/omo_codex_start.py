#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml"]
# ///
"""Start or resume tracked Codex work in an existing shell-only tmux pane.

Recovery receipts are private, helper-issued, integrity- and replay-checked
filesystem records.  They are not cryptographic provenance against another
process running as the same Unix user, which can write the same private files.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

try:
    from omo_manager.omo_codex_status import Args as StatusArgs
    from omo_manager.omo_codex_status import current_block, exact_tail, inspect, report_from_lines, tail
    from omo_manager.omo_codex_status import status as classify_status
    from omo_manager.omo_codex_stop import query_status_session_id
    from omo_manager.omo_task_lock import task_file_lock, task_target_lock
    from omo_manager.omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, parse_task_metadata
except ModuleNotFoundError:
    from omo_codex_status import Args as StatusArgs
    from omo_codex_status import current_block, exact_tail, inspect, report_from_lines, tail
    from omo_codex_status import status as classify_status
    from omo_codex_stop import query_status_session_id
    from omo_task_lock import task_file_lock, task_target_lock
    from omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, parse_task_metadata

HELPER_DIR = Path(__file__).resolve().parent
WORKER_DEFAULTS = HELPER_DIR / "WORKER_DEFAULTS.md"
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
SUCCESS_STATUSES = {"ready", "running"}
RESTARTABLE_STATUSES = {"error", "ready", "running", "stuck_input", "waiting_subagent"}
ROTATION_TASK_STATUSES = {"blocked", "running"}
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
RESUME_ARG_RE = re.compile(r"resume(?P<session>[0-9a-fA-F-]{36})")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PCODX_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
PCODX_ENV_KEYS = ("PCODX_POC_ROOT", "PCODX_RUN_DIR", "PCODX_LEDGER_PATH", "PCODX_SESSION_ID")
HUMAN_RESTART_ROOT = Path("/shagent/work_logs")
HUMAN_RESTART_TASK_FILE = "human_task_planner.md"
HUMAN_RESTART_AUTHORITY_FILE = Path("manager_mail/85c5dff58359-298.txt")
HUMAN_RESTART_AUTHORITY_LINES = (1, 3)
HUMAN_RESTART_AUTHORITY_SHA256 = "fc0ff6477ae67dc694738d6d6b3146e6a72555a08b2c367360fb3899e2e30a09"
HUMAN_RESTART_AUTHORITY_TEXT = """Subject: Re: human_task_planner.md: hwl:3 restart needs an email-native authorization

Why have you not restarted hwl:3? Do it now"""
HUMAN_RESTART_TARGET = "hwl:3.0"
HUMAN_RESTART_ACTION = "restart"
HUMAN_RESTART_SOURCE_MAX_BYTES = 1_000_000
# Delivery IDs are persisted as filenames.  Keep them opaque but basename-safe
# so a malformed CLI value can never escape the dedicated event directory.
DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
RECOVERY_EVIDENCE_MAX_AGE_S = 300.0
RECOVERY_EVENT_DIRNAME = ".omo-codex-recovery-events"
RECOVERY_RECEIPT_DIRNAME = ".omo-codex-recovery-receipts"
RECOVERY_RECEIPT_VERSION = "omo-codex-recovery-receipt-v1"
RECOVERY_ISSUANCE_VERSION = "omo-codex-recovery-issuance-v1"
DELIVERY_EVENT_DIRNAME = RECOVERY_EVENT_DIRNAME
DELIVERY_EVENT_VERSION = "omo-pending-watch-delivery-event-v1"
CODEX_LAUNCH_COMMAND = "bunx"
PCODX_LAUNCH_COMMAND = str(HELPER_DIR / "pcodx")
UPDATE_AVAILABLE_RE = re.compile(r"^✨\s*Update available! [0-9]+\.[0-9]+\.[0-9]+ -> [0-9]+\.[0-9]+\.[0-9]+$")
UPDATE_PROMPT_SUFFIX = (
    "Release notes: https://github.com/openai/codex/releases/latest",
    "› 1. Update now (runs `bun install -g @openai/codex`)",
    "2. Skip",
    "3. Skip until next version",
    "Press enter to continue",
)


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
    recover_non_codex: bool = False
    recovery_evidence: str = ""
    record_recovery_evidence: bool = False
    recovery_output: Path | None = None
    failed_delivery_id: str = ""
    recover_update_prompt: bool = False
    human_email_file: Path | None = None
    human_email_lines: tuple[int, int] | None = None
    rotate_worker: bool = False
    expected_task_sha256: str = ""
    expected_status: str = ""
    expected_owner_target: str = ""
    expected_pending_items: tuple[str, ...] = ()
    protected_targets: tuple[str, ...] = ()
    audit_output: Path | None = None


@dataclass(frozen=True)
class Pane:
    target: str
    pane_id: str
    window_id: str
    command: str
    workdir: Path
    pane_pid: int = 0


@dataclass(frozen=True)
class TaskBinding:
    is_manager: bool
    tool: str
    status: str
    runat: str
    managerat: str
    pending_task_items: tuple[str, ...]
    task_sha256: str


@dataclass(frozen=True)
class HumanRestartAuthority:
    source_path: Path
    source_lines: tuple[int, int]
    source_dev: int
    source_inode: int
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    action: str
    target: str
    pane_id: str
    window_id: str
    pane_pid: int


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--task-file", required=True, help="Active tracked task whose `runat` names the target pane.")
    _ = parser.add_argument("--target", required=True, help="Exact existing pane: SESSION:WINDOW[.PANE].")
    _ = parser.add_argument("--model", required=True)
    _ = parser.add_argument("--reasoning-effort", required=True, choices=EFFORTS)
    _ = parser.add_argument("--session-id", default="", help="Existing Codex session to resume without a new prompt.")
    _ = parser.add_argument("--prompt-file", type=Path, help="Task-local prompt for a fresh Codex session.")
    _ = parser.add_argument("--startup-timeout-s", type=float, default=45.0)
    _ = parser.add_argument(
        "--confirm-empty-shell",
        action="store_true",
        help="Confirm the target shell has no input to preserve; the helper sends Ctrl-C before launch.",
    )
    _ = parser.add_argument("--restart-running", action="store_true", help="Capture the current Codex session and atomically respawn it in this exact pane.")
    _ = parser.add_argument("--rotate-worker", action="store_true", help="Replace one exact live non-manager Codex worker with a fresh context in the same pane.")
    _ = parser.add_argument("--expected-task-sha256", help="Expected SHA-256 of the tracked task bytes; required with --rotate-worker.")
    _ = parser.add_argument("--expected-status", choices=sorted(ROTATION_TASK_STATUSES), help="Expected preserved task status; required with --rotate-worker.")
    _ = parser.add_argument("--expected-owner-target", help="Expected preserved task manager target; required with --rotate-worker.")
    _ = parser.add_argument("--expected-pending-item", action="append", default=[], help="Exact pending item in order; repeat for the full preserved queue with --rotate-worker.")
    _ = parser.add_argument("--protected-target", action="append", default=[], help="Target that this rotation must not touch; repeat the authoritative protected set with --rotate-worker.")
    _ = parser.add_argument("--audit-output", type=Path, help="New owner-private audit file; required with --rotate-worker.")
    _ = parser.add_argument(
        "--recover-update-prompt",
        action="store_true",
        help="Select Skip only in Codex's exact startup update menu for the supplied resumed session.",
    )
    _ = parser.add_argument(
        "--recover-non-codex",
        action="store_true",
        help="Explicitly replace a verified non-Codex process after a failed delivery; requires --prompt-file and --recovery-evidence.",
    )
    _ = parser.add_argument(
        "--recovery-evidence",
        help="Path to a recent private recovery receipt bound to this pane, its status capture, and a failed delivery (same-UID filesystem trust applies).",
    )
    _ = parser.add_argument(
        "--record-recovery-evidence",
        action="store_true",
        help="Record a one-use failed-delivery receipt after verifying this pane; does not launch Codex.",
    )
    _ = parser.add_argument("--recovery-output", type=Path, help="Watcher-issued receipt path (root/.omo-codex-recovery-receipts/<delivery-id>.receipt).")
    _ = parser.add_argument("--failed-delivery-id", help="Watcher delivery/problem event id for --record-recovery-evidence.")
    _ = parser.add_argument("--human-email-file", type=Path, help="Authoritative human email under ROOT/manager_mail for an h* same-pane restart.")
    _ = parser.add_argument("--human-email-lines", help="Inclusive source line range START-END for --human-email-file.")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv)
    if MODEL_RE.fullmatch(parsed.model) is None:
        parser.error("--model contains unsupported characters.")
    if parsed.session_id and UUID_RE.fullmatch(parsed.session_id) is None:
        parser.error("--session-id must be a Codex UUID.")
    modes = (parsed.restart_running, parsed.rotate_worker, parsed.recover_non_codex, parsed.record_recovery_evidence, parsed.recover_update_prompt)
    if sum(bool(value) for value in modes) > 1:
        parser.error("--restart-running, --rotate-worker, --recover-non-codex, --record-recovery-evidence, and --recover-update-prompt are mutually exclusive.")
    if parsed.restart_running and (parsed.prompt_file or parsed.session_id):
        parser.error("--restart-running captures the live session and does not accept --prompt-file or --session-id.")
    if parsed.rotate_worker:
        if parsed.prompt_file or parsed.session_id:
            parser.error("--rotate-worker starts from the tracked task and does not accept --prompt-file or --session-id.")
        if not all((parsed.expected_task_sha256, parsed.expected_status, parsed.expected_owner_target, parsed.expected_pending_item, parsed.protected_target, parsed.audit_output)):
            parser.error("--rotate-worker requires task digest, status, owner, full nonempty pending queue, protected-target set, and audit output assertions.")
        if SHA256_RE.fullmatch(parsed.expected_task_sha256) is None:
            parser.error("--expected-task-sha256 must be a lowercase SHA-256 value.")
        if any(target_identity(target) is None for target in parsed.protected_target):
            parser.error("--protected-target values must be exact SESSION:WINDOW[.PANE] targets.")
    elif any((parsed.expected_task_sha256, parsed.expected_status, parsed.expected_owner_target, parsed.expected_pending_item, parsed.protected_target, parsed.audit_output)):
        parser.error("rotation assertions are only valid with --rotate-worker.")
    if parsed.recover_non_codex and parsed.session_id:
        parser.error("--recover-non-codex launches a fresh session and does not accept --session-id.")
    if parsed.recover_non_codex and not parsed.prompt_file:
        parser.error("--recover-non-codex requires --prompt-file for the recorded task/prompt context.")
    if parsed.recover_non_codex and not parsed.recovery_evidence:
        parser.error("--recover-non-codex requires --recovery-evidence.")
    if parsed.record_recovery_evidence and (parsed.session_id or parsed.prompt_file):
        parser.error("--record-recovery-evidence records the pane only and does not accept --session-id or --prompt-file.")
    if parsed.record_recovery_evidence and not parsed.recovery_output:
        parser.error("--record-recovery-evidence requires --recovery-output.")
    if parsed.record_recovery_evidence and not parsed.failed_delivery_id:
        parser.error("--record-recovery-evidence requires --failed-delivery-id.")
    if parsed.recover_update_prompt and (not parsed.session_id or parsed.prompt_file):
        parser.error("--recover-update-prompt requires --session-id and does not accept --prompt-file.")
    if parsed.failed_delivery_id and not parsed.record_recovery_evidence:
        parser.error("--failed-delivery-id is only valid with --record-recovery-evidence.")
    if parsed.recovery_output and not parsed.record_recovery_evidence:
        parser.error("--recovery-output is only valid with --record-recovery-evidence.")
    if (parsed.human_email_file is None) != (parsed.human_email_lines is None):
        parser.error("--human-email-file and --human-email-lines must be supplied together.")
    human_email_lines: tuple[int, int] | None = None
    if parsed.human_email_lines is not None:
        match = re.fullmatch(r"([1-9]\d*)-([1-9]\d*)", parsed.human_email_lines)
        if match is None or int(match.group(1)) > int(match.group(2)):
            parser.error("--human-email-lines must be an inclusive START-END range.")
        human_email_lines = int(match.group(1)), int(match.group(2))
    if parsed.human_email_file is not None and not parsed.restart_running:
        parser.error("human email authority is only valid with --restart-running.")
    if not any(modes) and bool(parsed.session_id) == bool(parsed.prompt_file):
        parser.error("provide exactly one of --session-id or --prompt-file.")
    if parsed.recovery_evidence and not parsed.recover_non_codex:
        parser.error("--recovery-evidence is only valid with --recover-non-codex.")
    if not math.isfinite(parsed.startup_timeout_s) or parsed.startup_timeout_s <= 0:
        parser.error("--startup-timeout-s must be finite and positive.")
    if not any(modes) and not parsed.confirm_empty_shell:
        parser.error("--confirm-empty-shell is required because tmux cannot inspect a shell's input buffer.")
    if parsed.failed_delivery_id and DELIVERY_ID_RE.fullmatch(parsed.failed_delivery_id) is None:
        parser.error("--failed-delivery-id contains unsupported characters.")
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
        recover_non_codex=parsed.recover_non_codex,
        recovery_evidence=parsed.recovery_evidence or "",
        record_recovery_evidence=parsed.record_recovery_evidence,
        recovery_output=parsed.recovery_output.expanduser().resolve() if parsed.recovery_output else None,
        failed_delivery_id=parsed.failed_delivery_id or "",
        recover_update_prompt=parsed.recover_update_prompt,
        human_email_file=parsed.human_email_file.expanduser() if parsed.human_email_file else None,
        human_email_lines=human_email_lines,
        rotate_worker=parsed.rotate_worker,
        expected_task_sha256=parsed.expected_task_sha256 or "",
        expected_status=parsed.expected_status or "",
        expected_owner_target=parsed.expected_owner_target or "",
        expected_pending_items=tuple(parsed.expected_pending_item),
        protected_targets=tuple(parsed.protected_target),
        audit_output=parsed.audit_output.expanduser().resolve(strict=False) if parsed.audit_output else None,
    )


def run(command: list[str], *, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)


def target_identity(target: str) -> tuple[str, int, int | None] | None:
    if TARGET_RE.fullmatch(target) is None:
        return None
    session, window_and_pane = target.split(":", 1)
    window, separator, pane = window_and_pane.partition(".")
    return session, int(window), int(pane) if separator else None


def target_is_fresh_rotation_protected(target: str, protected_targets: tuple[str, ...]) -> bool:
    identity = target_identity(target)
    if identity is None:
        return False
    for protected in protected_targets:
        protected_identity = target_identity(protected)
        if protected_identity is not None and protected_identity[:2] == identity[:2]:
            return True
    return False


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
            "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{window_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}",
        ]
    )
    if result.returncode != 0:
        raise StartError(f"tmux target does not exist: {target}")
    if result.stdout == ":.\t\t\t\t\t\n":
        raise StartError(f"tmux target does not exist: {target}")
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 6 or not fields[1].startswith("%") or not fields[2].startswith("@") or not fields[5].isdigit() or int(fields[5]) <= 0:
        raise StartError(f"tmux returned invalid identity for target: {target}")
    resolved_identity = target_identity(fields[0])
    if resolved_identity is None:
        raise StartError(f"tmux returned invalid identity for target: {target}")
    requested_session, requested_window, requested_pane = requested_identity
    resolved_session, resolved_window, resolved_pane = resolved_identity
    if (requested_session, requested_window) != (resolved_session, resolved_window) or (requested_pane is not None and requested_pane != resolved_pane):
        raise StartError(f"tmux target does not exist exactly as requested: {target}")
    return Pane(fields[0], fields[1], fields[2], fields[3], Path(fields[4]), int(fields[5]))


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


def validate_task(args: Args, pane: Pane) -> TaskBinding:
    path = task_path(args.root, args.task_file)
    if not path.is_file():
        raise StartError(f"task file does not exist: {path}")
    task_bytes = path.read_bytes()
    metadata = parse_task_metadata(task_bytes.decode("utf-8"), args.root)
    if metadata is None:
        raise StartError("task file requires valid frontmatter.")
    if metadata.status not in TASK_FRONTMATTER_STATUSES - {"done"}:
        raise StartError(f"task status is not active: {metadata.status}")
    if metadata.tool not in {"codex", "pcodx"}:
        raise StartError(f"same-pane start supports only `tool: codex` or `tool: pcodx`, got {metadata.tool!r}.")
    if metadata.tool == "pcodx" and not args.restart_running:
        raise StartError("same-pane PCODX support is limited to --restart-running with a live state binding.")
    if args.rotate_worker:
        if metadata.is_manager:
            raise StartError("--rotate-worker supports non-manager worker tasks only.")
        if metadata.tool != "codex":
            raise StartError("--rotate-worker supports the ordinary Codex worker boundary only.")
        if metadata.runat != args.target:
            raise StartError("task `runat` does not exactly equal --target for worker rotation.")
        if metadata.status != args.expected_status:
            raise StartError("task status does not equal --expected-status.")
        if metadata.managerat != args.expected_owner_target:
            raise StartError("task owner does not equal --expected-owner-target.")
        if metadata.pending_task_items != args.expected_pending_items:
            raise StartError("task pending queue does not equal the ordered --expected-pending-item assertions.")
        if hashlib.sha256(task_bytes).hexdigest() != args.expected_task_sha256:
            raise StartError("task bytes do not equal --expected-task-sha256.")
    if resolve_pane(metadata.runat).pane_id != pane.pane_id:
        raise StartError(f"task `runat` {metadata.runat} does not identify target {pane.target}.")
    todo = args.root / "TODO.md"
    expected = f"{path.name} {metadata.runat}"
    if not todo.is_file() or expected not in current_todo_entries(todo.read_text(encoding="utf-8")):
        raise StartError(f"TODO `current` does not contain exact task entry: {expected}")
    return TaskBinding(
        metadata.is_manager,
        metadata.tool,
        metadata.status,
        metadata.runat,
        metadata.managerat,
        metadata.pending_task_items,
        hashlib.sha256(task_bytes).hexdigest(),
    )


def human_restart_source(args: Args) -> tuple[Path, os.stat_result, str]:
    """Read the one byte-exact private email authorized for `hwl:3`."""

    if args.human_email_file is None or args.human_email_lines is None:
        raise StartError("human-owned pane restart requires --human-email-file and --human-email-lines.")
    try:
        approved_root = HUMAN_RESTART_ROOT.resolve(strict=True)
    except OSError as error:
        raise StartError(f"approved human restart root is unavailable: {error}") from error
    if args.root.resolve(strict=False) != approved_root:
        raise StartError("human restart authority is bound to the exact approved work-log root.")
    if args.human_email_lines != HUMAN_RESTART_AUTHORITY_LINES:
        raise StartError("human restart authority does not select the exact approved source lines.")
    candidate = args.human_email_file
    if not candidate.is_absolute():
        candidate = args.root / candidate
    try:
        path = candidate.resolve(strict=True)
        approved_path = (approved_root / HUMAN_RESTART_AUTHORITY_FILE).resolve(strict=True)
        mail_root = (approved_root / "manager_mail").resolve(strict=True)
        mail_root_stat = mail_root.stat()
    except OSError as error:
        raise StartError(f"human restart authority source is unavailable: {error}") from error
    if path != approved_path:
        raise StartError("human restart authority does not name the exact approved email file.")
    if (
        path.parent != mail_root
        or not stat.S_ISDIR(mail_root_stat.st_mode)
        or mail_root_stat.st_uid != os.getuid()
        or stat.S_IMODE(mail_root_stat.st_mode) & 0o077
    ):
        raise StartError("human restart authority source directory is not owner-private.")
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            data = source.read(HUMAN_RESTART_SOURCE_MAX_BYTES + 1)
            after = os.fstat(source.fileno())
        current = path.stat()
    except OSError as error:
        raise StartError(f"human restart authority is not readable: {error}") from error
    before_identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    after_identity = after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    current_identity = current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
    if before_identity != after_identity or after_identity != current_identity:
        raise StartError("human restart authority source changed while it was read.")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or len(data) > HUMAN_RESTART_SOURCE_MAX_BYTES
    ):
        raise StartError("human restart authority source is not one bounded owner-private regular file.")
    digest = hashlib.sha256(data).hexdigest()
    if digest != HUMAN_RESTART_AUTHORITY_SHA256:
        raise StartError("human restart authority source content does not match the exact approved email.")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise StartError("human restart authority is not valid UTF-8.") from error
    start_line, end_line = args.human_email_lines
    if end_line > len(lines):
        raise StartError("human restart authority line range exceeds its source email.")
    excerpt = "\n".join(lines[start_line - 1 : end_line])
    if excerpt != HUMAN_RESTART_AUTHORITY_TEXT:
        raise StartError("human restart authority excerpt does not match the exact approved request.")
    return path, before, digest


def require_human_restart_authority(args: Args, pane: Pane) -> HumanRestartAuthority | None:
    """Bind the sole approved human-owned respawn to its exact live inputs."""

    if not pane.target.partition(":")[0].startswith("h"):
        return None
    if not args.restart_running:
        raise StartError("human-owned panes support only email-authorized --restart-running recovery.")
    if pane.target != HUMAN_RESTART_TARGET:
        raise StartError("human restart authority applies only to the exact approved hwl:3 pane.")
    if args.task_file != HUMAN_RESTART_TASK_FILE:
        raise StartError("human restart authority applies only to the exact approved human_task_planner.md task.")
    path, source, digest = human_restart_source(args)
    return HumanRestartAuthority(
        path,
        HUMAN_RESTART_AUTHORITY_LINES,
        source.st_dev,
        source.st_ino,
        source.st_size,
        source.st_mtime_ns,
        digest,
        HUMAN_RESTART_ACTION,
        pane.target,
        pane.pane_id,
        pane.window_id,
        pane.pane_pid,
    )


def verify_human_restart_authority(args: Args, pane: Pane, expected: HumanRestartAuthority | None) -> None:
    """Recheck source and pane/process custody immediately before respawn."""

    if expected is None:
        return
    current = resolve_pane(pane.target)
    if (current.pane_id, current.window_id, current.pane_pid) != (expected.pane_id, expected.window_id, expected.pane_pid):
        raise StartError("human restart authority is stale because the approved pane identity changed.")
    try:
        actual = require_human_restart_authority(args, current)
    except StartError as error:
        raise StartError(f"human restart authority became stale or mismatched: {error}") from error
    if actual != expected:
        raise StartError("human restart authority became stale or mismatched before respawn.")


def verify_task_binding(args: Args, pane: Pane, expected: TaskBinding) -> None:
    """Reject task content or pending-queue drift before or after restart."""

    try:
        actual = validate_task(args, pane)
    except (OSError, UnicodeError, StartError, ValueError) as error:
        raise StartError(f"task or pending queue no longer has its captured binding: {error}") from error
    if actual != expected:
        raise StartError("task or pending queue changed after restart preparation.")


def descendant_pids(root_pid: int) -> set[int]:
    """Return the current descendant process IDs without trusting process text."""

    result = run(["ps", "-eo", "pid=,ppid="])
    if result.returncode != 0:
        raise StartError("could not inspect the live process tree for PCODX recovery.")
    children: dict[int, set[int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        pid, parent = map(int, parts)
        children.setdefault(parent, set()).add(pid)
    descendants: set[int] = set()
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, set()):
            if child not in descendants:
                descendants.add(child)
                frontier.append(child)
    return descendants


def pcodx_state(pane: Pane) -> dict[str, str]:
    """Capture the one live PCODX state binding required for a same-pane resume."""

    candidates: set[tuple[tuple[str, str], ...]] = set()
    for pid in descendant_pids(pane.pane_pid):
        try:
            values = dict(
                field.split("=", 1)
                for field in Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8").split("\0")
                if "=" in field
            )
        except (OSError, UnicodeDecodeError):
            continue
        if values.get("PCODX_MODE") == "1" and all(values.get(key) for key in PCODX_ENV_KEYS):
            candidates.add(tuple((key, values[key]) for key in PCODX_ENV_KEYS))
    if len(candidates) != 1:
        raise StartError("could not capture one unambiguous live PCODX state binding; the pane was not replaced.")
    state = dict(next(iter(candidates)))
    run_dir = Path(state["PCODX_RUN_DIR"])
    ledger = Path(state["PCODX_LEDGER_PATH"])
    if (
        not Path(state["PCODX_POC_ROOT"]).is_dir()
        or not run_dir.is_absolute()
        or not run_dir.is_dir()
        or not ledger.is_absolute()
        or not ledger.is_relative_to(run_dir)
        or (ledger.exists() and not ledger.is_file())
        or PCODX_SESSION_RE.fullmatch(state["PCODX_SESSION_ID"]) is None
    ):
        raise StartError("live PCODX state binding is invalid; the pane was not replaced.")
    return state


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


def launch_command(
    args: Args,
    pane: Pane,
    prompt_path: Path | None,
    marker: str,
    *,
    replace_process: bool = False,
    tool: str = "codex",
    pcodx_env: Mapping[str, str] | None = None,
) -> str:
    executable = CODEX_LAUNCH_COMMAND if tool == "codex" else PCODX_LAUNCH_COMMAND
    codex = [executable]
    if tool == "codex":
        codex.append("@openai/codex")
    codex.extend(("--dangerously-bypass-approvals-and-sandbox", "--model", args.model, "--config", f'model_reasoning_effort="{args.reasoning_effort}"'))
    if tool == "codex":
        codex.extend(("--config", "check_for_update_on_startup=false"))
    if tool == "pcodx":
        if pcodx_env is None or tuple(pcodx_env) != PCODX_ENV_KEYS or any(not pcodx_env[key] for key in PCODX_ENV_KEYS):
            raise StartError("PCODX launch requires an exact live state binding.")
    if args.session_id:
        codex.extend(("resume", args.session_id))
    rendered = shlex.join(codex)
    if prompt_path is not None:
        rendered += f' "$(cat -- {shlex.quote(str(prompt_path))})"'
    exports = [f"export OMO_AGENT_TMUX_TARGET={shlex.quote(pane.target)}"]
    if pcodx_env is not None:
        exports.extend(f"export {key}={shlex.quote(pcodx_env[key])}" for key in PCODX_ENV_KEYS)
    announce = f"printf '%s\\n' {shlex.quote(marker)}"
    execution = f"exec {rendered}" if replace_process else rendered
    return f"{'; '.join(exports)}; cd {shlex.quote(str(pane.workdir))} && {announce} && {execution}"


def verify_same_pane(expected: Pane) -> None:
    current = resolve_pane(expected.target)
    if current.pane_id != expected.pane_id or current.window_id != expected.window_id:
        raise StartError("tmux pane or window identity changed during launch.")


def verify_same_process(expected: Pane) -> None:
    current = resolve_pane(expected.target)
    if current.pane_id != expected.pane_id or current.window_id != expected.window_id:
        raise StartError("tmux pane or window identity changed before process replacement.")
    if current.pane_pid != expected.pane_pid or current.command != expected.command:
        raise StartError("tmux pane process identity changed before process replacement.")


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
    # The format-guarded tmux command evaluates pane, window, and canonical
    # target identity in the server command queue immediately before the
    # destructive operation.  A moved/rebound pane takes the failure branch
    # and is left untouched.
    verify_same_process(pane)
    condition = "#{&&:#{==:#{pane_id},%s},#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.pane_id,
        pane.window_id,
        pane.target,
        pane.pane_pid,
        pane.command,
    )
    respawn_command = "respawn-pane -k -t %s -c %s %s" % (pane.pane_id, shlex.quote(str(pane.workdir)), shlex.quote(command))
    result = run(["tmux", "if-shell", "-F", "-t", pane.pane_id, condition, respawn_command, "run-shell 'exit 1'"])
    if result.returncode != 0:
        detail = result.stderr.strip() or "pane/window identity changed before respawn"
        raise StartError(f"failed to respawn Codex in {pane.target}: {detail}")
    verify_same_pane(pane)


def require_restartable_codex(pane: Pane) -> None:
    verify_same_process(pane)
    report = inspect(StatusArgs(pane.target, 80))
    verify_same_process(pane)
    if report.status not in RESTARTABLE_STATUSES:
        raise StartError(f"target {pane.target} is not a supported live Codex pane: {report.status}")


def parse_recovery_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in text.strip().split(";"):
        key, separator, value = item.partition("=")
        if not separator or not key or key in fields or not value:
            raise StartError("recovery evidence receipt has an invalid field set.")
        fields[key] = value
    return fields


def write_private_recovery_file(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    parent_created = not path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent_created:
        path.parent.chmod(0o700)
    else:
        parent_mode = path.parent.stat().st_mode
        if parent_mode & 0o077:
            raise StartError(f"recovery evidence directory is not private: {path.parent}")
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(text)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def write_private_recovery_file_exclusive(path: Path, text: str) -> None:
    """Create a private issuance record without replacing an existing one."""

    path = path.expanduser().resolve()
    try:
        parent_stat = path.parent.lstat()
    except OSError as error:
        raise StartError(f"recovery issuance directory is not readable: {path.parent}: {error}") from error
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise StartError(f"recovery issuance directory is not private: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise StartError(f"recovery issuance record already exists or cannot be created: {path}: {error}") from error
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(text)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def reserve_rotation_audit(path: Path, text: str) -> None:
    """Reserve one exclusive owner-private audit record before respawning a worker."""

    try:
        parent = path.parent.stat()
    except OSError as error:
        raise StartError(f"rotation audit directory is unavailable: {error}") from error
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise StartError("rotation audit directory must be owner-private.")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise StartError(f"could not reserve private rotation audit: {error}") from error


def finish_rotation_audit(path: Path, prepared: str, result: str, new_session_id: str = "") -> None:
    """Finalize only the exact owner-private rotation audit reserved by this run."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temporary: Path | None = None
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as output:
            current = os.fstat(output.fileno())
            if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600:
                raise StartError("reserved rotation audit lost its owner-private file binding.")
            if output.read() != prepared:
                raise StartError("reserved rotation audit changed before completion.")
        suffix = f"new-session-id: {new_session_id}\n" if new_session_id else ""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(prepared + suffix + f"final-result: {result}\n")
            output.flush()
            os.fsync(output.fileno())
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise StartError("reserved rotation audit changed before atomic finalization.")
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise StartError(f"could not finalize private rotation audit: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def recovery_issuance_path(receipt: Path) -> Path:
    return receipt.with_name(f".{receipt.name}.issued")


def delivery_event_path(root: Path, delivery_id: str) -> Path:
    return root / DELIVERY_EVENT_DIRNAME / f"{delivery_id}.event"


def read_delivery_event(root: Path, pane: Pane, delivery_id: str) -> tuple[Path, dict[str, str], str]:
    event_path = delivery_event_path(root, delivery_id).resolve()
    if not event_path.is_relative_to(root / DELIVERY_EVENT_DIRNAME):
        raise StartError("failed delivery event path is not bound to --root.")
    event_text = recovery_evidence_text(str(event_path))
    fields = parse_recovery_fields(event_text)
    required = {
        "version",
        "producer",
        "event_id",
        "delivery_id",
        "target",
        "pane_id",
        "window_id",
        "status",
        "delivery",
        "source",
        "observed_at",
        "receipt_file",
        "receipt_nonce",
        "tail_sha256",
        "error_sha256",
    }
    if set(fields) != required:
        raise StartError("failed delivery event has an invalid field set.")
    if (
        fields["version"] != DELIVERY_EVENT_VERSION
        or fields["producer"] != "omo_pending_watch"
        or fields["event_id"] != delivery_id
        or fields["delivery_id"] != delivery_id
        or fields["target"] != pane.target
        or fields["pane_id"] != pane.pane_id
        or fields["window_id"] != pane.window_id
        or fields["status"] != "not_codex"
        or fields["delivery"] != "failed"
        or fields["source"] != "omo_pending_watch"
        or fields["receipt_file"] != f"{RECOVERY_RECEIPT_DIRNAME}/{delivery_id}.receipt"
    ):
        raise StartError("failed delivery event is not bound to this pane or watcher.")
    if (
        DELIVERY_ID_RE.fullmatch(delivery_id) is None
        or SHA256_RE.fullmatch(fields["receipt_nonce"]) is None
        or SHA256_RE.fullmatch(fields["tail_sha256"]) is None
        or SHA256_RE.fullmatch(fields["error_sha256"]) is None
    ):
        raise StartError("failed delivery event has an invalid identity or digest.")
    try:
        observed_at = datetime.fromisoformat(fields["observed_at"])
    except ValueError as error:
        raise StartError("failed delivery event has an invalid observed_at timestamp.") from error
    age_s = (datetime.now(timezone.utc) - observed_at).total_seconds() if observed_at.tzinfo is not None else None
    if age_s is None or age_s < -30.0 or age_s > RECOVERY_EVIDENCE_MAX_AGE_S:
        raise StartError("failed delivery event is stale.")
    return event_path, fields, event_text


def record_recovery_evidence(root: Path, pane: Pane, output: Path | None, delivery_id: str) -> None:
    """Record a helper-produced, one-use receipt after a non-destructive probe."""

    root = root.expanduser().resolve()
    if output is None:
        raise StartError("--record-recovery-evidence requires --recovery-output.")
    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("same-pane recovery evidence cannot be recorded for a human-owned `h*` tmux session.")
    if pane.command in SHELL_COMMANDS or not pane.command:
        raise StartError(f"target {pane.target} is running {pane.command or 'unknown'}, not a verified non-Codex process.")
    if DELIVERY_ID_RE.fullmatch(delivery_id) is None:
        raise StartError("failed delivery id contains unsupported characters.")
    try:
        output_path = output.expanduser().resolve()
        receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
        try:
            receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt_stat = receipt_dir.lstat()
        except OSError as error:
            raise StartError(f"recovery evidence directory cannot be prepared: {receipt_dir}: {error}") from error
        if not stat.S_ISDIR(receipt_stat.st_mode) or receipt_stat.st_uid != os.getuid() or stat.S_IMODE(receipt_stat.st_mode) != 0o700:
            raise StartError(f"recovery evidence directory is not a private helper directory: {receipt_dir}")
        if output_path.parent != receipt_dir:
            raise StartError(f"recovery evidence output must be directly under {receipt_dir}")
        if output_path.exists():
            raise StartError(f"recovery evidence output already exists: {output_path}")
    except OSError as error:
        raise StartError(f"recovery evidence output cannot be inspected: {output}: {error}") from error
    captured, captured_lines = exact_tail(pane.target, 80)
    if not captured:
        raise StartError(f"target {pane.target} status capture failed; recovery is not safe.")
    report = report_from_lines(captured_lines)
    if report.status != "not_codex" or not report.lines:
        raise StartError(f"target {pane.target} does not have a nonempty not_codex status capture: {report.status}")
    verify_same_pane(pane)
    event_path, event_fields, event_text = read_delivery_event(root, pane, delivery_id)
    expected_receipt = (root / event_fields["receipt_file"]).resolve()
    if output_path != expected_receipt:
        raise StartError(f"recovery evidence output must be the watcher-issued path {expected_receipt}")
    tail_sha256 = hashlib.sha256("\n".join(captured_lines).encode("utf-8")).hexdigest()
    if event_fields["tail_sha256"] != tail_sha256:
        raise StartError("failed delivery event status capture changed before receipt recording.")
    event_sha256 = hashlib.sha256(event_text.encode("utf-8")).hexdigest()
    receipt_fields = {
        "version": RECOVERY_RECEIPT_VERSION,
        "producer": "omo_codex_start",
        "event_id": delivery_id,
        "event_file": str(event_path.relative_to(root)),
        "receipt_file": str(output_path.relative_to(root)),
        "receipt_nonce": event_fields["receipt_nonce"],
        "event_sha256": event_sha256,
        "target": pane.target,
        "pane_id": pane.pane_id,
        "window_id": pane.window_id,
        "status": "not_codex",
        "delivery": "failed",
        "fresh": "true",
        "source": "omo_pending_watch",
        "delivery_id": delivery_id,
        "observed_at": event_fields["observed_at"],
        "tail_sha256": tail_sha256,
    }
    receipt_text = ";".join(f"{key}={value}" for key, value in receipt_fields.items()) + "\n"
    write_private_recovery_file(output_path, receipt_text)
    receipt_stat = output_path.stat()
    issuance_fields = {
        "version": RECOVERY_ISSUANCE_VERSION,
        "receipt_file": str(output_path.relative_to(root)),
        "receipt_sha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
        "receipt_inode": str(receipt_stat.st_ino),
        "event_id": delivery_id,
        "receipt_nonce": event_fields["receipt_nonce"],
    }
    write_private_recovery_file_exclusive(
        recovery_issuance_path(output_path),
        ";".join(f"{key}={value}" for key, value in issuance_fields.items()) + "\n",
    )


def recovery_evidence_text(value: str) -> str:
    """Read a private, recorded recovery receipt; inline assertions are refused."""

    candidate = Path(value).expanduser()
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(candidate, flags)
        receipt = os.fstat(fd)
    except OSError as error:
        raise StartError(f"recovery evidence receipt is not readable: {candidate}: {error}") from error
    try:
        if not stat.S_ISREG(receipt.st_mode):
            raise StartError(f"recovery evidence must name a recorded receipt file: {candidate}")
        if receipt.st_uid != os.getuid():
            raise StartError(f"recovery evidence receipt is not owned by the invoking user: {candidate}")
        if receipt.st_mode & 0o022:
            raise StartError(f"recovery evidence receipt is group/world writable: {candidate}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            return stream.read()
    except OSError as error:
        raise StartError(f"recovery evidence is not readable: {candidate}: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)


def consume_recovery_receipt(root: Path, evidence: str) -> None:
    root = root.expanduser().resolve()
    receipt = Path(evidence).expanduser().resolve()
    receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
    if receipt.parent != receipt_dir or not receipt.is_relative_to(root):
        raise StartError("recovery evidence receipt must be directly under the helper receipt directory.")
    fields = parse_recovery_fields(recovery_evidence_text(str(receipt)))
    event_path = (root / fields.get("event_file", "")).resolve()
    if not event_path.is_relative_to(root / DELIVERY_EVENT_DIRNAME):
        raise StartError("recovery evidence receipt event path is not bound to --root.")
    issuance_path = recovery_issuance_path(receipt)
    markers = [
        event_path.with_suffix(".used"),
        receipt.with_name(f"{receipt.name}.used"),
        issuance_path.with_name(f"{issuance_path.name}.used"),
    ]
    created: list[Path] = []
    try:
        for marker in markers:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    fd = -1
                    stream.write(datetime.now(timezone.utc).isoformat())
            finally:
                if fd >= 0:
                    os.close(fd)
            created.append(marker)
    except FileExistsError as error:
        for marker in created:
            marker.unlink(missing_ok=True)
        raise StartError("recovery evidence receipt was already consumed.") from error
    except OSError as error:
        for marker in created:
            marker.unlink(missing_ok=True)
        raise StartError(f"could not consume recovery evidence receipt: {error}") from error


def require_recovery_target(pane: Pane, evidence: str, root: Path | None = None) -> None:
    """Require a recent receipt and fresh status capture for a non-Codex pane."""

    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("same-pane non-Codex recovery cannot modify a human-owned `h*` tmux session.")
    current = resolve_pane(pane.target)
    if current.pane_id != pane.pane_id or current.window_id != pane.window_id:
        raise StartError("tmux pane or window identity changed before non-Codex recovery.")
    if current.command in SHELL_COMMANDS or not current.command:
        raise StartError(f"target {current.target} is running {current.command or 'unknown'}, not a verified non-Codex process.")
    receipt_path = Path(evidence).expanduser().resolve()
    if root is None:
        root = receipt_path.parent.parent if receipt_path.parent.name == RECOVERY_RECEIPT_DIRNAME else receipt_path.parent
    root = root.expanduser().resolve()
    receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
    if receipt_path.parent != receipt_dir or not receipt_path.is_relative_to(root):
        raise StartError("recovery evidence receipt must be directly under the helper receipt directory.")
    receipt_text = recovery_evidence_text(evidence)
    fields = parse_recovery_fields(receipt_text)
    required = {
        "target",
        "pane_id",
        "window_id",
        "status",
        "delivery",
        "fresh",
        "source",
        "delivery_id",
        "observed_at",
        "tail_sha256",
        "version",
        "producer",
        "event_id",
        "event_file",
        "event_sha256",
        "receipt_file",
        "receipt_nonce",
    }
    if (
        set(fields) != required
        or fields["receipt_file"] != str(receipt_path.relative_to(root))
        or fields["receipt_file"] != f"{RECOVERY_RECEIPT_DIRNAME}/{fields['event_id']}.receipt"
        or fields["target"] != current.target
        or fields["pane_id"] != current.pane_id
        or fields["window_id"] != current.window_id
    ):
        raise StartError("recovery evidence receipt must bind this pane and window.")
    if (
        fields["version"] != RECOVERY_RECEIPT_VERSION
        or fields["producer"] != "omo_codex_start"
        or fields["status"] != "not_codex"
        or fields["delivery"] != "failed"
        or fields["fresh"] != "true"
        or fields["source"] != "omo_pending_watch"
    ):
        raise StartError("recovery evidence receipt must prove a fresh not_codex result after failed delivery.")
    if DELIVERY_ID_RE.fullmatch(fields["delivery_id"]) is None or DELIVERY_ID_RE.fullmatch(fields["event_id"]) is None:
        raise StartError("recovery evidence receipt has an invalid event identity.")
    if SHA256_RE.fullmatch(fields["event_sha256"]) is None or SHA256_RE.fullmatch(fields["receipt_nonce"]) is None:
        raise StartError("recovery evidence receipt has an invalid event_sha256.")
    event_path = (root / fields["event_file"]).resolve()
    if not event_path.is_relative_to(root) or event_path.name != f"{fields['event_id']}.event" or not event_path.is_relative_to(root / DELIVERY_EVENT_DIRNAME):
        raise StartError("recovery evidence receipt event path is not bound to --root.")
    issuance_path = recovery_issuance_path(receipt_path)
    issuance_used_path = issuance_path.with_name(f"{issuance_path.name}.used")
    if event_path.with_suffix(".used").exists() or receipt_path.with_name(f"{receipt_path.name}.used").exists() or issuance_used_path.exists():
        raise StartError("recovery evidence receipt was already consumed.")
    event_text = recovery_evidence_text(str(event_path))
    if hashlib.sha256(event_text.encode("utf-8")).hexdigest() != fields["event_sha256"]:
        raise StartError("recovery evidence receipt event record changed.")
    event_fields = parse_recovery_fields(event_text)
    expected_event_fields = {
        "version": DELIVERY_EVENT_VERSION,
        "producer": "omo_pending_watch",
        "event_id": fields["event_id"],
        "target": current.target,
        "pane_id": current.pane_id,
        "window_id": current.window_id,
        "status": "not_codex",
        "delivery": "failed",
        "source": "omo_pending_watch",
        "delivery_id": fields["delivery_id"],
        "observed_at": fields["observed_at"],
        "receipt_file": fields["receipt_file"],
        "receipt_nonce": fields["receipt_nonce"],
        "tail_sha256": fields["tail_sha256"],
        "error_sha256": event_fields.get("error_sha256", ""),
    }
    if set(event_fields) != set(expected_event_fields) or any(event_fields[key] != value for key, value in expected_event_fields.items() if key != "error_sha256"):
        raise StartError("recovery evidence receipt is not bound to its helper-produced event.")
    if SHA256_RE.fullmatch(event_fields["error_sha256"]) is None:
        raise StartError("recovery evidence receipt event has an invalid error digest.")
    issuance_text = recovery_evidence_text(str(issuance_path))
    issuance_fields = parse_recovery_fields(issuance_text)
    issuance_required = {"version", "receipt_file", "receipt_sha256", "receipt_inode", "event_id", "receipt_nonce"}
    if set(issuance_fields) != issuance_required or issuance_fields["version"] != RECOVERY_ISSUANCE_VERSION:
        raise StartError("recovery evidence receipt has no valid helper issuance record.")
    if (
        issuance_fields["receipt_file"] != fields["receipt_file"]
        or issuance_fields["event_id"] != fields["event_id"]
        or issuance_fields["receipt_nonce"] != fields["receipt_nonce"]
        or SHA256_RE.fullmatch(issuance_fields["receipt_sha256"]) is None
        or not issuance_fields["receipt_inode"].isdigit()
    ):
        raise StartError("recovery evidence receipt issuance record is not bound to this file.")
    try:
        receipt_stat = receipt_path.stat()
    except OSError as error:
        raise StartError("recovery evidence receipt disappeared before validation.") from error
    if str(receipt_stat.st_ino) != issuance_fields["receipt_inode"] or hashlib.sha256(receipt_text.encode("utf-8")).hexdigest() != issuance_fields["receipt_sha256"]:
        raise StartError("recovery evidence receipt does not match its immutable issuance record.")
    try:
        observed_at = datetime.fromisoformat(fields["observed_at"])
    except ValueError as error:
        raise StartError("recovery evidence receipt has an invalid observed_at timestamp.") from error
    if observed_at.tzinfo is None:
        raise StartError("recovery evidence receipt observed_at must include a timezone.")
    age_s = (datetime.now(timezone.utc) - observed_at).total_seconds()
    if age_s < -30.0 or age_s > RECOVERY_EVIDENCE_MAX_AGE_S:
        raise StartError("recovery evidence receipt is stale or from the future.")
    if SHA256_RE.fullmatch(fields["tail_sha256"]) is None:
        raise StartError("recovery evidence receipt has an invalid tail_sha256.")
    captured, captured_lines = exact_tail(current.target, 80)
    if not captured:
        raise StartError(f"target {current.target} status capture failed; recovery is not safe.")
    report = report_from_lines(captured_lines)
    verify_same_pane(pane)
    if report.status != "not_codex":
        raise StartError(f"target {current.target} no longer has fresh not_codex evidence: {report.status}")
    if not report.lines:
        raise StartError(f"target {current.target} produced an empty status capture; recovery evidence is not fresh.")
    digest = hashlib.sha256("\n".join(captured_lines).encode("utf-8")).hexdigest()
    if digest != fields["tail_sha256"]:
        raise StartError(f"target {current.target} status capture changed after the recovery receipt was recorded.")


def post_marker_lines(pane: Pane, marker: str) -> list[str] | None:
    lines = tail(pane.target, 200)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == marker:
            return lines[index + 1 :]
    return None


def codex_update_prompt_start(lines: list[str]) -> int | None:
    visible = [(index, line.strip()) for index, line in enumerate(lines) if line.strip()]
    n_prompt_lines = len(UPDATE_PROMPT_SUFFIX) + 1
    if len(visible) < n_prompt_lines:
        return None
    prompt = visible[-n_prompt_lines:]
    if UPDATE_AVAILABLE_RE.fullmatch(prompt[0][1]) is None or tuple(line for _, line in prompt[1:]) != UPDATE_PROMPT_SUFFIX:
        return None
    return prompt[0][0]


def is_codex_update_prompt(lines: list[str]) -> bool:
    """Recognize only Codex's active startup update-choice menu."""

    return codex_update_prompt_start(lines) is not None


def require_update_process(pane: Pane) -> Pane:
    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("update-prompt recovery cannot modify a human-owned `h*` tmux session.")
    current = resolve_pane(pane.target)
    if current.pane_id != pane.pane_id or current.window_id != pane.window_id:
        raise StartError("tmux pane or window identity changed before update-prompt recovery.")
    if current.pane_pid != pane.pane_pid:
        raise StartError("tmux pane process identity changed before update-prompt recovery.")
    if current.command != CODEX_LAUNCH_COMMAND:
        raise StartError(f"target {current.target} is running {current.command or 'unknown'}, not the Codex launcher.")
    return current


def require_update_prompt(pane: Pane, session_id: str = "") -> None:
    current = require_update_process(pane)
    captured, lines = exact_tail(current.target, 80)
    if not captured:
        raise StartError(f"target {current.target} update-prompt capture failed; no input was sent.")
    require_update_process(pane)
    prompt_start = codex_update_prompt_start(lines)
    if prompt_start is None:
        raise StartError(f"target {current.target} does not show the exact Codex startup update menu; no input was sent.")
    if session_id:
        compact_launches = "".join("".join(lines[:prompt_start]).split())
        _, launcher, latest_launch = compact_launches.rpartition("@openai/codex")
        resumed_sessions = [match.group("session") for match in RESUME_ARG_RE.finditer(latest_launch)]
        if not launcher or not resumed_sessions or resumed_sessions[-1] != session_id:
            raise StartError(f"target {current.target} update menu is not bound to the latest resumed session {session_id}; no input was sent.")


def skip_codex_update_prompt(pane: Pane, session_id: str = "") -> None:
    """Select documented option `2. Skip` after exact state and identity checks."""

    require_update_prompt(pane, session_id)
    condition = "#{&&:#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.window_id,
        pane.target,
        pane.pane_pid,
        CODEX_LAUNCH_COMMAND,
    )
    send = f"send-keys -t {pane.pane_id} 2 Enter"
    result = run(["tmux", "if-shell", "-F", "-t", pane.pane_id, condition, send, "run-shell 'exit 1'"])
    if result.returncode != 0:
        detail = result.stderr.strip() or "pane/window/process identity changed before update-prompt recovery"
        raise StartError(f"failed to skip Codex update in {pane.target}: {detail}")
    require_update_process(pane)


def wait_update_recovery(pane: Pane, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        verify_same_pane(pane)
        report = inspect(StatusArgs(pane.target, 80))
        if report.status in SUCCESS_STATUSES:
            return report.status
        if report.status == "error":
            raise StartError("Codex update-prompt recovery reached an error state.")
        time.sleep(0.25)
    raise StartError("timed out waiting for Codex after skipping its startup update.")


def wait_started(pane: Pane, marker: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    update_skipped = False
    while time.monotonic() < deadline:
        verify_same_pane(pane)
        lines = post_marker_lines(pane, marker)
        if lines is not None and not update_skipped and is_codex_update_prompt(lines):
            skip_codex_update_prompt(pane)
            update_skipped = True
            time.sleep(0.25)
            continue
        classification = "not_codex" if lines is None else classify_status(lines, current_block(lines))
        if classification in SUCCESS_STATUSES:
            return classification
        if classification == "error":
            raise StartError("Codex startup reached an error state.")
        time.sleep(0.25)
    raise StartError("timed out waiting for Codex to become running or ready.")


def verify_restart_continuity(
    args: Args,
    original: Pane,
    session_id: str,
    task: TaskBinding,
    pcodx: Mapping[str, str] | None,
) -> None:
    """Prove same-pane, resumed-session, task, queue, and PCODX continuity."""

    current = resolve_pane(original.target)
    if current.pane_id != original.pane_id or current.window_id != original.window_id:
        raise StartError("tmux pane or window identity changed after restart.")
    if current.pane_pid == original.pane_pid:
        raise StartError("Codex pane process identity did not change during restart.")
    verify_task_binding(args, current, task)
    if pcodx is not None and pcodx_state(current) != dict(pcodx):
        raise StartError("live PCODX state did not retain its original session custody after restart.")
    resumed_session, _ = query_status_session_id(current.pane_id, 240, min(10.0, args.startup_timeout_s))
    if resumed_session != session_id:
        raise StartError("restarted Codex did not prove continuity with the captured original session.")


def verify_fresh_rotation(args: Args, original: Pane, original_session_id: str, task: TaskBinding) -> str:
    """Prove same-pane fresh-session startup and unchanged task boundaries."""

    current = resolve_pane(original.target)
    if current.pane_id != original.pane_id or current.window_id != original.window_id:
        raise StartError("tmux pane or window identity changed after worker rotation.")
    if current.pane_pid == original.pane_pid:
        raise StartError("Codex pane process identity did not change during worker rotation.")
    verify_task_binding(args, current, task)
    fresh_session_id, _ = query_status_session_id(current.pane_id, 240, min(10.0, args.startup_timeout_s))
    if not fresh_session_id:
        raise StartError("rotated worker did not expose a new Codex session id.")
    if fresh_session_id == original_session_id:
        raise StartError("rotated worker resumed the old Codex session instead of starting fresh.")
    return fresh_session_id


def start(args: Args) -> str:
    modes = (args.restart_running, args.rotate_worker, args.recover_non_codex, args.record_recovery_evidence, args.recover_update_prompt)
    if sum(bool(value) for value in modes) > 1:
        raise StartError("--restart-running, --rotate-worker, --recover-non-codex, --record-recovery-evidence, and --recover-update-prompt are mutually exclusive.")
    if args.rotate_worker:
        if args.session_id or args.prompt_file is not None:
            raise StartError("--rotate-worker starts fresh from the tracked task and does not accept a session or prompt file.")
        if not all((args.expected_task_sha256, args.expected_status, args.expected_owner_target, args.expected_pending_items, args.protected_targets, args.audit_output)):
            raise StartError("--rotate-worker requires all explicit lifecycle, queue, protection, and audit assertions.")
        if args.target.partition(":")[0].startswith("h"):
            raise StartError("worker rotation cannot modify a human-owned `h*` tmux session.")
        if target_is_fresh_rotation_protected(args.target, args.protected_targets):
            raise StartError("worker rotation target is in the explicit protected-target set.")
    if args.recover_non_codex:
        if args.session_id:
            raise StartError("--recover-non-codex launches a fresh session and does not accept --session-id.")
        if args.prompt_file is None:
            raise StartError("--recover-non-codex requires --prompt-file for the recorded task/prompt context.")
        if not args.recovery_evidence:
            raise StartError("--recover-non-codex requires --recovery-evidence.")
    if args.record_recovery_evidence:
        if args.session_id or args.prompt_file is not None:
            raise StartError("--record-recovery-evidence records the pane only and does not accept --session-id or --prompt-file.")
        if args.recovery_output is None:
            raise StartError("--record-recovery-evidence requires --recovery-output.")
        if not args.failed_delivery_id:
            raise StartError("--record-recovery-evidence requires --failed-delivery-id.")
    if args.recover_update_prompt and (not args.session_id or args.prompt_file is not None):
        raise StartError("--recover-update-prompt requires --session-id and does not accept --prompt-file.")
    pane = resolve_pane(args.target)
    human_restart_authority = require_human_restart_authority(args, pane)
    if os.environ.get("TMUX_PANE") == pane.pane_id:
        raise StartError("run this helper from a different pane than the empty target.")
    if not any(modes):
        require_same_shell(pane)
    path = task_path(args.root, args.task_file)
    with task_target_lock(args.root, pane.target), task_file_lock(path):
        verify_same_pane(pane)
        if not any(modes):
            require_same_shell(pane)
        task_binding = validate_task(args, pane)
        if args.restart_running or args.rotate_worker:
            require_restartable_codex(pane)
        if args.recover_non_codex:
            require_recovery_target(pane, args.recovery_evidence, args.root)
        if args.record_recovery_evidence:
            if args.dry_run:
                print(f"target: {pane.target}")
                print("mode: record-recovery-evidence")
                print(f"output: {args.recovery_output}")
                return "dry-run"
            record_recovery_evidence(args.root, pane, args.recovery_output, args.failed_delivery_id)
            return "recovery-evidence-recorded"
        if args.recover_update_prompt:
            if args.dry_run:
                require_update_prompt(pane, args.session_id)
                print(f"target: {pane.target}")
                print("mode: recover-update-prompt")
                print(f"session: {args.session_id}")
                return "dry-run"
            skip_codex_update_prompt(pane, args.session_id)
            return wait_update_recovery(pane, args.startup_timeout_s)
        effective_args = args
        live_pcodx_state: dict[str, str] | None = None
        original_session_id = ""
        if args.restart_running and task_binding.tool == "pcodx":
            live_pcodx_state = pcodx_state(pane)
        if args.restart_running and not args.session_id:
            if args.dry_run:
                effective_args = replace(args, session_id="00000000-0000-4000-8000-000000000000")
            else:
                session_id, _ = query_status_session_id(pane.pane_id, 240, min(10.0, args.startup_timeout_s))
                if not session_id:
                    raise StartError("could not capture the current Codex session id; the pane was not replaced.")
                require_restartable_codex(pane)
                if task_binding.tool == "pcodx" and pcodx_state(pane) != live_pcodx_state:
                    raise StartError("live PCODX state changed during session capture; the pane was not replaced.")
                effective_args = replace(args, session_id=session_id)
        if args.rotate_worker:
            original_session_id, _ = query_status_session_id(pane.pane_id, 240, min(10.0, args.startup_timeout_s))
            if not original_session_id:
                raise StartError("could not capture the current worker session id; the pane was not replaced.")
            require_restartable_codex(pane)
            verify_task_binding(args, pane, task_binding)
            effective_args = replace(args, prompt_file=path, session_id="")
        text = prompt_text(effective_args, False if args.rotate_worker else task_binding.is_manager)
        prompt_path: Path | None = None
        try:
            if text:
                fd, raw_path = tempfile.mkstemp(prefix="omo-codex-start-prompt-", suffix=".txt")
                os.close(fd)
                prompt_path = Path(raw_path)
                prompt_path.chmod(0o600)
                prompt_path.write_text(text, encoding="utf-8")
            marker = f"[omo-codex-start:{os.getpid()}:{time.time_ns()}]"
            command = launch_command(
                effective_args,
                pane,
                prompt_path,
                marker,
                replace_process=args.restart_running or args.rotate_worker or args.recover_non_codex,
                tool=task_binding.tool,
                pcodx_env=live_pcodx_state,
            )
            if args.dry_run:
                print(f"target: {pane.target}")
                mode = "restart-running" if args.restart_running else "rotate-worker" if args.rotate_worker else "recover-non-codex" if args.recover_non_codex else "resume" if args.session_id else "fresh"
                print(f"mode: {mode}")
                print(f"command: {command}")
                return "dry-run"
            if args.restart_running or args.rotate_worker or args.recover_non_codex:
                if args.recover_non_codex:
                    require_recovery_target(pane, args.recovery_evidence, args.root)
                    consume_recovery_receipt(args.root, args.recovery_evidence)
                if args.restart_running:
                    verify_task_binding(args, pane, task_binding)
                    if task_binding.tool == "pcodx" and pcodx_state(pane) != live_pcodx_state:
                        raise StartError("live PCODX state changed before respawn; the pane was not replaced.")
                    verify_human_restart_authority(args, pane, human_restart_authority)
                if args.rotate_worker:
                    verify_task_binding(args, pane, task_binding)
                    current_session_id, _ = query_status_session_id(pane.pane_id, 240, min(10.0, args.startup_timeout_s))
                    if current_session_id != original_session_id:
                        raise StartError("current worker session changed before rotation; the pane was not replaced.")
                    audit_path = args.audit_output
                    if audit_path is None:
                        raise StartError("--rotate-worker requires --audit-output.")
                    queue_sha256 = hashlib.sha256("\0".join(task_binding.pending_task_items).encode()).hexdigest()
                    prepared_audit = "\n".join(
                        (
                            "operation: rotate-worker",
                            f"task-file: {args.task_file}",
                            f"target: {pane.target}",
                            f"pane-id: {pane.pane_id}",
                            f"window-id: {pane.window_id}",
                            f"old-pane-pid: {pane.pane_pid}",
                            f"old-session-id: {original_session_id}",
                            f"task-sha256: {task_binding.task_sha256}",
                            f"status: {task_binding.status}",
                            f"manager-target: {task_binding.managerat}",
                            f"pending-items-sha256: {queue_sha256}",
                            "is-manager: false",
                            "tool: codex",
                            "completion: unknown-until-finalized",
                            "",
                        )
                    )
                    reserve_rotation_audit(audit_path, prepared_audit)
                    try:
                        current_session_id, _ = query_status_session_id(pane.pane_id, 240, min(10.0, args.startup_timeout_s))
                        if current_session_id != original_session_id:
                            raise StartError("current worker session changed after audit reservation; the pane was not replaced.")
                        verify_task_binding(args, pane, task_binding)
                        respawn_codex(pane, command)
                        result = wait_started(pane, marker, args.startup_timeout_s)
                        new_session_id = verify_fresh_rotation(args, pane, original_session_id, task_binding)
                    except Exception as rotation_error:
                        try:
                            finish_rotation_audit(audit_path, prepared_audit, "failed")
                        except Exception as audit_error:
                            rotation_error.add_note(f"private audit finalization also failed; audit remains completion-unknown: {audit_error}")
                        raise
                    finish_rotation_audit(audit_path, prepared_audit, "success", new_session_id)
                    return result
                respawn_codex(pane, command)
            else:
                require_same_shell(pane)
                send_shell_command(pane, command)
            result = wait_started(pane, marker, args.startup_timeout_s)
            if args.restart_running:
                verify_restart_continuity(effective_args, pane, effective_args.session_id, task_binding, live_pcodx_state)
            return result
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
